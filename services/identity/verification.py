"""本校核验闭环（D3a）：申请状态机、邮箱挑战、审批与登记（规格 5.1）。

流程（规格 5.1 五步）：发起申请（姓名/学校，自填只预填不占位）→ 域/映射规则
校验 → 独立 school_identity 挑战验证邮箱控制权 → 核验负责人名册核对批准 →
同事务登记唯一身份 key + 置 person verified + 账本。

挑战语义（规格 8.1）：6 位安全随机、5 分钟、5 次错锁死、重发使旧码失效、
60s 冷却 + 时/日限次（Redis，不可达 503 fail-closed）；短码只存专用密钥
HMAC 摘要；payload_digest 绑定申请（声明变更后旧码不可用）；一次性消费。

已注册校园账号属账号认领问题（D3b），核验不偷偷变成第二账号授权（规格 5.1.6）。
"""
import re
import secrets
import string
import uuid
from datetime import datetime, timedelta

from exts import db, redis_client
from models import IdentityApplicationModel, IdentityChallengeModel, PersonModel
from services.auth_context import AuthRejected, challenge_digest, constant_time_eq
from services.identity import events, school
from services.identity.errors import IdentityKeyConflict
from services.identity.registry import register_identity_key

CHALLENGE_TTL = 300
MAX_ATTEMPTS = 5
RESEND_COOLDOWN = 60
HOURLY_LIMIT = 5
DAILY_LIMIT = 10

PURPOSE = 'school_identity'
_IDENTIFIER_RE = re.compile(r'^[A-Za-z0-9._-]{2,64}$')


# ── 申请 ────────────────────────────────────────────────────────

def create_or_update_application(user, *, school_id, claimed_name,
                                 claimed_identifier, contact_email):
    """建/改草稿（不 commit）。同一用户同校仅一个活跃申请；声明变更
    （标识/邮箱）使既有邮箱证明作废——challenge_verified_at 清零重验。"""
    cfg = school.get_school_config(school_id)
    if user.account_kind in ('service',):
        raise AuthRejected('该类账号不能发起身份核验', status=403, machine='FORBIDDEN')
    name = (claimed_name or '').strip()
    if not name or len(name) > 100:
        raise AuthRejected('请填写待核验姓名', status=400, machine='BAD_CLAIM')
    identifier = (claimed_identifier or '').strip()
    if not _IDENTIFIER_RE.match(identifier):
        raise AuthRejected('NetID/学号格式不合法（2-64 位字母数字与 . _ -）',
                           status=400, machine='BAD_CLAIM')
    normalized = school.validate_contact_email(cfg, contact_email, identifier)

    app = IdentityApplicationModel.query.filter(
        IdentityApplicationModel.applicant_user_id == user.id,
        IdentityApplicationModel.school_id == school_id,
        IdentityApplicationModel.status.notin_(
            IdentityApplicationModel.TERMINAL)).first()
    if app is None:
        changed = True
        action = 'identity.application.create'
        app = IdentityApplicationModel(
            school_id=school_id, applicant_user_id=user.id,
            applicant_person_id=user.person_id,
            claimed_name=name, claimed_identifier=identifier,
            contact_email=normalized)
        db.session.add(app)
        db.session.flush()
    elif app.status != 'draft':
        raise AuthRejected('已有待审核申请，不能修改；如需更正请先撤回',
                           status=409, machine='APPLICATION_ACTIVE')
    else:
        action = 'identity.application.update'
        changed = (app.claimed_identifier, app.contact_email) != (identifier, normalized)
        app.claimed_name = name
        app.claimed_identifier = identifier
        app.contact_email = normalized
    if changed and action != 'identity.application.create':
        app.challenge_verified_at = None  # 声明变了，旧邮箱证明作废（防挪用）
    events.record_event(
        action, actor_user_id=user.id,
        target_ids={'user_id': user.id, 'person_id': user.person_id},
        before=None, after={'status': app.status},
        evidence_refs={'note': f'school={school_id}'},
        reason='核验申请草稿')
    return app


def submit_application(user, app):
    """提交审核（不 commit）：必须先通过邮箱控制证明。"""
    _require_owner(user, app)
    if app.status != 'draft':
        raise AuthRejected('申请不在可提交状态', status=409, machine='BAD_STATE')
    if not app.challenge_verified_at:
        raise AuthRejected('请先完成邮箱验证再提交', status=400,
                           machine='CHALLENGE_REQUIRED')
    before = app.status
    app.status = 'submitted'
    events.record_event(
        'identity.application.submit', actor_user_id=user.id,
        target_ids={'user_id': user.id, 'person_id': app.applicant_person_id},
        before={'status': before}, after={'status': app.status},
        reason='提交核验审核')
    return app


def withdraw_application(user, app):
    """撤回（不 commit）：仅申请人本人、终态前。"""
    _require_owner(user, app)
    if app.status in IdentityApplicationModel.TERMINAL:
        raise AuthRejected('申请已结束，无需撤回', status=409, machine='BAD_STATE')
    before = app.status
    app.status = 'withdrawn'
    events.record_event(
        'identity.application.withdraw', actor_user_id=user.id,
        target_ids={'user_id': user.id, 'person_id': app.applicant_person_id},
        before={'status': before}, after={'status': app.status},
        reason='申请人撤回')
    return app


# ── 邮箱挑战 ────────────────────────────────────────────────────

def _payload_digest(app):
    return challenge_digest(f'{PURPOSE}:{app.id}:{app.contact_email}:{app.claimed_identifier}')


def _require_redis():
    try:
        redis_client.ping()
    except Exception as e:
        raise AuthRejected('验证服务暂不可用，请稍后重试',
                           status=503, machine='SERVICE_UNAVAILABLE') from e


def _rl_keys(email):
    now = datetime.now()
    return (f'idch:rl:{email}:{now:%Y%m%d%H}', f'idch:rl:{email}:{now:%Y%m%d}')


def issue_challenge(user, app):
    """生成挑战（不 commit；返回短码明文仅供发送邮件，绝不回 API 响应）。

    重发使旧码失效（旧未消费行作废）；60s 冷却 + 时/日限次。
    """
    _require_owner(user, app)
    if app.status in IdentityApplicationModel.TERMINAL:
        raise AuthRejected('申请已结束', status=409, machine='BAD_STATE')
    _require_redis()
    email = app.contact_email
    cd_key = f'idch:cd:{email}'
    if redis_client.exists(cd_key):
        ttl = redis_client.ttl(cd_key)
        raise AuthRejected(f'发送过于频繁，请 {max(ttl, 1)} 秒后再试',
                           status=429, machine='CHALLENGE_COOLDOWN')
    hour_key, day_key = _rl_keys(email)
    if (int(redis_client.get(hour_key) or 0) >= HOURLY_LIMIT
            or int(redis_client.get(day_key) or 0) >= DAILY_LIMIT):
        raise AuthRejected('今日验证码发送次数已达上限，请明日再试',
                           status=429, machine='CHALLENGE_LIMIT')

    code = ''.join(secrets.choice(string.digits) for _ in range(6))
    now = datetime.now()
    # 旧码失效：同申请未消费的挑战全部作废
    for old in IdentityChallengeModel.query.filter_by(
            application_id=app.id, purpose=PURPOSE, consumed_at=None).all():
        old.consumed_at = now
    ch = IdentityChallengeModel(
        id=uuid.uuid4().hex, purpose=PURPOSE, application_id=app.id,
        actor_user_id=user.id, target_user_id=None,
        destination_digest=challenge_digest(email),
        secret_digest=challenge_digest(f'{PURPOSE}:{app.id}:{email}:{code}'),
        payload_digest=_payload_digest(app),
        expires_at=now + timedelta(seconds=CHALLENGE_TTL))
    db.session.add(ch)
    redis_client.setex(cd_key, RESEND_COOLDOWN, '1')
    for key, window in ((hour_key, 3700), (day_key, 90000)):
        redis_client.incr(key)
        if redis_client.ttl(key) < 0:
            redis_client.expire(key, window)
    events.record_event(
        'identity.challenge.issue', actor_user_id=user.id,
        target_ids={'user_id': user.id},
        evidence_refs={'challenge_id': ch.id},
        reason='发送核验邮箱验证码')
    return code, ch


def verify_challenge(user, app, code):
    """校验并一次性消费（不 commit）。错 5 次锁死；申请声明变更后旧码不可用。"""
    _require_owner(user, app)
    ch = IdentityChallengeModel.query.filter_by(
        application_id=app.id, purpose=PURPOSE, consumed_at=None)\
        .order_by(IdentityChallengeModel.created_at.desc()).first()
    if ch is None:
        return False
    now = datetime.now()
    if ch.expires_at <= now or ch.payload_digest != _payload_digest(app):
        ch.consumed_at = now
        return False
    given = challenge_digest(f'{PURPOSE}:{app.id}:{app.contact_email}:{(code or "").strip()}')
    if constant_time_eq(given, ch.secret_digest):
        ch.consumed_at = now
        app.challenge_verified_at = now
        events.record_event(
            'identity.challenge.verify', actor_user_id=user.id,
            target_ids={'user_id': user.id},
            evidence_refs={'challenge_id': ch.id},
            reason='邮箱控制权验证通过')
        return True
    ch.attempts += 1
    if ch.attempts >= MAX_ATTEMPTS:
        ch.consumed_at = now  # 锁死：重新申请挑战
    return False


def invalidate_challenge(ch):
    """邮件发送失败时作废挑战（规格 14：发送失败 challenge 不算成功）。"""
    if ch and ch.consumed_at is None:
        ch.consumed_at = datetime.now()


# ── 审批 ────────────────────────────────────────────────────────

def approve_application(app, reviewer, *, note=''):
    """审批通过（不 commit）：负责人门槛 + 同事务登记唯一身份 key。

    key 冲突不覆盖、不泄露归属人（规格 8.2.4/15 章）——可恢复冲突，申请留在
    submitted 由人工处理（名册核对后走更正/争议流程）。
    """
    cfg = school.get_school_config(app.school_id)
    school.require_reviewer(cfg, reviewer)
    if app.status == 'approved' and app.reviewed_by == reviewer.id:
        return app  # 幂等重放
    if app.status != 'submitted':
        raise AuthRejected('申请不在待审状态', status=409, machine='BAD_STATE')
    if not app.challenge_verified_at:
        raise AuthRejected('申请人尚未完成邮箱验证，不能批准',
                           status=400, machine='CHALLENGE_REQUIRED')

    person = db.session.get(PersonModel, app.applicant_person_id) if \
        app.applicant_person_id else None
    if person is None:
        raise AuthRejected('申请人缺少人员档案（数据异常）', status=500,
                           machine='PERSON_MISSING')
    before = {'status': app.status, 'verification_status': person.verification_status}
    try:
        register_identity_key(
            person, issuer=cfg.school_id, kind='netid',
            key=app.claimed_identifier,
            assurance_method='school_email',
            proof_ref=f'application#{app.id}')
    except IdentityKeyConflict:
        raise AuthRejected('该 NetID 已被其他档案登记，不能重复核验通过；'
                           '请人工核对名册后按争议流程处理',
                           status=409, machine='IDENTITY_KEY_CONFLICT')
    person.verification_status = 'verified'
    person.verified_name = app.claimed_name
    app.status = 'approved'
    app.reviewed_by = reviewer.id
    app.reviewed_at = datetime.now()
    events.record_event(
        'identity.application.approve', actor_user_id=reviewer.id,
        target_ids={'user_id': app.applicant_user_id,
                    'person_id': person.id,
                    'primary_user_id': app.applicant_user_id},
        before=before,
        after={'status': app.status, 'verification_status': person.verification_status,
               'issuer': cfg.school_id, 'kind': 'netid',
               'assurance_method': 'school_email', 'proof_ref': f'application#{app.id}'},
        evidence_refs={'note': note[:64] if note else ''},
        reason='核验负责人批准')
    return app


def reject_application(app, reviewer, *, reason):
    """驳回（不 commit）：原因必留（支持补交新版本——重新建申请）。"""
    cfg = school.get_school_config(app.school_id)
    school.require_reviewer(cfg, reviewer)
    if app.status in IdentityApplicationModel.TERMINAL:
        raise AuthRejected('申请已结束', status=409, machine='BAD_STATE')
    reason = (reason or '').strip()
    if not reason:
        raise AuthRejected('请填写驳回原因', status=400, machine='BAD_REQUEST')
    before = app.status
    app.status = 'rejected'
    app.reject_reason = reason[:255]
    app.reviewed_by = reviewer.id
    app.reviewed_at = datetime.now()
    events.record_event(
        'identity.application.reject', actor_user_id=reviewer.id,
        target_ids={'user_id': app.applicant_user_id,
                    'person_id': app.applicant_person_id},
        before={'status': before}, after={'status': app.status},
        evidence_refs={'note': reason[:64]},
        reason='核验负责人驳回')
    return app


# ── 读侧 ────────────────────────────────────────────────────────

def list_my_applications(user):
    return IdentityApplicationModel.query.filter_by(
        applicant_user_id=user.id).order_by(IdentityApplicationModel.id.desc()).all()


def application_overview(user):
    """/identity/status 概览：人员核验态 + 当前申请 + 各校配置（脱敏）。"""
    from models import IdentitySchoolConfigModel
    person = db.session.get(PersonModel, user.person_id) if user.person_id else None
    apps = list_my_applications(user)
    schools = [{'school_id': c.school_id, 'name': c.name} for c in
               IdentitySchoolConfigModel.query.order_by(
                   IdentitySchoolConfigModel.school_id).all()]
    return {
        'person': None if person is None else {
            'public_id': person.public_id,
            'verification_status': person.verification_status,
            'verified_name': person.verified_name,
            'record_status': person.record_status,
        },
        'applications': [_app_brief(a) for a in apps],
        'schools': schools,
    }


def _app_brief(app):
    return {
        'id': app.id, 'school_id': app.school_id,
        'claimed_name': app.claimed_name,
        'claimed_identifier': app.claimed_identifier,
        'contact_email': app.contact_email,
        'status': app.status,
        'challenge_verified': bool(app.challenge_verified_at),
        'reject_reason': app.reject_reason,
        'submitted': app.status != 'draft',
        'created_at': app.created_at.strftime('%Y-%m-%d %H:%M') if app.created_at else None,
    }


def _require_owner(user, app):
    if app.applicant_user_id != user.id:
        # 不区分「不存在/无权」——防他人枚举申请 id（规格 16 隐私/越权行）
        raise AuthRejected('申请不存在', status=404, machine='NOT_FOUND')

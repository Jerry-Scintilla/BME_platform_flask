"""外校名册路径（D3c，规格 5.2）：导入、邀请、领取、审批回链。

流程：负责人导入已确认名单（roster_ref 稳定引用码 upsert，批次可变人员不重建）
→ 对条目发认领邀请（一次性令牌，7 天，邮件链接）→ 持邀请者在身份中心领取：
  验证名册联系邮箱控制权（挑战绑定邀请，转发邀请不构成核验成功）→ 自动生成
  method='roster' 的核验申请（submitted）→ 负责人审批通过 → 登记
  kind='roster_ref' 身份键 + 名册条目绑定 Person（后续来访找回同一人）。
无名单者走 method='manual' 人工申请（kind='email' 以已验邮箱为键）——两条路
共用 D3a 的申请状态机与审批队列。

外校学校配置：personal_email_domains 为空 = 无自动域校验（不做域匹配/NetID
映射），凭「邮箱控制 + 名册/人工核对」双因素（规格 5.2.6 语义）。
"""
import secrets
import string
import uuid
from datetime import datetime, timedelta

from exts import db, redis_client
from models import (IdentityApplicationModel, IdentityChallengeModel,
                    IdentityRosterInviteModel, IdentityRosterModel,
                    UserModel)
from services.auth_context import AuthRejected, challenge_digest, constant_time_eq
from services.identity import events, school

INVITE_DAYS = 7
CLAIM_PURPOSE = 'roster_claim'
CHALLENGE_TTL = 300
MAX_ATTEMPTS = 5


# ── 名册导入（管理端）────────────────────────────────────────────

def import_roster(operator, *, school_id, rows, dry_run=False):
    """按 (school_id, roster_ref) upsert——批次可变，人员不因新批次重建（5.2）。

    rows: [{roster_ref, name, contact_email, institution_id?}]；
    返回 (created, updated, skipped, errors) 摘要；dry_run 不落库。
    """
    cfg = school.get_school_config(school_id)
    created = updated = skipped = 0
    errors = []
    from services.identity.registry import canonicalize
    for i, row in enumerate(rows or []):
        ref = (row.get('roster_ref') or '').strip()
        name = (row.get('name') or '').strip()
        try:
            email = canonicalize('email', row.get('contact_email') or '')
        except ValueError as e:
            errors.append(f'第 {i + 1} 行联系方式无效：{e}')
            continue
        if not ref or not name:
            errors.append(f'第 {i + 1} 行缺少 roster_ref 或姓名')
            continue
        existing = IdentityRosterModel.query.filter_by(
            school_id=school_id, roster_ref=ref).first()
        if existing is None:
            if not dry_run:
                db.session.add(IdentityRosterModel(
                    school_id=school_id, roster_ref=ref, name=name,
                    contact_email=email,
                    institution_id=(row.get('institution_id') or '').strip() or None,
                    owner_user_id=operator.id))
            created += 1
        else:
            # 同引用码：更新联系方式/姓名/学号，认领绑定不动（找回同一人）
            changed = (existing.name, existing.contact_email) != (name, email)
            existing.name = name
            existing.contact_email = email
            if row.get('institution_id'):
                existing.institution_id = row['institution_id'].strip()
            updated += 1 if changed else 0
            skipped += 0 if changed else 1
    if not dry_run:
        events.record_event(
            'identity.roster.import', actor_user_id=operator.id,
            evidence_refs={'note': f'{school_id}: +{created} ~{updated}'},
            reason='名册导入（upsert by ref）')
        db.session.commit()
    return {'created': created, 'updated': updated, 'skipped': skipped,
            'errors': errors}


# ── 邀请（管理端）────────────────────────────────────────────────

def issue_invite(operator, roster_id):
    """发认领邀请：一次性令牌 7 天（过期人工重发=新令牌，旧令牌作废）。

    返回 (invite, link_path)：邮件发送由蓝图负责（发送失败作废邀请，
    规格 14「邮件失败 challenge 不算成功」同语义）。
    """
    entry = db.session.get(IdentityRosterModel, roster_id)
    if entry is None:
        raise AuthRejected('名册条目不存在', status=404, machine='NOT_FOUND')
    # 旧未用令牌作废（重发语义）
    for old in IdentityRosterInviteModel.query.filter_by(
            roster_id=roster_id, used_at=None).all():
        old.used_at = datetime.now()
    invite = IdentityRosterInviteModel(
        id=uuid.uuid4().hex, roster_id=roster_id, issued_by=operator.id,
        expires_at=datetime.now() + timedelta(days=INVITE_DAYS))
    db.session.add(invite)
    db.session.flush()
    events.record_event(
        'identity.roster.invite', actor_user_id=operator.id,
        target_ids=None,
        evidence_refs={'note': f'roster#{roster_id}'},
        reason='发出认领邀请')
    link = f'/user-center/identity?invite={invite.id}'
    return invite, link


# ── 领取（用户端，登录态）────────────────────────────────────────

def _payload_digest(invite_id, email):
    return challenge_digest(f'{CLAIM_PURPOSE}:{invite_id}:{email}')


def start_claim(user, token):
    """领取第一步：校验邀请有效性 → 向名册联系邮箱发验证码。

    返回 (entry, invite)；码由蓝图发邮件（不回响应）。挑战绑定邀请与邮箱——
    转发邀请者过不了邮箱控制这一关（规格 5.2）。
    """
    invite = _live_invite(token)
    entry = db.session.get(IdentityRosterModel, invite.roster_id)
    if entry.status != 'active':
        raise AuthRejected('该名册条目已失效', status=409, machine='BAD_STATE')
    if entry.claimed_person_id and entry.claimed_person_id != user.person_id:
        raise AuthRejected('该名册条目已被认领', status=409, machine='ALREADY_CLAIMED')
    if user.account_kind == 'service':
        raise AuthRejected('该类账号不能发起认领', status=403, machine='FORBIDDEN')
    code = _issue_claim_challenge(invite, entry, user)
    return entry, invite, code


def _issue_claim_challenge(invite, entry, user=None):
    try:
        cd_key = f'idch:cd:{entry.contact_email}'
        if redis_client.exists(cd_key):
            ttl = redis_client.ttl(cd_key)
            raise AuthRejected(f'发送过于频繁，请 {max(ttl, 1)} 秒后再试',
                               status=429, machine='CHALLENGE_COOLDOWN')
    except AuthRejected:
        raise
    except Exception as e:
        raise AuthRejected('验证服务暂不可用，请稍后重试',
                           status=503, machine='SERVICE_UNAVAILABLE') from e
    now = datetime.now()
    for old in IdentityChallengeModel.query.filter_by(
            purpose=CLAIM_PURPOSE, consumed_at=None)\
            .filter(IdentityChallengeModel.case_id == invite.id).all():
        old.consumed_at = now
    code = ''.join(secrets.choice(string.digits) for _ in range(6))
    db.session.add(IdentityChallengeModel(
        id=uuid.uuid4().hex, purpose=CLAIM_PURPOSE, application_id=None,
        case_id=invite.id,
        actor_user_id=user.id if user else None, target_user_id=None,
        destination_digest=challenge_digest(entry.contact_email),
        secret_digest=challenge_digest(
            f'{CLAIM_PURPOSE}:{invite.id}:{entry.contact_email}:{code}'),
        payload_digest=_payload_digest(invite.id, entry.contact_email),
        expires_at=now + timedelta(seconds=CHALLENGE_TTL)))
    redis_client.setex(cd_key, 60, '1')
    return code


def verify_claim(user, token, code):
    """领取第二步：验码（一次性/5 错锁死）→ 生成 method='roster' 申请进审核队列。"""
    invite = _live_invite(token)
    entry = db.session.get(IdentityRosterModel, invite.roster_id)
    ch = IdentityChallengeModel.query.filter_by(
        purpose=CLAIM_PURPOSE, consumed_at=None,
        case_id=invite.id).order_by(
        IdentityChallengeModel.created_at.desc()).first()
    now = datetime.now()
    if ch is None or ch.expires_at <= now:
        return None
    given = challenge_digest(
        f'{CLAIM_PURPOSE}:{invite.id}:{entry.contact_email}:{(code or "").strip()}')
    if not constant_time_eq(given, ch.secret_digest):
        ch.attempts += 1
        if ch.attempts >= MAX_ATTEMPTS:
            ch.consumed_at = now
        return None
    ch.consumed_at = now
    invite.used_at = now
    # 生成申请（submitted 直入队列；邮箱控制证明已含，复核靠负责人名册核对）
    app = IdentityApplicationModel(
        school_id=entry.school_id, applicant_user_id=user.id,
        applicant_person_id=user.person_id,
        claimed_name=entry.name, claimed_identifier=entry.roster_ref,
        contact_email=entry.contact_email, method='roster',
        roster_id=entry.id, status='submitted',
        challenge_verified_at=now)
    db.session.add(app)
    db.session.flush()
    events.record_event(
        'identity.roster.claim', actor_user_id=user.id,
        target_ids={'user_id': user.id, 'person_id': user.person_id},
        after={'status': 'submitted'},
        evidence_refs={'note': f'roster#{entry.id}'},
        reason='名册认领（邮箱控制已验证）')
    return app


def _live_invite(token):
    invite = db.session.get(IdentityRosterInviteModel, (token or '').strip())
    if invite is None or invite.used_at is not None or invite.expires_at <= datetime.now():
        raise AuthRejected('邀请无效或已使用，请联系负责人重新发送',
                           status=404, machine='INVITE_INVALID')
    return invite


def claim_preview(token):
    """领取入口的预览（不泄露全名/全邮箱——领取者本就应有该邮箱）。"""
    invite = _live_invite(token)
    entry = db.session.get(IdentityRosterModel, invite.roster_id)
    from models import IdentitySchoolConfigModel
    cfg = db.session.get(IdentitySchoolConfigModel, entry.school_id)
    email = entry.contact_email
    local, _, domain = email.partition('@')
    masked = (local[:2] + '***') if len(local) > 2 else (local[:1] + '***')
    return {'school': cfg.name if cfg else entry.school_id,
            'name': entry.name, 'contact_masked': f'{masked}@{domain}'}

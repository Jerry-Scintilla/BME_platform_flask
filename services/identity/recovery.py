"""恢复申诉骨架（D3c，规格 7.4）：丢失账号/因素的人工恢复案例 + 冷静期。

原则（规格 7.4）：不凭校园邮箱和同名自动领取旧账号；人工恢复由负责人按已确认
名册/既有关系/可核对记录验证；安全问题和公开资料不作唯一证据；不收证件影像。
公开入口统一话术（不回显账号存在性）；联系邮箱须验控制权（挑战绑定案例）；
批准进入冷静期（默认 24h；特权账号或存异议 72h 且需两名不同审核人复核）；
执行走既有运维通道（find_password/人工重置+撤会话），本服务承载状态机与审计。
"""
import secrets
import string
import uuid
from datetime import datetime, timedelta

from exts import db, redis_client
from models import IdentityChallengeModel, IdentityRecoveryCaseModel, UserModel
from services.auth_context import AuthRejected, challenge_digest, constant_time_eq
from services.identity import events

RECOVERY_PURPOSE = 'recovery_claim'
CHALLENGE_TTL = 600           # 恢复验证码 10 分钟（公开入口给足输入时间）
MAX_ATTEMPTS = 5
COOLDOWN_HOURS = 24
COOLDOWN_PRIVILEGED_HOURS = 72


def _case_ref(case):
    return f'recovery:{case.id}'


def _payload_digest(case):
    return challenge_digest(f'{RECOVERY_PURPOSE}:{case.id}:{case.contact_email}')


def submit_case(*, kind, target_email, contact_email, statement):
    """公开提交（未登录可用；统一话术防账号枚举）。

    建草稿案例并向联系邮箱发验证码（码由蓝图发邮件）。限流：同联系邮箱
    60s 冷却——防公开入口被刷。
    """
    if kind not in ('account_lost', 'factor_lost'):
        raise AuthRejected('恢复类型不合法', status=400, machine='BAD_REQUEST')
    statement = (statement or '').strip()
    if len(statement) < 20:
        raise AuthRejected('请具体描述丢失情况与可核对的信息（至少 20 字）',
                           status=400, machine='BAD_REQUEST')
    from services.identity.registry import canonicalize
    try:
        target = canonicalize('email', target_email or '')
        contact = canonicalize('email', contact_email or '')
    except ValueError:
        raise AuthRejected('请填写有效的邮箱', status=400, machine='BAD_REQUEST')
    if target == contact:
        raise AuthRejected('联系邮箱不能与丢失账号邮箱相同（丢失即不可达）',
                           status=400, machine='BAD_REQUEST')
    try:
        cd_key = f'idch:cd:{contact}'
        if redis_client.exists(cd_key):
            raise AuthRejected(f'提交过于频繁，请 {max(redis_client.ttl(cd_key), 1)} 秒后再试',
                               status=429, machine='CHALLENGE_COOLDOWN')
    except AuthRejected:
        raise
    except Exception as e:
        raise AuthRejected('服务暂不可用，请稍后重试',
                           status=503, machine='SERVICE_UNAVAILABLE') from e

    case = IdentityRecoveryCaseModel(
        kind=kind, target_email=target, contact_email=contact,
        statement=statement[:2000])
    db.session.add(case)
    db.session.flush()
    code = _issue_challenge(case)
    return case, code


def _issue_challenge(case):
    now = datetime.now()
    for old in IdentityChallengeModel.query.filter_by(
            purpose=RECOVERY_PURPOSE, consumed_at=None)\
            .filter(IdentityChallengeModel.case_id == _case_ref(case)).all():
        old.consumed_at = now
    code = ''.join(secrets.choice(string.digits) for _ in range(6))
    db.session.add(IdentityChallengeModel(
        id=uuid.uuid4().hex, purpose=RECOVERY_PURPOSE, application_id=None,
        case_id=_case_ref(case), actor_user_id=None, target_user_id=None,
        destination_digest=challenge_digest(case.contact_email),
        secret_digest=challenge_digest(
            f'{RECOVERY_PURPOSE}:{case.id}:{case.contact_email}:{code}'),
        payload_digest=_payload_digest(case),
        expires_at=now + timedelta(seconds=CHALLENGE_TTL)))
    redis_client.setex(f'idch:cd:{case.contact_email}', 60, '1')
    return code


def verify_case(case_id, code):
    """验证联系邮箱控制权 → 案例进入 submitted 队列。一次性/5 错锁死。"""
    case = db.session.get(IdentityRecoveryCaseModel, case_id)
    if case is None or case.status != 'draft':
        raise AuthRejected('案例不存在或已处理', status=404, machine='NOT_FOUND')
    ch = IdentityChallengeModel.query.filter_by(
        purpose=RECOVERY_PURPOSE, consumed_at=None,
        case_id=_case_ref(case)).order_by(
        IdentityChallengeModel.created_at.desc()).first()
    now = datetime.now()
    if ch is None or ch.expires_at <= now or ch.payload_digest != _payload_digest(case):
        return False
    given = challenge_digest(
        f'{RECOVERY_PURPOSE}:{case.id}:{case.contact_email}:{(code or "").strip()}')
    if constant_time_eq(given, ch.secret_digest):
        ch.consumed_at = now
        case.contact_verified_at = now
        case.status = 'submitted'
        # 特权账号（超管/非 standard）恢复：冷静期 72h + 双人复核（规格 7.4）
        target = UserModel.query.filter_by(email=case.target_email).first()
        case.require_two = bool(target is not None and (
            target.is_admin() or target.account_kind != 'standard'))
        events.record_event(
            'identity.recovery.submit', actor_user_id=None,
            target_ids=None,
            after=None,
            evidence_refs={'note': f'case#{case.id} kind={case.kind}'},
            reason='恢复案例提交（联系邮箱已验证）')
        return True
    ch.attempts += 1
    if ch.attempts >= MAX_ATTEMPTS:
        ch.consumed_at = now
    return False


def decide_case(case, reviewer, *, decision, note=''):
    """管理端决策：批准→冷静期（24h/特权 72h）；驳回留因。

    特权案例（require_two）需两名不同审核人先后批准才进入冷静期——
    第一人批准记录在 decision_note，状态保持 submitted 等第二人。
    """
    if case.status != 'submitted':
        raise AuthRejected('案例不在待审状态', status=409, machine='BAD_STATE')
    note = (note or '').strip()
    if decision == 'rejected' and not note:
        raise AuthRejected('请填写驳回原因', status=400, machine='BAD_REQUEST')
    hours = COOLDOWN_PRIVILEGED_HOURS if case.require_two else COOLDOWN_HOURS
    if decision == 'approved' and case.require_two:
        first = (case.decision_note or '')
        if not first.startswith('approved#'):
            case.decision_note = f'approved#{reviewer.id}:{note[:80]}'
            events.record_event(
                'identity.recovery.first_approval', actor_user_id=reviewer.id,
                evidence_refs={'note': f'case#{case.id}'},
                reason='特权恢复第一批准（待第二复核）')
            return case  # 仍 submitted，等第二人
        if first.startswith(f'approved#{reviewer.id}:'):
            raise AuthRejected('特权恢复需两名不同审核人复核',
                               status=409, machine='SECOND_REVIEWER_REQUIRED')
    if decision == 'approved':
        case.status = 'cooldown'
        case.cooldown_until = datetime.now() + timedelta(hours=hours)
        case.decided_by = reviewer.id
        case.decided_at = datetime.now()
        case.decision_note = (case.decision_note or '') + f'|approved {note[:80]}'
    else:
        case.status = 'rejected'
        case.decided_by = reviewer.id
        case.decided_at = datetime.now()
        case.decision_note = note[:255]
    events.record_event(
        'identity.recovery.decide', actor_user_id=reviewer.id,
        after=None,
        evidence_refs={'note': f'case#{case.id} {decision}'},
        reason='恢复案例决策')
    return case


def complete_case(case, operator):
    """冷静期届满后标记执行完毕（执行动作本身走既有运维通道并另行留痕；
    完成时若目标账号存在，建议在执行侧 bump 撤旧会话——规格 7.4）。"""
    if case.status != 'cooldown':
        raise AuthRejected('案例不在冷静期', status=409, machine='BAD_STATE')
    if case.cooldown_until and case.cooldown_until > datetime.now():
        raise AuthRejected(f'冷静期未满（至 {case.cooldown_until:%m-%d %H:%M}）',
                           status=409, machine='COOLDOWN_ACTIVE')
    case.status = 'done'
    events.record_event(
        'identity.recovery.done', actor_user_id=operator.id,
        evidence_refs={'note': f'case#{case.id}'},
        reason='恢复执行完毕（凭据重置与撤会话在执行通道完成）')
    return case

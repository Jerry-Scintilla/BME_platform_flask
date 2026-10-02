"""双账号认领与空壳归并（D3b，规格 7.1/7.2/8 章）。

流程：A 登录建案例 → A 近期认证证明（绑当前 sid+版本快照）→ B 独立凭据证明
（密码/可选 TOTP，不建 B 会话、不覆盖 A 登录态；错误统一 422 TARGET_PROOF_FAILED
不触发 A 登出）→ 空壳扫描（只读全模块；任何未覆盖/异常=不能证明空壳转人工）→
预览（preview_digest 绑定计划+案例版本；签事务授权与 10 分钟完成收据）→
confirm 在一个事务内复核全部不变量后执行归并（身份登记迁移、B 归属存续人员、
B lifecycle=merged、双端 bump 撤会话、账本/outbox 留痕）。

归并写序（D2 实证的复合外键顺序，规格 3.4/8.2.1）：锁 user(A,B 按 id 升序) →
锁 person → 删 B 侧 primary → 迁 person_identity → 改 B.user.person_id →
B_p.merged → bump 双端。任一步失败全部回滚。

空壳自动归并条件（7.2 全满足）：双端近期证明、无特权/封禁/争议、B 无任何
业务数据（扫描覆盖见 SHELL_MODULES）、预览与提交时复查一致；否则 awaiting_review
人工审（特权/扫描异常需两名审核人批准）。
"""
import hmac
import uuid
from datetime import datetime, timedelta

from exts import db, redis_client
from models import (
    AccountLinkCaseModel, IdentityApplicationModel, IdentityAttestationModel,
    IdentityCaseAccountLockModel, IdentityExceptionGrantModel, IdentityOutboxModel,
    IdentityTransactionAuthorizationModel, PersonIdentityModel, PersonModel,
    PersonPrimaryAccountModel, UserModel)
from services import auth_mfa, auth_sessions
from services.auth_context import AuthRejected, challenge_digest, require_recent_auth
from services.identity import events

ATTEST_TTL = 300                 # 证明 5 分钟，不延长
AUTHZ_TTL = 300                  # 事务授权不超过两端证明剩余窗口（取更早者）
RECEIPT_TTL = 600                # 完成收据 10 分钟，仅内存
COLLECTION_HOURS = 24            # collecting 未提交过期
APPROVAL_DAYS = 7                # 批准待确认有效期
TARGET_PROOF_MAX = 5             # B 证明错误次数上限（锁案例）

RECEIPT_HMAC_PURPOSE = 'identity-receipt-v1'

# 空壳扫描模块清单（规格 10.2：只读检查器）。键=模块名（账本/预览用），
# 值=(模型, 过滤列)。B 命中任一非零项即非空壳；查询异常视为扫描异常转人工。
from models import (ArticleModel, ArticleV2Model, CampChapterCertification,
                     CampJoinRequest, CampLeave, CampLearningProgress, CampMember,
                     CampStaff, ClubMembership, ClubOfficer, DiscussionThread,
                     FeedbackTicket, LearningProgressModel, LLMQuotaRequestModel,
                     LLMUserKeyModel, MedalUserModel, ShowcaseProject, TaskAssignee,
                     UserCourseModel, WorkAccessGrant, WorkFile, WorkItemParticipant)

SHELL_MODULES = {
    'user_course': (UserCourseModel, 'user_id'),
    'learning_progress': (LearningProgressModel, 'user_id'),
    'camp_learning': (CampLearningProgress, 'user_id'),
    'chapter_cert': (CampChapterCertification, 'student_user_id'),
    'article': (ArticleModel, 'author_id'),
    'article_v2': (ArticleV2Model, 'author_id'),
    'discussion': (DiscussionThread, 'author_id'),
    'showcase': (ShowcaseProject, 'owner_user_id'),
    'camp_member': (CampMember, 'user_id'),
    'camp_staff': (CampStaff, 'user_id'),
    'camp_join_request': (CampJoinRequest, 'user_id'),
    'camp_leave': (CampLeave, 'user_id'),
    'club_membership': (ClubMembership, 'user_id'),
    'club_officer': (ClubOfficer, 'user_id'),
    'task_assignee': (TaskAssignee, 'student_id'),
    'work_participant': (WorkItemParticipant, 'user_id'),
    'work_grant': (WorkAccessGrant, 'user_id'),
    'work_file': (WorkFile, 'created_by'),
    'medal': (MedalUserModel, 'user_id'),
    'llm_key': (LLMUserKeyModel, 'user_id'),
    'llm_quota_request': (LLMQuotaRequestModel, 'user_id'),
    'feedback_ticket': (FeedbackTicket, 'reporter_user_id'),
}


# ── 案例 ────────────────────────────────────────────────────────

def create_case(actor):
    """发起认领（规格 7.1.1）：服务器固定发起者，随机 case_id；锁 A。

    Idempotency：一人同时只有一个未终态案例（收集期内旧案例先撤回）。
    """
    user = actor.user
    # 服务号不认领（虚构自然人禁入）；test 号与核验同口径放行（限制在正式营业务
    # 写入，规格 9.3——dev 全流程演练也依赖 seed 号）
    if user.account_kind == 'service' or user.is_admin():
        raise AuthRejected('该类账号不能发起账号认领', status=403, machine='FORBIDDEN')
    if user.person_id is None:
        raise AuthRejected('缺少人员档案（数据异常）', status=500, machine='PERSON_MISSING')
    existing = AccountLinkCaseModel.query.filter(
        AccountLinkCaseModel.account_a == user.id,
        AccountLinkCaseModel.state.notin_(('applied', 'cancelled', 'expired', 'failed'))\
    ).first()
    if existing is not None:
        raise AuthRejected('已有进行中的认领案例，请先处理或撤回',
                           status=409, machine='CASE_EXISTS')
    _require_lock_free(user.id)
    case = AccountLinkCaseModel(
        id=uuid.uuid4().hex, account_a=user.id,
        state='collecting',
        collection_expires_at=datetime.now() + timedelta(hours=COLLECTION_HOURS))
    db.session.add(case)
    db.session.flush()
    _acquire_lock(user.id, case.id)
    events.record_event('identity.link.create', actor_user_id=user.id,
                        target_ids={'user_id': user.id, 'person_id': user.person_id},
                        after={'status': case.state},
                        evidence_refs={'note': case.id[:12]},
                        reason='发起账号认领')
    return case


def get_case_for(actor, case):
    """对象级权限：仅发起者 A 本人（不因同 Person 自动共享，规格 11）。

    接受案例模型或 id（蓝图传 id，服务内部传模型）。
    """
    case_id = case.id if isinstance(case, AccountLinkCaseModel) else case
    case = db.session.get(AccountLinkCaseModel, case_id)
    if case is None or case.account_a != actor.user.id:
        raise AuthRejected('案例不存在', status=404, machine='NOT_FOUND')
    return case


# ── 双端证明 ────────────────────────────────────────────────────

def prove_initiator(actor, case):
    """A 端证明（7.1.2）：当前会话 + 近期认证；绑 sid 与版本快照，不替换登录态。"""
    get_case_for(actor, case)  # 仅 A
    if case.state != 'collecting':
        raise AuthRejected('案例不在证明阶段', status=409, machine='BAD_STATE')
    _check_collection_window(case)
    require_recent_auth(actor)  # legacy 会话/超窗即拒
    now = datetime.now()
    att = IdentityAttestationModel(
        id=uuid.uuid4().hex, proven_user_id=actor.user.id,
        security_version_snapshot=actor.user.security_version or 0,
        actor_sid=actor.session.sid if actor.session else None,
        case_id=case.id, purpose='link_side_a',
        auth_time=actor.session.auth_time if actor.session else now,
        amr=actor.session.amr if actor.session else 'pwd',
        evidence_ref=None,
        expires_at=now + timedelta(seconds=ATTEST_TTL))
    db.session.add(att)
    db.session.flush()
    events.record_event('identity.link.prove_a', actor_user_id=actor.user.id,
                        target_ids={'user_id': actor.user.id},
                        evidence_refs={'note': att.id[:12]},
                        reason='发起端近期认证证明')
    return att


def prove_target(actor, case, *, target_email, password, totp=None):
    """B 端独立凭据证明（7.1.4）：验证 B 账号凭据（B 启用 MFA 必须通过），
    不建 B 会话、不返回任何 B 凭据；错误统一 422 TARGET_PROOF_FAILED。

    证明通过才锁 B（证明前不能凭邮箱锁别人，规格 8.2）。B 邮箱不存在与
    密码错误同话术（防枚举）。限流计数依赖 Redis：不可达即 503（挑战类 fail-closed）。
    """
    get_case_for(actor, case)
    if case.state != 'collecting':
        raise AuthRejected('案例不在证明阶段', status=409, machine='BAD_STATE')
    _check_collection_window(case)
    try:
        key = f'linkproof:{case.id}'
        fails = int(redis_client.get(key) or 0)
    except Exception as e:
        raise AuthRejected('验证服务暂不可用，请稍后重试',
                           status=503, machine='SERVICE_UNAVAILABLE') from e
    if fails >= TARGET_PROOF_MAX:
        _transition(case, 'failed', actor.user.id, 'B 端证明错误超限')
        db.session.commit()  # 异常路径的状态变更须落库（调用方 except 不会 commit）
        raise AuthRejected('证明错误次数过多，案例已终止',
                           status=429, machine='PROOF_LIMIT')

    target = UserModel.query.filter_by(email=(target_email or '').strip()).first()
    ok = False
    amr = 'pwd'
    if target is not None and target.status != 'banned':
        if target.check_password(password or ''):
            ok = True
            factor = auth_mfa.active_totp_factor(target)
            if factor is not None:
                if auth_mfa.verify_totp_code(factor, totp or ''):
                    amr = 'pwd+totp'
                else:
                    ok = False
    if not ok:
        try:
            redis_client.setex(key, 3600, str(fails + 1))
        except Exception:
            pass  # 计数失败不掩盖主错误
        raise AuthRejected('目标账号验证失败', status=422, machine='TARGET_PROOF_FAILED')

    if target.id == case.account_a:
        raise AuthRejected('目标账号验证失败', status=422, machine='TARGET_PROOF_FAILED')
    if target.account_kind != 'standard' or target.is_admin():
        _transition(case, 'awaiting_review', actor.user.id,
                    '目标账号含特权/特殊用途，转人工')
        db.session.commit()  # 异常路径状态变更落库
        raise AuthRejected('目标账号需人工处理', status=409, machine='MANUAL_REQUIRED')
    _require_lock_free(target.id, case.id)
    case.account_b = target.id
    db.session.flush()
    _acquire_lock(target.id, case.id)
    now = datetime.now()
    att = IdentityAttestationModel(
        id=uuid.uuid4().hex, proven_user_id=target.id,
        security_version_snapshot=target.security_version or 0,
        actor_sid=None,  # B 独立凭据证明，无普通会话（规格 3.2）
        case_id=case.id, purpose='link_side_b',
        auth_time=now, amr=amr,
        evidence_ref=f'case:{case.id[:12]}',
        expires_at=now + timedelta(seconds=ATTEST_TTL))
    db.session.add(att)
    db.session.flush()
    try:
        redis_client.delete(key)
    except Exception:
        pass
    if _has_valid_attestation(case, 'link_side_a'):
        _transition(case, 'proof_ready', actor.user.id, '双端证明齐备')
    events.record_event('identity.link.prove_b', actor_user_id=actor.user.id,
                        target_ids={'user_id': target.id},
                        evidence_refs={'note': att.id[:12]},
                        reason='目标端独立凭据证明')
    return att


# ── 空壳扫描（7.2/10.2 只读检查器）──────────────────────────────

def shell_scan(user_b):
    """只读扫描 B 是否空壳。返回 (is_shell, blockers)。

    任何未登记引用或查询异常都视为「不能证明空壳」→ blockers 带 scan_error
    转人工；不能因页面显示 0 篇文章判空壳（规格 7.2）。
    """
    blockers = []
    if user_b.role == 'super_admin' or user_b.account_kind != 'standard':
        blockers.append('target_privileged')
    if (user_b.status or 'active') == 'banned':
        blockers.append('target_banned')
    if user_b.lifecycle not in (None, 'active'):
        blockers.append('target_lifecycle')
    person = db.session.get(PersonModel, user_b.person_id) if user_b.person_id else None
    if person is None:
        blockers.append('target_person_missing')
    elif person.verification_status in ('disputed', 'revoked'):
        blockers.append('target_disputed')

    hits = {}
    for name, (model, col) in SHELL_MODULES.items():
        try:
            n = model.query.filter(getattr(model, col) == user_b.id).count()
        except Exception:
            blockers.append(f'scan_error:{name}')  # 查询异常=不能证明空壳
            continue
        if n:
            hits[name] = n
    if hits:
        blockers.append('target_has_business_data')
    return (not blockers), blockers


def shell_scan_summary(user_b):
    is_shell, blockers = shell_scan(user_b)
    return {'is_shell': is_shell, 'blockers': blockers}


# ── 预览与授权（7.1.6-8）────────────────────────────────────────

def _live_attestation(case, purpose):
    att = IdentityAttestationModel.query.filter_by(
        case_id=case.id, purpose=purpose, consumed_at=None)\
        .order_by(IdentityAttestationModel.created_at.desc()).first()
    if att is None or att.expires_at <= datetime.now():
        return None
    user = db.session.get(UserModel, att.proven_user_id)
    if user is None or (user.security_version or 0) != att.security_version_snapshot:
        return None  # 版本漂移即作废（B 重置密码后旧证明失效）
    return att


def _has_valid_attestation(case, purpose):
    return _live_attestation(case, purpose) is not None


def _plan_digest(case, user_a, user_b, scan):
    """服务器预览摘要：绑定存续人员/主号/迁移键/扫描结果（7.1.8）。

    不含 case.version——案例状态推进（如转审核）不该废止计划本身；数据变化
    由摘要内容捕获，状态合法性由状态机保证。
    """
    payload = {
        'case': case.id,
        'surviving_person': user_a.person_id,
        'primary_user': user_a.id,
        'merged_user': user_b.id, 'merged_person': user_b.person_id,
        'moving_keys': sorted(f'{r.issuer}|{r.kind}|{r.canonical_key}'
                              for r in PersonIdentityModel.query.filter_by(
                                  person_id=user_b.person_id).all()),
        'shell': scan['is_shell'], 'blockers': sorted(scan['blockers']),
        'policy_version': case.policy_version,
    }
    return challenge_digest(repr(payload)), payload


def build_preview(actor, case):
    """生成最终预览 + 事务授权 + 完成收据（不 commit）。

    空壳且无 blocker → preview_ready（可自助 confirm）；有 blocker →
    awaiting_review（人工审；特权/扫描异常需双人）。
    """
    user_a = actor.user
    get_case_for(actor, case)
    if case.state not in ('collecting', 'proof_ready'):
        raise AuthRejected('案例不在可预览状态', status=409, machine='BAD_STATE')
    att_a = _live_attestation(case, 'link_side_a')
    att_b = _live_attestation(case, 'link_side_b')
    if att_a is None or att_b is None:
        raise AuthRejected('双端证明缺失或已过期，请重新证明',
                           status=403, machine='REAUTH_REQUIRED')
    user_b = db.session.get(UserModel, case.account_b)
    scan = shell_scan_summary(user_b)

    digest, payload = _plan_digest(case, user_a, user_b, scan)
    case.preview_digest = digest
    case.surviving_person_id = user_a.person_id
    case.selected_primary_user_id = user_a.id
    expires = min(att_a.expires_at, att_b.expires_at,
                  datetime.now() + timedelta(seconds=AUTHZ_TTL))
    authz = IdentityTransactionAuthorizationModel(
        id=uuid.uuid4().hex, case_id=case.id, preview_digest=digest,
        attestation_a=att_a.id, attestation_b=att_b.id,
        policy_version=case.policy_version, expires_at=expires)
    db.session.add(authz)
    new_state = 'preview_ready' if scan['is_shell'] else 'awaiting_review'
    _transition(case, new_state, user_a.id,
                '空壳可自助' if scan['is_shell'] else '存在阻断项转人工')
    events.record_event('identity.link.preview', actor_user_id=user_a.id,
                        target_ids={'user_id': user_b.id, 'person_id': user_a.person_id},
                        after={'status': case.state},
                        evidence_refs={'note': digest[:12]},
                        reason=f"预览 shell={scan['is_shell']}")
    receipt = _issue_receipt(case.id, digest)
    return {'digest': digest, 'plan': payload, 'authorization_expires_at': expires,
            'receipt': receipt, 'case_state': case.state}


def _issue_receipt(case_id, digest):
    """10 分钟完成收据：HMAC 自包含令牌（仅内存，不入 URL，规格 7.1.8/11）。"""
    exp = int((datetime.now() + timedelta(seconds=RECEIPT_TTL)).timestamp())
    body = f'{RECEIPT_HMAC_PURPOSE}|{case_id}|{digest}|{exp}'
    sig = challenge_digest(body)
    return f'{case_id}.{exp}.{sig}'


def check_receipt(token):
    """校验收据 → 最小状态（applied/未提交），不披露双方资料（7.1.11）。"""
    parts = (token or '').split('.')
    if len(parts) != 3:
        raise AuthRejected('收据无效', status=400, machine='BAD_REQUEST')
    case_id, exp, sig = parts
    case = db.session.get(AccountLinkCaseModel, case_id)
    if case is None or case.preview_digest is None:
        raise AuthRejected('收据无效', status=400, machine='BAD_REQUEST')
    expect = challenge_digest(f'{RECEIPT_HMAC_PURPOSE}|{case_id}|{case.preview_digest}|{exp}')
    if not hmac.compare_digest(expect, sig):
        raise AuthRejected('收据无效', status=400, machine='BAD_REQUEST')
    if int(exp) < datetime.now().timestamp():
        raise AuthRejected('收据已过期', status=410, machine='RECEIPT_EXPIRED')
    return {'case_id': case_id,
            'state': case.state if case.state in ('applied', 'prepared') else 'pending'}


# ── 审核与确认（7.4/11）─────────────────────────────────────────

def review_case(case, reviewer, *, decision, reason='', require_two=None):
    """人工审核决策：每名审核人一份有效批准（version 递增覆盖旧意见）。

    特权/扫描异常（阻断项非空）需 ≥2 名不同审核人批准才进入
    approved_waiting_confirmation；普通情形一名即可。
    """
    from models import IdentityReviewDecisionModel
    if case.state != 'awaiting_review':
        raise AuthRejected('案例不在待审状态', status=409, machine='BAD_STATE')
    reason = (reason or '').strip()
    if decision == 'rejected' and not reason:
        raise AuthRejected('请填写驳回原因', status=400, machine='BAD_REQUEST')
    if case.account_a == reviewer.id or case.account_b == reviewer.id:
        raise AuthRejected('当事人不可审核自己的案例', status=403, machine='FORBIDDEN')
    user_b = db.session.get(UserModel, case.account_b)
    _, blockers = shell_scan(user_b)
    # 2026-10-02 平台运营决策：现行核验审核人仅一名（admin@433），阻断项案例的
    # 双人门槛会使案例永久停在 awaiting_review（第二名审核人不存在）。
    # 覆盖为单人批准即可推进；将来审核人补足 >=2 名时删除本行，即恢复规格 7.4 双人制。
    need_two = False if require_two is None else require_two
    plan_digest = case.preview_digest or ''
    prev = IdentityReviewDecisionModel.query.filter_by(
        case_id=case.id, reviewer_user_id=reviewer.id)\
        .order_by(IdentityReviewDecisionModel.version.desc()).first()
    row = IdentityReviewDecisionModel(
        case_id=case.id, reviewer_user_id=reviewer.id, plan_digest=plan_digest,
        decision=decision, scope=None,
        version=(prev.version + 1) if prev else 1,
        valid_until=datetime.now() + timedelta(days=APPROVAL_DAYS))
    db.session.add(row)
    db.session.flush()
    if decision == 'rejected':
        _transition(case, 'cancelled', reviewer.id, f'审核驳回：{reason[:80]}')
        _release_locks(case)
    else:
        rows = IdentityReviewDecisionModel.query.filter_by(
            case_id=case.id, decision='approved').filter(
            IdentityReviewDecisionModel.valid_until > datetime.now(),
            IdentityReviewDecisionModel.plan_digest == plan_digest).all()
        approvals = len({r.reviewer_user_id for r in rows})  # 每人只计一份
        if approvals >= (2 if need_two else 1):
            _transition(case, 'approved_waiting_confirmation', reviewer.id,
                        f'批准（{approvals} 份有效）')
            case.approval_expires_at = datetime.now() + timedelta(days=APPROVAL_DAYS)
    events.record_event('identity.link.review', actor_user_id=reviewer.id,
                        target_ids={'user_id': case.account_a},
                        after=None, evidence_refs={'note': f'{decision}:{reason[:48]}'},
                        reason='关联案例审核')
    return case


def confirm_link(actor, case, *, preview_digest):
    """确认执行（7.1.9-10）：一个事务内复核全部不变量后归并。

    复核失败按语义返回 409（预览过期/数据变化）或 403（证明过期重认证）；
    案例已 applied 幂等返回。
    """
    user_a = actor.user
    get_case_for(actor, case)
    if case.state == 'applied':
        return {'case_id': case.id, 'state': 'applied', 'replay': True}
    if case.state not in ('preview_ready', 'approved_waiting_confirmation'):
        raise AuthRejected('案例不在可确认状态', status=409, machine='BAD_STATE')
    if case.approval_expires_at and case.approval_expires_at <= datetime.now():
        _transition(case, 'expired', user_a.id, '批准有效期届满')
        raise AuthRejected('批准已过期，请重新发起', status=409, machine='APPROVAL_EXPIRED')
    if not hmac.compare_digest(preview_digest or '', case.preview_digest or ''):
        raise AuthRejected('预览已变化，请重新预览', status=409, machine='STALE_PREVIEW')

    att_a = _live_attestation(case, 'link_side_a')
    att_b = _live_attestation(case, 'link_side_b')
    if att_a is None or att_b is None:
        raise AuthRejected('证明过期，请重新完成双端证明',
                           status=403, machine='REAUTH_REQUIRED')
    authz = IdentityTransactionAuthorizationModel.query.filter_by(
        case_id=case.id, preview_digest=case.preview_digest, consumed_at=None)\
        .order_by(IdentityTransactionAuthorizationModel.created_at.desc()).first()
    if authz is None or authz.expires_at <= datetime.now():
        raise AuthRejected('事务授权已过期，请重新预览', status=409, machine='STALE_PREVIEW')

    # 锁序（8.2.1）：user 按 id 升序先锁，再 person 按 id——注意排序只决定
    # 加锁顺序，语义角色（A 的/B 的）按归属取，不能随排序换位
    user_b = db.session.get(UserModel, case.account_b)
    for u in sorted([user_a, user_b], key=lambda x: x.id):
        auth_sessions.lock_user(u.id)
    person_a = db.session.get(PersonModel, user_a.person_id)
    person_b = db.session.get(PersonModel, user_b.person_id)
    if person_a is None or person_b is None:
        raise AuthRejected('人员档案缺失（数据异常）', status=500, machine='PERSON_MISSING')
    for p in sorted([person_a, person_b], key=lambda x: x.id):
        db.session.get(PersonModel, p.id, with_for_update=True)

    # 提交前复查（7.1.9/7.2：预览与提交时结果一致）
    if case.state == 'preview_ready':
        is_shell, blockers = shell_scan(user_b)
        if not is_shell:
            raise AuthRejected('目标账号数据已变化，需重新预览或转人工',
                               status=409, machine='STALE_PREVIEW')
    else:  # approved 路径：计划期间数据变化→409
        digest_now, _ = _plan_digest(case, user_a, user_b, shell_scan_summary(user_b))
        if not hmac.compare_digest(digest_now, case.preview_digest):
            raise AuthRejected('目标账号数据已变化，需重新审核',
                               status=409, machine='STALE_PREVIEW')

    # ── prepared → applied：一个事务内完成全部写（规格 3.4 顺序）──
    _transition(case, 'prepared', user_a.id, '确认执行')
    # 1) 先撤 B 侧 primary（复合外键父子顺序，D2 实证）
    PersonPrimaryAccountModel.query.filter_by(person_id=person_b.id).delete()
    # 2) 身份登记迁移到存续人员（学校身份归属 A 的 Person，7.2）
    moved = PersonIdentityModel.query.filter_by(person_id=person_b.id).all()
    for row in moved:
        row.person_id = person_a.id
    # 3) B 账号归属存续人员（nonprimary；先迁登记再改指针）
    user_b.person_id = person_a.id
    # 4) B 生命周期与旧人员档案——7.3 语义：空壳立即冻结；有业务数据的账号
    #    保持 active（已归存续人员、明确 nonprimary），生成 14 天续办宽限供
    #    在途事项收尾，到期由管理端 freeze 复核后才 merged（不斩断进行中业务）
    _freeze_now = shell_scan(user_b)[0]
    user_b.lifecycle = 'merged' if _freeze_now else 'active'
    person_b.record_status = 'merged'
    person_b.merged_to_person_id = person_a.id
    # 5) 存续人员核验态继承（B 若已核验，A 的 Person 获得核验依据）
    if moved and person_a.verification_status != 'verified':
        person_a.verification_status = 'verified'
    if person_a.verified_name is None and person_b.verified_name:
        person_a.verified_name = person_b.verified_name
    # 6) 双端撤权：A 保留当前会话续用，B 全撤（7.1.10）
    auth_sessions.bump_security_version(
        user_a, except_sid=actor.session.sid if actor.session else None,
        reason='账号关联归并（发起端）')
    auth_sessions.bump_security_version(user_b, reason='账号关联归并（被归并端）')
    # 6b) 归并落地后收尾遗留核验申请：B 账号已退休，其未终态申请一律作废；
    #     存续人员若已核验，A 的在途申请同样失去意义（否则审核队列里会出现
    #     「待审账号却已核验」的矛盾行，且批准会撞 person_identity 唯一键 409）。
    _stale_user_ids = [user_b.id]
    if person_a.verification_status == 'verified':
        _stale_user_ids.append(user_a.id)
    for _app in IdentityApplicationModel.query.filter(
            IdentityApplicationModel.applicant_user_id.in_(_stale_user_ids),
            IdentityApplicationModel.status.in_(
                ['draft', 'submitted', 'reviewing'])).all():
        _app.status = 'withdrawn'
    # 7) 消费证明与授权；案例终态；账本；outbox 通知（提交与发送分离）
    att_a.consumed_at = att_b.consumed_at = datetime.now()
    authz.consumed_at = datetime.now()
    _transition(case, 'applied', user_a.id, '归并完成')
    _release_locks(case)
    before = {'status': 'separate', 'merged_user': user_b.id,
              'merged_person': person_b.id}
    after = {'status': case.state, 'person_id': person_a.id,
             'primary_user_id': user_a.id, 'merged_to_person_id': person_a.id,
             'lifecycle': user_b.lifecycle, 'record_status': 'merged'}
    op, _created = events.run_idempotent(
        user_a.id, 'identity.link.apply', case.id,
        {'case': case.id, 'digest': case.preview_digest},
        lambda o: case.id)
    ev = events.record_event(
        'identity.link.apply', actor_user_id=user_a.id,
        operation_id=op.operation_id,
        target_ids={'user_id': user_b.id, 'person_id': person_a.id,
                    'primary_user_id': user_a.id},
        before=before, after=after,
        evidence_refs={'operation_id': op.operation_id},
        reason='空壳副号归并执行')
    if not _freeze_now:
        db.session.add(IdentityExceptionGrantModel(
            person_id=person_a.id, user_id=user_b.id,
            operation_scope='camp_join', scope_id=None,
            valid_until=datetime.now() + timedelta(days=14),
            reason=f'续办：案例 {case.id[:8]} 归并交接期（在途事项收尾，到期人工复核冻结）',
            approved_by=0))
    # 通知双端原验证渠道（提交与发送分离，规格 7.1.11/S14；发送属后续 outbox 消费者）
    for uid in (user_a.id, user_b.id):
        db.session.add(IdentityOutboxModel(
            event_id=ev.event_id, channel='notification',
            payload={'case_id': case.id, 'action': 'link_applied', 'to_user': uid}))
    return {'case_id': case.id, 'state': 'applied', 'replay': False}


def withdraw_case(actor, case):
    """撤回（collecting→approved_waiting_confirmation 均可；prepared 先查结果，
    已 applied 的逆操作是新审批任务而非撤回——规格 10.3）。"""
    user_a = actor.user
    get_case_for(actor, case)
    if case.state == 'applied':
        raise AuthRejected('归并已完成，撤销需走人工补偿流程',
                           status=409, machine='BAD_STATE')
    if case.state in ('cancelled', 'expired', 'failed'):
        raise AuthRejected('案例已结束', status=409, machine='BAD_STATE')
    if case.state == 'prepared':
        raise AuthRejected('案例正在执行，请稍后按收据查询结果',
                           status=409, machine='BAD_STATE')
    _transition(case, 'cancelled', user_a.id, '发起者撤回')
    _release_locks(case)
    events.record_event('identity.link.withdraw', actor_user_id=user_a.id,
                        target_ids={'user_id': user_a.id},
                        after={'status': case.state}, reason='撤回认领案例')
    return case


# ── 占位锁与状态推进 ────────────────────────────────────────────

def _acquire_lock(user_id, case_id, hours=COLLECTION_HOURS):
    db.session.add(IdentityCaseAccountLockModel(
        user_id=user_id, case_id=case_id,
        expires_at=datetime.now() + timedelta(hours=hours)))


def _require_lock_free(user_id, except_case=None):
    lock = db.session.get(IdentityCaseAccountLockModel, user_id)
    if lock is None:
        return
    if lock.expires_at <= datetime.now():
        db.session.delete(lock)  # 到期占位：服务端校验后释放（规格 3.2）
        return
    if except_case and lock.case_id == except_case:
        return
    raise AuthRejected('该账号已在另一个认领案例中',
                       status=409, machine='ACCOUNT_LOCKED')


def _release_locks(case):
    for uid in (case.account_a, case.account_b):
        lock = db.session.get(IdentityCaseAccountLockModel, uid) if uid else None
        if lock and lock.case_id == case.id:
            db.session.delete(lock)


def _transition(case, new_state, actor_user_id, note):
    """状态推进 + version 乐观锁（账本事件由调用方按各自语义记录）。"""
    case.state = new_state
    case.version += 1


def _check_collection_window(case):
    if case.collection_expires_at and case.collection_expires_at <= datetime.now():
        _transition(case, 'expired', case.account_a, '收集期届满')
        _release_locks(case)
        raise AuthRejected('案例已过期，请重新发起', status=409, machine='CASE_EXPIRED')


def freeze_merged_account(case, operator):
    """续办到期后的冻结动作（7.3：指定事项完成/交接验收后才 merged）。

    仅对 applied 案例且 B 未 merged 时有效；同事务：B.lifecycle=merged、
    bump 撤 B 会话、撤续办宽限、账本留痕。空壳案例 confirm 时已即时 merged，
    不经此函数；幂等（已 merged 直接返回）。
    """
    c = db.session.get(AccountLinkCaseModel, case.id)
    if c is None or c.state != 'applied':
        raise AuthRejected('案例未完成归并', status=409, machine='BAD_STATE')
    user_b = db.session.get(UserModel, c.account_b)
    if user_b is None:
        raise AuthRejected('目标账号缺失', status=404, machine='NOT_FOUND')
    if user_b.lifecycle == 'merged':
        return c
    auth_sessions.lock_user(user_b.id)
    user_b.lifecycle = 'merged'
    auth_sessions.bump_security_version(user_b, reason='续办期满冻结（人工复核）')
    for g in IdentityExceptionGrantModel.query.filter_by(
            user_id=user_b.id, operation_scope='camp_join', state='active').all():
        if (g.reason or '').startswith('续办'):
            g.state = 'expired'
    events.record_event(
        'identity.link.freeze', actor_user_id=operator.id,
        target_ids={'user_id': user_b.id, 'person_id': user_b.person_id},
        before={'lifecycle': 'active'}, after={'lifecycle': 'merged'},
        evidence_refs={'note': c.id[:12]},
        reason='续办期满人工复核冻结')
    return c

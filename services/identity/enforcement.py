"""业务写入口的人员判定（D5，规格 9.1/9.2/9.4）。

enforcement 模式（规格 13.1，IDENTITY_ENFORCEMENT_MODE）：
  off      不评估（锚点登记仍写——锚点是数据不是行为）
  shadow   只记账本事件（identity.enforcement.shadow），不拦——默认
  enforce  违规拒绝（409 IDENTITY_RULE）
判定与业务对象权限正交：身份门槛不替代原对象权限检查（规格 9.1）。

第一轮规则：
  R1 同一 Person 同营/同单元唯一（参与锚点 PK 裁决，回填报告冲突）
  R2 新参与指向当前主参与号；续办/宽限经 identity_exception_grant（实时查有效期）
  R3 防授权复活：merged 账号不产生新自动工作区授权；veto 随人员走
      （person_workspace_restriction，换主号不得绕过——规格 9.4）

核验要求（R0：正式参与需人员已核验或有效宽限）按规格 13.3 属强制阶段——
第一轮 shadow 只记录不拦，enforce 模式下同样只记（avoid 拦死存量），
30 天补全期结束后由运营决策打开（记在演进备忘）。
"""
from datetime import datetime

import config
from exts import db
from models import (CampPersonParticipationModel, IdentityExceptionGrantModel,
                    PersonModel, PersonPrimaryAccountModel,
                    PersonWorkspaceRestrictionModel, UserModel)
from services.identity import events

MODES = ('off', 'shadow', 'enforce')


def mode():
    return config.IDENTITY_ENFORCEMENT_MODE


def _is_enforce():
    return mode() == 'enforce'


# ── 宽限 ────────────────────────────────────────────────────────

def exception_valid(user, operation_scope, scope_id=None):
    """宽限/续办例外：实时查有效期与状态（不依赖清理任务，规格 4 状态机）。"""
    now = datetime.now()
    q = IdentityExceptionGrantModel.query.filter_by(
        user_id=user.id, operation_scope=operation_scope, state='active'
    ).filter(IdentityExceptionGrantModel.valid_until > now)
    if scope_id is not None:
        q = q.filter(db.or_(IdentityExceptionGrantModel.scope_id == scope_id,
                            IdentityExceptionGrantModel.scope_id.is_(None)))
    return q.first() is not None


# ── R1/R2：营期参与判定与锚点 ───────────────────────────────────

def check_camp_join(sid, user):
    """营期参与判定（建 CampMember 前调用）。返回 (allowed, reason)。

    shadow 模式恒 (True, …)（违规只记账本）；enforce 模式违规 (False, …)。
    """
    violations = []
    person = db.session.get(PersonModel, user.person_id) if user.person_id else None
    if person is None:
        violations.append(('person_missing', '目标账号缺少人员档案'))
    else:
        primary = db.session.get(PersonPrimaryAccountModel, person.id)
        if primary is None or primary.user_id != user.id:
            if not exception_valid(user, 'camp_join', sid):
                violations.append((
                    'nonprimary',
                    '该账号不是其人员档案的正式参与号；请用主账号报名或申请续办宽限'))
        dup = CampPersonParticipationModel.query.get((sid, person.id))
        if dup is not None and dup.user_id != user.id:
            if not exception_valid(user, 'camp_join', sid):
                violations.append((
                    'duplicate_person',
                    '同一人员已在本营期以另一账号参与；如需变更请走负责人裁定'))
    if not violations:
        return True, None
    _record('camp_join', user, sid, violations)
    if _is_enforce():
        return False, violations[0][1]
    return True, None


def register_camp_participation(member):
    """写营期参与锚点（_assign_member 成功路径调用；不 commit）。

    PK 冲突（同 person 同营已有锚点）在 shadow 下不拦业务——冲突保留原锚点并
    记账本（回填/巡检报告）；enforce 下 check_camp_join 已在事前拦截，此处冲突
    属竞态，同样记账不覆盖。
    """
    user = db.session.get(UserModel, member.user_id)
    person = db.session.get(PersonModel, user.person_id) if user and user.person_id else None
    if person is None:
        _record('anchor', user or member.user_id, member.camp_session_id,
                [('person_missing', '建锚点失败：目标账号缺少人员档案')])
        return None
    existing = CampPersonParticipationModel.query.get(
        (member.camp_session_id, person.id))
    if existing is not None:
        if existing.camp_member_id != member.id:
            _record('anchor', user, member.camp_session_id, [(
                'duplicate_person',
                f'锚点冲突：本营已有 user#{existing.user_id} 的锚点，保留不覆盖')])
        return existing
    row = CampPersonParticipationModel(
        camp_session_id=member.camp_session_id, person_id=person.id,
        user_id=user.id, camp_member_id=member.id)
    db.session.add(row)
    events.record_event(
        'identity.participation.register', actor_user_id=user.id,
        target_ids={'user_id': user.id, 'person_id': person.id},
        after={'status': 'active'},
        evidence_refs={'note': f'camp#{member.camp_session_id}'},
        reason='营期参与锚点登记')
    return row


def close_camp_participation(member, *, reason='ended'):
    """成员退出/移除时锚点同步状态化（不 commit；幂等）。"""
    user = db.session.get(UserModel, member.user_id)
    if not user or not user.person_id:
        return None
    row = CampPersonParticipationModel.query.get(
        (member.camp_session_id, user.person_id))
    if row is None or row.camp_member_id != member.id:
        return row
    row.state = 'ended' if reason == 'ended' else row.state
    return row


# ── R3：工作区防复活 ────────────────────────────────────────────

def workspace_denied(user, workspace_id):
    """工作区授权判定（provisioning/手动授权入口复用）。

    防复活是硬约束（规格 9.4「不得为 merged 账号重建授权」「换主号不得绕过
    否决」均无条件），denied 与 enforcement 模式无关；模式只影响账本事件的
    措辞（shadow 记录/enforce 拒绝）。
    """
    violations = []
    if user.lifecycle == 'merged':
        violations.append(('merged_account', '已合并账号不再产生工作区授权'))
    if user.person_id:
        r = PersonWorkspaceRestrictionModel.query.get((user.person_id, workspace_id))
        if r is not None and r.state == 'vetoed':
            violations.append(('person_veto', '该人员被治理否决此工作区'))
    if not violations:
        return False, None
    _record('workspace_grant', user, workspace_id, violations)
    return True, violations[0][1]


def migrate_account_veto_to_person(user, workspace_id, *, reason, source_event=None):
    """账号级 veto 提升为人员级（回填脚本用；不 commit）。幂等：已有行人不动。"""
    if not user.person_id:
        return None
    existing = PersonWorkspaceRestrictionModel.query.get((user.person_id, workspace_id))
    if existing is not None:
        return existing
    row = PersonWorkspaceRestrictionModel(
        person_id=user.person_id, workspace_id=workspace_id,
        state='vetoed', reason=reason, source_event=source_event or 'account_veto')
    db.session.add(row)
    return row


# ── 记录 ────────────────────────────────────────────────────────

def _record(operation, user_or_id, scope_id, violations):
    uid = user_or_id.id if isinstance(user_or_id, UserModel) else user_or_id
    pid = user_or_id.person_id if isinstance(user_or_id, UserModel) else None
    events.record_event(
        'identity.enforcement.shadow', actor_user_id=uid,
        target_ids={'user_id': uid, 'person_id': pid},
        after=None,
        evidence_refs={'note': f'{operation}#{scope_id}: '
                               + ';'.join(v[0] for v in violations)[:120]},
        reason='人员规则' + ('拒绝（enforce）' if _is_enforce() else '记录（shadow）'))

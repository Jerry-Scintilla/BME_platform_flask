"""项目营应用服务：营期绑定、项目成员编排；不提交调用方事务。"""
from datetime import datetime
import uuid

from exts import db
from models import (CampUnit, CampMember, CampMemberEvent, CampMembershipEvent,
                    CampSession, UserModel, PersonModel, CampGroup,
                    GroupMembership, GroupRoleAssignment, GroupOperation, member_history_scope)
from services.groups import GroupService, GovernanceContext, GovernanceDecision, GroupError
from services.groups.service import _digest
from services.groups.identity import require_live_account, eligible_now
from services.identity import enforcement
from .policy import ProjectGovernancePolicy
from .reads import UnitMembershipRead


core = GroupService({'project': ProjectGovernancePolicy()})


def require_bound(unit):
    if unit.group_id is None:
        raise GroupError('历史项目尚未完成成员映射，请先预览并迁移该单元',
                         machine='PROJECT_GROUP_MAPPING_REQUIRED')


def lock_project(unit, actor_id, user_ids=()):
    """统一顺序：所有账号、人员、核心组、单元、营期。"""
    ids = sorted(set(user_ids) | {actor_id})
    users = UserModel.query.filter(UserModel.id.in_(ids)).order_by(UserModel.id).populate_existing().with_for_update().all()
    person_ids = sorted({user.person_id for user in users if user.person_id})
    if person_ids:
        PersonModel.query.filter(PersonModel.id.in_(person_ids)).order_by(PersonModel.id).populate_existing().with_for_update().all()
    if unit.group_id:
        CampGroup.query.filter_by(id=unit.group_id).populate_existing().with_for_update().one()
    CampUnit.query.filter_by(id=unit.id).populate_existing().with_for_update().one()
    CampSession.query.filter_by(id=unit.camp_session_id).populate_existing().with_for_update().one()


def _command(unit, actor_id, reason, key=None):
    group = db.session.get(CampGroup, unit.group_id)
    return dict(context=GovernanceContext('project', unit.id), actor_user_id=actor_id,
                group_id=unit.group_id, expected_version=group.version,
                reason=reason or '项目成员管理', idempotency_key=key or uuid.uuid4().hex)


def _event(unit, user_id, actor_id, action, source, reason, before=None, after=None):
    import json
    # 保留旧事件与 roster.version 契约；它是业务事件，不是第二份成员名单。
    db.session.add(CampMembershipEvent(
        camp_session_id=unit.camp_session_id, unit_id=unit.id, user_id=user_id,
        action=action, source=source, operator_id=actor_id, reason=reason,
        before=json.dumps(before) if before else None, after=json.dumps(after) if after else None))
    db.session.flush()


def ensure_camp_participant(sid, user_id, actor_id, reason):
    user = db.session.get(UserModel, user_id)
    if user is None:
        raise GroupError('参与账号不存在', status=404)
    ok, message = enforcement.check_camp_join(sid, user)
    if not ok:
        raise GroupError(message, machine='GROUP_IDENTITY_REQUIRED')
    with member_history_scope():
        member = CampMember.query.filter_by(camp_session_id=sid, user_id=user_id).first()
    if member and member.status != 'active':
        raise GroupError('该人员已退出营期，请先通过营期成员管理恢复参与', machine='GROUP_PARTICIPATION_REQUIRED')
    if member is None:
        member = CampMember(camp_session_id=sid, user_id=user_id, role='member', status='active')
        db.session.add(member)
        db.session.flush()
        db.session.add(CampMemberEvent(camp_session_id=sid, user_id=user_id, action='assign',
                                      operator_id=actor_id, reason=reason))
    anchor = enforcement.register_camp_participation(member)
    if anchor is not None and (anchor.user_id != user_id or anchor.camp_member_id != member.id):
        raise GroupError('营期人员锚点冲突，不能为同一人员新建第二份项目参与', machine='GROUP_DUPLICATE_PERSON')
    return member


def bind_new_project(unit, actor_id):
    """审核编排内调用：入池、组与营期绑定、负责人职责同事务建立。"""
    if unit.group_id:
        return unit
    if UnitMembershipRead.query.filter_by(unit_id=unit.id).first():
        raise GroupError('已有旧成员，必须走迁移映射', machine='PROJECT_GROUP_MAPPING_REQUIRED')
    lock_project(unit, actor_id, [unit.owner_user_id])
    ensure_camp_participant(unit.camp_session_id, unit.owner_user_id, actor_id, '项目审核自动入池')
    result = core.create_group(name=unit.name, owner_user_id=unit.owner_user_id,
                               context=GovernanceContext('project', unit.id), actor_user_id=actor_id,
                               idempotency_key=f'project:{unit.id}:create', reason='项目审核创建小组')
    unit.group_id = result['group']['id']
    db.session.flush()
    core.grant_role(user_id=unit.owner_user_id, role='project_leader',
                    **_command(unit, actor_id, '项目审核任命负责人', f'project:{unit.id}:leader'))
    _event(unit, unit.owner_user_id, actor_id, 'select', 'approve', '项目审核', after={'role': 'leader'})
    return unit


def add_member(unit, user_id, role, source, actor_id, reason=None):
    require_bound(unit)
    if role != 'member':
        raise GroupError('负责人任命必须走转交流程', machine='GROUP_FORBIDDEN', status=403)
    with db.session.begin_nested():
        lock_project(unit, actor_id, [user_id])
        participant = CampMember.query.filter_by(camp_session_id=unit.camp_session_id,
            user_id=user_id, status='active').populate_existing().with_for_update().first()
        if participant is None:
            raise GroupError('须先通过营期准入，不能通过入组自动入营', machine='GROUP_PARTICIPATION_REQUIRED')
        ensure_camp_participant(unit.camp_session_id, user_id, actor_id, reason or '加入项目')
        core.add_member(user_id=user_id, **_command(unit, actor_id, reason))
        core.grant_role(user_id=user_id, role='project_member', **_command(unit, actor_id, reason))
        _event(unit, user_id, actor_id, 'select', source, reason, after={'role': 'member'})
    return UnitMembershipRead.query.filter_by(unit_id=unit.id, user_id=user_id).populate_existing().one()


def end_member(unit, user_id, action, source, actor_id, reason=None):
    require_bound(unit)
    with db.session.begin_nested():
        lock_project(unit, actor_id, [user_id])
        core.end_membership(user_id=user_id, **_command(unit, actor_id, reason))
        _event(unit, user_id, actor_id, action, source, reason, after={'status': 'ended'})


def transfer_leader(unit, user_id, actor_id, reason):
    require_bound(unit)
    with db.session.begin_nested():
        old_id = unit.owner_user_id
        lock_project(unit, actor_id, [old_id, user_id])
        if old_id != unit.owner_user_id:
            raise GroupError('负责人已变化，请刷新', machine='GROUP_VERSION_CONFLICT')
        ensure_camp_participant(unit.camp_session_id, user_id, actor_id, reason)
        core.add_member(user_id=user_id, **_command(unit, actor_id, reason))
        core.transfer_owner(user_id=user_id, **_command(unit, actor_id, reason))
        core.end_role(user_id=user_id, role='project_member', **_command(unit, actor_id, reason))
        core.grant_role(user_id=user_id, role='project_leader', **_command(unit, actor_id, reason))
        core.end_role(user_id=old_id, role='project_leader', **_command(unit, actor_id, reason))
        old_user = db.session.get(UserModel, old_id)
        decision = GovernanceDecision('camp:project_admin', 'camp_join', unit.camp_session_id)
        policy = ProjectGovernancePolicy()
        if eligible_now(old_user, decision) and policy.participant_allowed(
                context=GovernanceContext('project', unit.id), group=None, user=old_user):
            core.grant_role(user_id=old_id, role='project_member', **_command(unit, actor_id, reason))
        else:
            # 停用/离营的旧负责人仍应能交接，但不能给其重新授予成员职责。
            core.end_membership(user_id=old_id, **_command(unit, actor_id, reason))
        db.session.expire(unit, ['camp_group'])
        _event(unit, user_id, actor_id, 'leader_change', 'admin_adjust', reason,
               before={'leader': old_id}, after={'leader': user_id})


def set_status(unit, status, actor_id, reason):
    require_bound(unit)
    members = GroupMembership.query.filter_by(group_id=unit.group_id, status='active').all()
    with db.session.begin_nested():
        lock_project(unit, actor_id, [m.user_id for m in members])
        actor = db.session.get(UserModel, actor_id)
        if not actor or not actor.is_admin():
            raise GroupError('仅管理员可以变更项目状态', status=403)
        if status not in ('active', 'paused', 'terminated') or unit.status == 'terminated':
            raise GroupError('项目状态转换无效')
        policy = ProjectGovernancePolicy()
        policy.authorize(context=GovernanceContext('project', unit.id), actor=actor,
                         group=db.session.get(CampGroup, unit.group_id),
                         action='archive_group', payload={})
        previous = unit.status
        if status == 'terminated':
            core.archive_group(end_memberships=True, **_command(unit, actor_id, reason))
        unit.status = status
        _event(unit, unit.owner_user_id, actor_id, 'unit_status', 'admin_adjust', reason,
               before={'unit_status': previous}, after={'unit_status': status})


def unit_role(unit, user):
    try:
        require_live_account(user)
    except GroupError:
        return None
    if user.is_admin():
        return 'leader'
    if not unit.group_id:
        if not CampMember.query.filter_by(camp_session_id=unit.camp_session_id,
                                          user_id=user.id, status='active').first():
            return None
        row = UnitMembershipRead.query.filter_by(unit_id=unit.id, user_id=user.id, status='active').first()
        return row.role if row else None
    try:
        roles = core.effective_roles(context=GovernanceContext('project', unit.id),
                                      actor_user_id=user.id, group_id=unit.group_id, user_id=user.id)
    except GroupError:
        return None
    names = {r['role'] for r in roles}
    return 'leader' if 'project_leader' in names else ('member' if 'project_member' in names else None)


def selection_version(unit_id):
    row = CampMembershipEvent.query.filter_by(unit_id=unit_id).order_by(CampMembershipEvent.id.desc()).first()
    return row.id if row else 0


def select_members(unit, actor_id, operations, key, expected_version):
    """整批持锁、数据库幂等；保留原端点逐项结果格式。"""
    require_bound(unit)
    if not isinstance(key, str) or not key.strip() or len(key) > 128 or type(expected_version) is not int or expected_version < 0:
        raise GroupError('幂等键或预期名单版本无效', status=400)
    if not isinstance(operations, list) or not 1 <= len(operations) <= 500:
        raise GroupError('operations 必须包含 1 至 500 项', status=400)
    for item in operations:
        if not isinstance(item, dict) or type(item.get('user_id')) is not int or item['user_id'] < 1:
            raise GroupError('成员 ID 必须为正整数', status=400)
    digest = _digest([unit.id, operations, expected_version])
    operation_id = _digest(['project_selection', actor_id, unit.id, key])
    with db.session.begin_nested():
        lock_project(unit, actor_id, [o['user_id'] for o in operations])
        actor = db.session.get(UserModel, actor_id)
        if unit_role(unit, actor) != 'leader':
            raise GroupError('仅负责人可勾选成员', status=403)
        camp = db.session.get(CampSession, unit.camp_session_id)
        if camp.status != 'selecting' or unit.status != 'active':
            raise GroupError('当前不在组队窗口', machine='CAMP_FORMATION_CLOSED')
        prior = GroupOperation.query.filter_by(id=operation_id).with_for_update().first()
        if prior:
            if prior.request_digest != digest:
                raise GroupError('同幂等键请求内容不一致', machine='IDEMPOTENCY_CONFLICT')
            return prior.result
        if expected_version != selection_version(unit.id):
            raise GroupError('名单已变化，请刷新', machine='SELECTION_VERSION_CONFLICT')
        results = []
        for item in operations:
            uid, op = item['user_id'], item.get('op')
            row = UnitMembershipRead.query.filter_by(unit_id=unit.id, user_id=uid).populate_existing().first()
            outcome = dict(user_id=uid, op=op)
            try:
                if op == 'add':
                    if row and row.status == 'active':
                        outcome['result'] = 'unchanged'
                    else:
                        add_member(unit, uid, 'member', 'leader_pick', actor_id)
                        outcome['result'] = 'added'
                elif op == 'remove':
                    if not row or row.status != 'active':
                        outcome['result'] = 'unchanged'
                    else:
                        end_member(unit, uid, 'deselect', 'leader_pick', actor_id)
                        outcome['result'] = 'removed'
                else:
                    raise GroupError('op 仅支持 add/remove', status=400)
            except GroupError as exc:
                outcome.update(result='forbidden', message=str(exc), machine=exc.machine)
            results.append(outcome)
        result = dict(code=200, version=selection_version(unit.id), results=results)
        db.session.add(GroupOperation(id=operation_id, actor_user_id=actor_id, group_id=unit.group_id,
                                      request_digest=digest, result=result))
        db.session.flush()
        return result


def end_camp_participations(sid, user_id, actor_id, reason):
    """退营先结束绑定组参与；仍为负责人时要求先转交，不留下隐形复活职务。"""
    GroupService._lock_users(actor_id, user_id)
    units = CampUnit.query.join(GroupMembership, GroupMembership.group_id == CampUnit.group_id).filter(
        CampUnit.camp_session_id == sid, CampUnit.unit_type == 'project', GroupMembership.user_id == user_id,
        GroupMembership.status == 'active').order_by(CampUnit.id).all()
    if any(unit.owner_user_id == user_id for unit in units):
        raise GroupError('请先转交该成员负责的项目，再移出营期', machine='GROUP_OWNER_REQUIRED')
    if units and not db.session.get(UserModel, actor_id).is_admin():
        raise GroupError('该成员参与项目，请由管理员处理项目退出后再退营', status=403)
    for unit in units:
        end_member(unit, user_id, 'exit', 'admin_adjust', actor_id, reason or '退出营期')

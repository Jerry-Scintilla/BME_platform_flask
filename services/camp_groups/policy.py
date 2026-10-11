"""真实营期绑定与项目治理策略；不导入营期蓝图。"""
from datetime import datetime

from exts import db
from models import CampSession, CampUnit, CampMember, GroupMembership, GroupRoleAssignment
from services.groups import GovernancePolicy, GovernanceDecision, GroupError
from services.groups.identity import eligible_now


class ProjectGovernancePolicy(GovernancePolicy):
    def _resolve(self, context, *, lock=False):
        query = CampUnit.query.filter_by(id=context.scope_id, unit_type='project')
        unit = query.populate_existing().with_for_update().first() if lock else query.first()
        if unit is None:
            raise GroupError('项目单元不存在', status=404)
        query = CampSession.query.filter_by(id=unit.camp_session_id, category='project')
        camp = query.populate_existing().with_for_update().first() if lock else query.first()
        if camp is None or camp.status == 'deleted':
            raise GroupError('营期不存在', status=404)
        return unit, camp

    @staticmethod
    def _participant(camp_id, user_id):
        return CampMember.query.filter_by(camp_session_id=camp_id, user_id=user_id,
                                          status='active').first() is not None

    def authorize(self, *, context, actor, group, action, payload):
        reading = action.startswith('read_')
        unit, camp = self._resolve(context, lock=not reading)
        if context.kind != 'project':
            return None
        if group is None:
            if action != 'create_group' or unit.group_id is not None:
                return None
        elif unit.group_id != group.id:
            return None
        decision = GovernanceDecision('camp:project_admin', 'camp_join', camp.id)
        if not reading and (camp.status == 'archived' or unit.status == 'terminated'):
            raise GroupError('营期或项目已结束，只能读取', machine='GROUP_READ_ONLY')
        # 保留既有项目管理端的 super_admin 范围，不顺便扩权给 CampStaff。
        if actor.is_admin():
            if action == 'rename_group':
                raise GroupError('项目名称变更须同步项目档案，当前不开放', machine='GROUP_FORBIDDEN', status=403)
            return decision
        if group is None or not self._participant(camp.id, actor.id):
            return None
        member = GroupMembership.query.filter_by(group_id=group.id, user_id=actor.id, status='active').first()
        if member is None or not eligible_now(actor, decision):
            return None
        leader = GroupRoleAssignment.query.filter_by(
            membership_id=member.id, role='project_leader', scope_type='group', scope_id=0,
            active_slot=1).filter(
                GroupRoleAssignment.started_at <= datetime.now(),
                db.or_(GroupRoleAssignment.expires_at.is_(None),
                       GroupRoleAssignment.expires_at > datetime.now())).first()
        is_leader = leader is not None and eligible_now(actor, decision, appointment=True)
        if reading:
            if action == 'read_events' and not is_leader:
                return None
            return GovernanceDecision('camp:project_member', 'camp_join', camp.id)
        # 普通负责人仅能在选组期调整普通成员，不能任命组长或变更治理负责人。
        if is_leader and camp.status == 'selecting' and unit.status == 'active':
            if action in ('add_member', 'end_membership') or (
                    action in ('grant_role', 'end_role') and payload.get('role') == 'project_member'):
                return GovernanceDecision('camp:project_leader_selection', 'camp_join', camp.id)
        return None

    def participant_allowed(self, *, context, group, user):
        _, camp = self._resolve(context)
        return CampMember.query.filter(CampMember.camp_session_id == camp.id,
            CampMember.user_id == user.id, CampMember.status == 'active',
            CampMember.role.in_(('member', 'student'))).first() is not None

    def role_allowed(self, *, context, group, user, role, scope_type, scope_id):
        return (role in ('project_leader', 'project_member') and scope_type == 'group'
                and scope_id == 0 and not (role == 'project_leader' and user.is_admin())
                and self.participant_allowed(context=context, group=group, user=user))

"""项目成员集中读取：已绑定单元只读核心，未绑定单元只读历史表，不双写。"""
from sqlalchemy import case, event, literal, select, union_all

from exts import db
from models import (CampUnit, CampUnitMember, GroupMembership, GroupRoleAssignment,
                    LegacyUnitMembershipMap, ProjectLegacyMap, ProjectCampParticipant, ProjectCampBinding, CampMember)

_owned = select(ProjectLegacyMap.unit_id).where(ProjectLegacyMap.write_model == 'project')


_old = select(
    literal('legacy').label('source'), CampUnitMember.id, CampUnitMember.unit_id,
    CampUnitMember.user_id, CampUnitMember.role, CampUnitMember.status,
    CampUnitMember.started_at, CampUnitMember.ended_at,
).join(CampUnit, CampUnit.id == CampUnitMember.unit_id).where(CampUnit.group_id.is_(None), CampUnit.unit_type == 'project', ~CampUnit.id.in_(_owned))

# 结束参与后仍显示最后一次职责，供档案使用；授权另走 unit_role/effective_roles。
_last_role = select(GroupRoleAssignment.role).where(
    GroupRoleAssignment.membership_id == GroupMembership.id,
    GroupRoleAssignment.role.in_(('project_leader', 'project_member')),
).order_by(GroupRoleAssignment.id.desc()).limit(1).correlate(GroupMembership).scalar_subquery()
_legacy_role = select(CampUnitMember.role).join(
    LegacyUnitMembershipMap, LegacyUnitMembershipMap.legacy_member_id == CampUnitMember.id
).where(LegacyUnitMembershipMap.membership_id == GroupMembership.id).correlate(GroupMembership).scalar_subquery()
_new = select(
    literal('core').label('source'), GroupMembership.id, CampUnit.id.label('unit_id'),
    GroupMembership.user_id,
    case((_last_role == 'project_leader', 'leader'), (_last_role == 'project_member', 'member'),
         else_=db.func.coalesce(_legacy_role, 'member')).label('role'),
    GroupMembership.status, GroupMembership.started_at, GroupMembership.ended_at,
).join(CampUnit, CampUnit.group_id == GroupMembership.group_id).where(CampUnit.unit_type == 'project', ~CampUnit.id.in_(_owned))

_project = select(
    literal('project_core').label('source'), ProjectCampParticipant.id,
    ProjectLegacyMap.unit_id, ProjectCampParticipant.user_id,
    case((ProjectCampParticipant.user_id == ProjectCampBinding.leader_user_id, 'leader'), else_='member').label('role'),
    case((CampMember.status == 'active', ProjectCampParticipant.status), else_='ended').label('status'),
    ProjectCampParticipant.started_at, ProjectCampParticipant.ended_at,
).join(ProjectCampBinding, ProjectCampBinding.id == ProjectCampParticipant.binding_id).join(
    ProjectLegacyMap, ProjectLegacyMap.binding_id == ProjectCampBinding.id).join(
    CampMember, CampMember.id == ProjectCampParticipant.camp_member_id).where(ProjectLegacyMap.write_model == 'project')


class UnitMembershipRead(db.Model):
    __table__ = union_all(_old, _new, _project).subquery('unit_membership_read')
    __mapper_args__ = {'primary_key': [__table__.c.source, __table__.c.id]}


def current_members(unit):
    """归档在领域锁后读取基础事实，避免RR快照及UNION派生表的旧读。"""
    from types import SimpleNamespace
    def current(query):return query.populate_existing().with_for_update().all()
    linked=current(ProjectLegacyMap.query.filter_by(unit_id=unit.id,write_model='project'))
    if linked:
        binding=current(ProjectCampBinding.query.filter_by(id=linked[0].binding_id))[0]
        rows=current(ProjectCampParticipant.query.filter_by(binding_id=binding.id))
        people={m.id:m for m in current(CampMember.query.filter(CampMember.id.in_([r.camp_member_id for r in rows])))}
        return [SimpleNamespace(user_id=r.user_id,role='leader' if r.user_id==binding.leader_user_id else 'member',status=r.status if people[r.camp_member_id].status=='active' else 'ended') for r in rows]
    if unit.group_id:
        rows=current(GroupMembership.query.filter_by(group_id=unit.group_id))
        roles=current(GroupRoleAssignment.query.filter_by(group_id=unit.group_id).order_by(GroupRoleAssignment.id))
        names={r.membership_id:r.role for r in roles if r.role in ('project_leader','project_member')}
        return [SimpleNamespace(user_id=r.user_id,role='leader' if names.get(r.id)=='project_leader' else 'member',status=r.status) for r in rows]
    return current(CampUnitMember.query.filter_by(unit_id=unit.id))


def _read_only(*_args):
    raise RuntimeError('项目成员投影只读，请通过营期小组应用服务变更')


for _event in ('before_insert', 'before_update', 'before_delete'):
    event.listen(UnitMembershipRead, _event, _read_only)

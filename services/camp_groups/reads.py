"""项目成员集中读取：已绑定单元只读核心，未绑定单元只读历史表，不双写。"""
from sqlalchemy import case, event, literal, select, union_all

from exts import db
from models import (CampUnit, CampUnitMember, GroupMembership, GroupRoleAssignment,
                    LegacyUnitMembershipMap)


_old = select(
    literal('legacy').label('source'), CampUnitMember.id, CampUnitMember.unit_id,
    CampUnitMember.user_id, CampUnitMember.role, CampUnitMember.status,
    CampUnitMember.started_at, CampUnitMember.ended_at,
).join(CampUnit, CampUnit.id == CampUnitMember.unit_id).where(CampUnit.group_id.is_(None), CampUnit.unit_type == 'project')

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
).join(CampUnit, CampUnit.group_id == GroupMembership.group_id).where(CampUnit.unit_type == 'project')


class UnitMembershipRead(db.Model):
    __table__ = union_all(_old, _new).subquery('unit_membership_read')
    __mapper_args__ = {'primary_key': [__table__.c.source, __table__.c.id]}


def _read_only(*_args):
    raise RuntimeError('项目成员投影只读，请通过营期小组应用服务变更')


for _event in ('before_insert', 'before_update', 'before_delete'):
    event.listen(UnitMembershipRead, _event, _read_only)

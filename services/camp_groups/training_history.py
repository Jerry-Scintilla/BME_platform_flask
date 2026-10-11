"""培训历史读模型；只给本营管理人员扩展历史范围，不参与当前教学授权。"""
from contextlib import nullcontext
from datetime import datetime
from exts import db
from models import (CampMember, CampAttendancePlan, GroupMembership,
                    GroupRoleAssignment, CampUnit, UserModel, member_history_scope)
from . import training_access as access


def manager(sid, user):
    return bool(user and (user.is_admin() or access.staff(sid, user.id)))


def include_history(camp, user, requested=False):
    return manager(camp.id, user) and (camp.status == 'archived' or requested)


def members(sid, *, history=False):
    with member_history_scope() if history else nullcontext():
        return CampMember.query.filter_by(camp_session_id=sid).order_by(CampMember.user_id).all()


def attendance_ids(camp, user, *, history=False):
    if manager(camp.id, user):
        roster = members(camp.id, history=history)
        ids = {row.user_id for row in roster if row.needs_attendance}
        if history:
            # 已退出或后来免考勤者仍保留过去的承诺日，不据现在的职责擦掉历史。
            ids |= {row.user_id for row in CampAttendancePlan.query.filter_by(camp_session_id=camp.id)}
        return sorted(ids)
    current = access.taught_students(camp.id, user.id)
    return sorted(row.user_id for row in members(camp.id) if row.needs_attendance and row.user_id in current)


def group_roster(unit):
    people = GroupMembership.query.filter_by(group_id=unit.group_id).order_by(GroupMembership.id).all()
    roles = GroupRoleAssignment.query.filter_by(group_id=unit.group_id).order_by(GroupRoleAssignment.id).all()
    names = {u.id: u.username for u in UserModel.query.filter(UserModel.id.in_([p.user_id for p in people]))}
    camp_members = {p.user_id:p for p in members(unit.camp_session_id, history=True)}
    now = datetime.now()
    return [dict(user_id=p.user_id, username=names.get(p.user_id, ''), membership_status=p.status,
        camp_status=camp_members[p.user_id].status if p.user_id in camp_members else 'removed',
        started_at=p.started_at.isoformat() if p.started_at else None,
        ended_at=p.ended_at.isoformat() if p.ended_at else None,
        roles=[dict(id=r.id,role=r.role,scope_type=r.scope_type,scope_id=r.scope_id,
            active=p.status == 'active' and p.user_id in camp_members and camp_members[p.user_id].status == 'active' and r.active_slot == 1 and r.started_at <= now and (not r.expires_at or r.expires_at > now),
            started_at=r.started_at.isoformat(),ended_at=r.ended_at.isoformat() if r.ended_at else None)
            for r in roles if r.membership_id == p.id]) for p in people]


def mentor_units(sid):
    """曾任/现任导生筛选只用于老师历史报表，不恢复其任何访问权限。"""
    pairs = db.session.query(GroupMembership.user_id, CampUnit.id).join(
        GroupRoleAssignment, GroupRoleAssignment.membership_id == GroupMembership.id).join(
        CampUnit, CampUnit.group_id == GroupMembership.group_id).filter(
        CampUnit.camp_session_id == sid, CampUnit.unit_type == 'mentor_team', GroupRoleAssignment.role == 'mentor').all()
    result = {}
    for uid, unit_id in pairs:
        result.setdefault(uid, set()).add(unit_id)
    return result

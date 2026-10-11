"""教学职责与兼容读口径；不依赖蓝图，不以客户端视角作为授权。"""
from datetime import datetime
from exts import db
from models import (CampSession, CampMember, CampStaff, CampUnit, CampGroup,
                    GroupMembership, GroupRoleAssignment, UserModel,
                    CampCourseAssignment, TrainingGroupProfile)
from services.groups import GovernanceDecision
from services.groups.identity import eligible_now


def enabled(sid):
    camp = db.session.get(CampSession, sid)
    return bool(camp and camp.category == 'learning' and camp.training_groups_ready)



def recruitment_mentor(member):
    """兼容字段的唯一读口径：切换后不回退可能过期的旧列。"""
    if not enabled(member.camp_session_id):
        return member.legacy_team_mentor_id
    if not member.recruitment_unit_id:
        return None
    profile = db.session.get(TrainingGroupProfile, member.recruitment_unit_id)
    unit = db.session.get(CampUnit, member.recruitment_unit_id)
    if not profile or not unit or profile.camp_session_id != member.camp_session_id or unit.camp_session_id != member.camp_session_id or unit.unit_type != 'mentor_team':
        return None
    return profile.recruitment_mentor_id


def recruitment_mentor_expression(cls):
    ready = db.exists(db.select(CampSession.id).where(CampSession.id == cls.camp_session_id,
        CampSession.category == 'learning', CampSession.training_groups_ready.is_(True)).correlate(cls))
    mentor = db.select(TrainingGroupProfile.recruitment_mentor_id).join(
        CampUnit, CampUnit.id == TrainingGroupProfile.unit_id).where(
        TrainingGroupProfile.unit_id == cls.recruitment_unit_id,
        TrainingGroupProfile.camp_session_id == cls.camp_session_id,
        CampUnit.camp_session_id == cls.camp_session_id, CampUnit.unit_type == 'mentor_team').correlate(cls).scalar_subquery()
    return db.case((ready, mentor), else_=cls.legacy_team_mentor_id)


def role_query(sid, uid=None, role=None, *, include_paused=False):
    now = datetime.now()
    q = db.session.query(GroupRoleAssignment, GroupMembership, CampUnit).join(
        GroupMembership, GroupMembership.id == GroupRoleAssignment.membership_id).join(
        CampUnit, CampUnit.group_id == GroupMembership.group_id).join(
        CampGroup, CampGroup.id == CampUnit.group_id).join(
        CampMember, db.and_(CampMember.camp_session_id == CampUnit.camp_session_id,
                           CampMember.user_id == GroupMembership.user_id))
    q = q.filter(CampUnit.camp_session_id == sid, CampUnit.unit_type == 'mentor_team',
                 CampUnit.status.in_(('active', 'paused')) if include_paused else CampUnit.status == 'active', CampGroup.status == 'active',
                 CampMember.status == 'active', GroupMembership.status == 'active',
                 GroupRoleAssignment.active_slot == 1, GroupRoleAssignment.started_at <= now,
                 db.or_(GroupRoleAssignment.expires_at.is_(None), GroupRoleAssignment.expires_at > now))
    if uid is not None:
        q = q.filter(GroupMembership.user_id == uid)
    if role:
        q = q.filter(GroupRoleAssignment.role == role)
    return q


def roles(sid, uid, *, unit_id=None, course_id=None):
    user = db.session.get(UserModel, uid)
    decision = GovernanceDecision('camp:training', 'camp_join', sid)
    if not eligible_now(user, decision):
        return []
    result = []
    query = role_query(sid, uid)
    if unit_id is not None:
        query = query.filter(CampUnit.id == unit_id)
    for r, member, unit in query.all():
        if member.person_id != user.person_id:
            continue
        if r.role == 'mentor' and not eligible_now(user, decision, appointment=True):
            continue
        if course_id is not None and r.scope_type == 'course' and r.scope_id != course_id:
            continue
        result.append((r, unit))
    return result


def has_role(sid, uid, role):
    member = CampMember.query.filter_by(camp_session_id=sid, user_id=uid, status='active').first()
    if not member:
        return False
    if role == 'student':
        return bool(member.is_learner)
    if role != 'mentor' or not enabled(sid):
        return member.role == role
    return any(r.role == role for r, _ in roles(sid, uid))


def mentor_expression(cls):
    # SQL 只用于候选/统计；具体操作还通过 roles 做人员及核验门槛复核。
    now = datetime.now()
    ready = db.select(CampSession.training_groups_ready).where(CampSession.id == cls.camp_session_id).correlate(cls).scalar_subquery()
    active = db.exists(db.select(GroupRoleAssignment.id).select_from(GroupRoleAssignment).join(
        GroupMembership, GroupMembership.id == GroupRoleAssignment.membership_id).join(
        CampUnit, CampUnit.group_id == GroupMembership.group_id).join(
        CampGroup, CampGroup.id == CampUnit.group_id).where(
            CampUnit.camp_session_id == cls.camp_session_id, GroupMembership.user_id == cls.user_id,
            CampUnit.unit_type == 'mentor_team', CampUnit.status == 'active', CampGroup.status == 'active',
            GroupMembership.status == 'active', GroupRoleAssignment.role == 'mentor',
            GroupRoleAssignment.active_slot == 1, GroupRoleAssignment.started_at <= now,
            db.or_(GroupRoleAssignment.expires_at.is_(None), GroupRoleAssignment.expires_at > now)))
    return db.or_(db.and_(ready.is_(True), active), db.and_(ready.is_not(True), cls.role == 'mentor'))


def member_roles(member):
    if not member or member.status != 'active':
        return []
    result = []
    if member.is_learner:
        result.append('student')
    if has_role(member.camp_session_id, member.user_id, 'mentor'):
        result.append('mentor')
    if member.role == 'member':
        result.append('member')
    return result


def staff(sid, uid):
    return CampStaff.query.filter_by(camp_session_id=sid, user_id=uid, status='active').first()


def learner_memberships(sid, *, unit_ids, course_id=None):
    """按明确组范围一次取候选；仅在本次调用复用人员核验结果，不跨写入缓存授权。"""
    if not unit_ids:
        return []
    query = role_query(sid, role='learner').filter(CampUnit.id.in_(unit_ids))
    if course_id is not None:
        query = query.filter(db.or_(GroupRoleAssignment.scope_type == 'group', GroupRoleAssignment.scope_id == course_id))
    rows = query.all()
    users = {u.id:u for u in UserModel.query.filter(UserModel.id.in_({m.user_id for _,m,_ in rows}))} if rows else {}
    decision = GovernanceDecision('camp:training', 'camp_join', sid)
    allowed = {uid for uid,user in users.items() if eligible_now(user, decision)}
    return [(r,m,u) for r,m,u in rows if m.user_id in allowed and users[m.user_id].person_id == m.person_id]


def taught_students(sid, mentor_id, *, course_id=None, unit_id=None):
    if not enabled(sid):
        return {m.user_id for m in CampMember.query.filter_by(
            camp_session_id=sid, team_mentor_id=mentor_id).filter(CampMember.is_learner).all()}
    units = {u.id for r, u in roles(sid, mentor_id, unit_id=unit_id, course_id=course_id) if r.role == 'mentor'}
    return {member.user_id for _,member,_ in learner_memberships(sid, unit_ids=units, course_id=course_id)
            if member.user_id != mentor_id}


def can_teach(sid, mentor_id, student_id, course_id):
    if mentor_id == student_id:
        return False
    row = CampCourseAssignment.query.filter_by(camp_session_id=sid, student_user_id=student_id,
                                               course_id=course_id, status='active').first()
    if not row:
        return False
    if not enabled(sid):
        return has_role(sid, mentor_id, 'mentor') and student_id in taught_students(sid, mentor_id)
    if not row.unit_id or student_id not in taught_students(sid, mentor_id, unit_id=row.unit_id, course_id=course_id):
        return False
    profile = db.session.get(TrainingGroupProfile, row.unit_id)
    return bool(profile and course_id in (profile.course_ids or []))


def learner_units(sid, uid):
    return sorted({u.id for r, u in roles(sid, uid) if r.role == 'learner'})


def approver_ids(sid, uid):
    if not enabled(sid):
        m = CampMember.query.filter_by(camp_session_id=sid, user_id=uid).first()
        return [m.team_mentor_id] if m and m.team_mentor_id and m.team_mentor_id != uid and has_role(sid, m.team_mentor_id, 'mentor') else []
    units = set(learner_units(sid, uid))
    candidates = {m.user_id for r, m, u in role_query(sid, role='mentor').all() if u.id in units and m.user_id != uid}
    return sorted(mid for mid in candidates if any(r.role == 'mentor' and u.id in units for r, u in roles(sid, mid)))


def meeting_unit_id(meeting):
    if meeting.unit_id:
        return meeting.unit_id
    profile = TrainingGroupProfile.query.filter_by(camp_session_id=meeting.camp_session_id,
                                                   recruitment_mentor_id=meeting.mentor_id).first()
    return profile.unit_id if profile else None


def meeting_students(meeting, *, assigned_at=None, course_id=None):
    if not enabled(meeting.camp_session_id):
        ids = taught_students(meeting.camp_session_id, meeting.mentor_id)
        if assigned_at:
            ids &= {r.user_id for r in CampMember.query.filter_by(camp_session_id=meeting.camp_session_id).all()
                    if r.joined_at and r.joined_at <= assigned_at}
        return sorted(ids)
    unit_id = meeting_unit_id(meeting)
    from models import GroupMembershipEvent
    candidates = learner_memberships(meeting.camp_session_id, unit_ids={unit_id} if unit_id else set(), course_id=course_id)
    event = (GroupMembershipEvent.query.filter_by(context_kind='training', context_id=unit_id,
        action='legacy_mapping').order_by(GroupMembershipEvent.id).first()) if assigned_at and unit_id else None
    legacy_users = {r['user_id'] for r in (event.before or {}).get('source', {}).get('members', [])} if event else set()
    result = set()
    for role, member, unit in candidates:
        since = max(role.started_at, member.started_at)
        # 显式旧名单承接不是新入组；保持原营期加入时间下界，不伪造旧换组时间。
        if event and member.user_id in legacy_users and since <= event.occurred_at:
            old = CampMember.query.filter_by(camp_session_id=meeting.camp_session_id, user_id=member.user_id).first()
            since = old.joined_at if old and old.joined_at else since
        if assigned_at is None or since <= assigned_at:
            result.add(member.user_id)
    return sorted(result)

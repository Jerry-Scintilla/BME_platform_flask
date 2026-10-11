"""培训营应用服务：编组、招生、课程责任；所有变更随调用方事务提交。"""
from contextvars import ContextVar
from datetime import datetime
import json
from uuid import uuid4

from exts import db
from models import (CampSession, CampUnit, CampGroup, CampMember, CampStaff, UserModel,
                    GroupMembership, GroupRoleAssignment, TrainingGroupProfile,
                    CampMentorProfile, CampMentorMatch, CampCourseAssignment, UserCourseModel,
                    CampTeachingEvent, PersonModel)
from services.groups import GroupService, GovernancePolicy, GovernanceContext, GovernanceDecision, GroupError
from services.groups.identity import require_live_account
from . import training_access as access

_selection = ContextVar('training_selection_command', default=None)


def lock_users(ids):
    users = UserModel.query.filter(UserModel.id.in_(sorted({i for i in ids if i is not None}))).order_by(UserModel.id).populate_existing().with_for_update().all()
    pids = sorted({u.person_id for u in users if u.person_id})
    if pids:
        PersonModel.query.filter(PersonModel.id.in_(pids)).order_by(PersonModel.id).populate_existing().with_for_update().all()


def managed(sid, actor):
    return actor.is_admin() or access.staff(sid, actor.id) is not None


def lock_camp(sid):
    camp = CampSession.query.filter_by(id=sid).populate_existing().with_for_update().first()
    if not camp or camp.category != 'learning' or camp.status == 'deleted':
        raise GroupError('培训营不存在', status=404)
    if camp.status == 'archived':
        raise GroupError('营期已归档，只读', machine='GROUP_READ_ONLY')
    from .maintenance import assert_writable
    assert_writable(camp)
    if not camp.training_groups_ready:
        raise GroupError('此营期须先完成旧培训名单映射', machine='TRAINING_MAPPING_REQUIRED')
    return camp


class TrainingGovernancePolicy(GovernancePolicy):
    def authorize(self, *, context, actor, group, action, payload):
        unit = db.session.get(CampUnit, context.scope_id)
        if not unit or unit.unit_type != 'mentor_team':
            return None
        camp = db.session.get(CampSession, unit.camp_session_id)
        if not camp or not camp.training_groups_ready or camp.status == 'deleted':
            return None
        if group is not None and unit.group_id != group.id:
            return None
        if group is None and (action != 'create_group' or unit.group_id):
            return None
        reading = action.startswith('read_')
        if not reading and (camp.status == 'archived' or unit.status != 'active'):
            raise GroupError('营期或小组只读', machine='GROUP_READ_ONLY')
        if managed(camp.id, actor):
            return GovernanceDecision('camp:training_staff', 'camp_join', camp.id)
        if reading and any(u.id == unit.id for _, u in access.roles(camp.id, actor.id)):
            if action != 'read_events' or any(r.role == 'mentor' and u.id == unit.id for r, u in access.roles(camp.id, actor.id)):
                return GovernanceDecision('camp:training_member', 'camp_join', camp.id)
        selection = _selection.get()
        if selection == (actor.id, unit.id, payload.get('user_id')) and (
                action in ('add_member', 'end_membership') or
                (action in ('grant_role', 'end_role') and payload.get('role') == 'learner')):
            return GovernanceDecision('camp:mentor_selection', 'camp_join', camp.id)
        return None

    def participant_allowed(self, *, context, group, user):
        unit = db.session.get(CampUnit, context.scope_id)
        return bool(unit and CampMember.query.filter_by(camp_session_id=unit.camp_session_id,
                                                       user_id=user.id, status='active').first())

    def role_allowed(self, *, context, group, user, role, scope_type, scope_id):
        profile = db.session.get(TrainingGroupProfile, context.scope_id)
        return (role in ('learner', 'mentor') and not user.is_admin()
                and self.participant_allowed(context=context, group=group, user=user)
                and (scope_type == 'group' and scope_id == 0 or
                     scope_type == 'course' and profile is not None and scope_id in (profile.course_ids or [])))


core = GroupService({'training': TrainingGovernancePolicy()})


def command(unit, actor, reason):
    group = db.session.get(CampGroup, unit.group_id)
    return dict(context=GovernanceContext('training', unit.id), actor_user_id=actor,
                group_id=group.id, expected_version=group.version,
                idempotency_key=str(uuid4()), reason=reason)


def record(sid, uid, actor, action, before=None, after=None, course_id=None, chapter_id=None, reason='教学关系调整'):
    db.session.add(CampTeachingEvent(camp_session_id=sid, user_id=uid, actor_user_id=actor,
        action=action, before=before, after=after, course_id=course_id, chapter_id=chapter_id, reason=reason))


def require_manager(sid, actor_id):
    actor = db.session.get(UserModel, actor_id)
    require_live_account(actor)
    if not managed(sid, actor):
        raise GroupError('需要本营成员管理权限', status=403)
    return actor


def validate_courses(camp, direction, course_ids):
    try:
        definitions = json.loads(camp.ms_tags or '[]')
    except (ValueError, TypeError):
        definitions = []
    catalog = set()
    selected = None
    for d in definitions:
        if isinstance(d, dict):
            ids = d.get('course_ids') or ([d['course_id']] if d.get('course_id') else [])
            catalog.update(ids)
            if d.get('name') == direction:
                selected = ids
    if direction and selected is None:
        raise GroupError('方向不在本营配置中', status=400)
    if course_ids is not None and not isinstance(course_ids, list):
        raise GroupError('course_ids 须为数组', status=400)
    raw_ids = course_ids if course_ids is not None else selected or []
    if any(type(cid) is not int for cid in raw_ids):
        raise GroupError('课程 ID 须为整数', status=400)
    ids = list(dict.fromkeys(raw_ids))
    if any(type(cid) is not int or cid not in catalog for cid in ids):
        raise GroupError('课程不在本营方向目录中', status=400)
    return ids


def create_group(sid, *, actor_id, mentor_id, name, direction=None, course_ids=None, capacity=None, recruitment=False):
    require_manager(sid, actor_id)
    GroupService._lock_users(actor_id, mentor_id)
    camp = lock_camp(sid)
    if capacity is not None and (type(capacity) is not int or not 1 <= capacity <= 30):
        raise GroupError('容量须为 1 至 30 或空', status=400)
    if not CampMember.query.filter_by(camp_session_id=sid, user_id=mentor_id).first():
        raise GroupError('请先将导生加入营期', status=409)
    ids = validate_courses(camp, direction, course_ids)
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 100:
        raise GroupError('组名不能为空且不超过 100 字', status=400)
    name = name.strip()
    unit = CampUnit(camp_session_id=sid, unit_type='mentor_team', name=name,
                    owner_user_id=mentor_id, status='active')
    db.session.add(unit)
    db.session.flush()
    result = core.create_group(name=name, owner_user_id=mentor_id, context=GovernanceContext('training', unit.id),
        actor_user_id=actor_id, idempotency_key=str(uuid4()), reason='创建培训组')
    unit.group_id = result['group']['id']
    profile = TrainingGroupProfile(unit_id=unit.id, camp_session_id=sid, direction=direction,
        course_ids=ids, capacity=capacity, recruitment_mentor_id=mentor_id if recruitment else None)
    db.session.add(profile)
    db.session.flush()
    core.grant_role(user_id=mentor_id, role='mentor', **command(unit, actor_id, '任命培训导生'))
    return unit


def recruitment_group(sid, mentor_id, actor_id):
    profile = TrainingGroupProfile.query.filter_by(camp_session_id=sid, recruitment_mentor_id=mentor_id).first()
    if profile:
        return db.session.get(CampUnit, profile.unit_id)
    user = db.session.get(UserModel, mentor_id)
    old = CampMentorProfile.query.filter_by(camp_session_id=sid, user_id=mentor_id).first()
    tags = json.loads(old.tags or '[]') if old else []
    return create_group(sid, actor_id=actor_id, mentor_id=mentor_id,
                        name=f'{user.username}的招生组 #{mentor_id}', direction=tags[0] if tags else None,
                        capacity=old.capacity if old else None, recruitment=True)


def assign_course(unit, uid, cid, actor_id, *, transfer=False, source='group'):
    camp = db.session.get(CampSession, unit.camp_session_id)
    if unit.id not in access.learner_units(camp.id, uid):
        raise GroupError('学员不在目标学习组', status=409)
    profile = db.session.get(TrainingGroupProfile, unit.id)
    if cid not in (profile.course_ids or []):
        raise GroupError('课程不在本组教学配置中', status=400)
    row = CampCourseAssignment.query.filter_by(camp_session_id=camp.id,
        student_user_id=uid, course_id=cid).populate_existing().with_for_update().first()
    if row and row.status == 'active' and row.unit_id and row.unit_id != unit.id and not transfer:
        raise GroupError('同营同课已归属另一学习组，请明确转交课程责任', machine='COURSE_GROUP_CONFLICT')
    before = {'unit_id': row.unit_id, 'status': row.status} if row else None
    if not row:
        row = CampCourseAssignment(camp_session_id=camp.id, student_user_id=uid, course_id=cid,
            source_type=source, source_ref_id=actor_id, status='active')
        db.session.add(row)
    elif row.status != 'active':
        row.status, row.ended_at, row.assigned_at = 'active', None, datetime.now()
    row.unit_id = unit.id
    uc = UserCourseModel.query.filter_by(user_id=uid, course_id=cid).first()
    if not uc:
        db.session.add(UserCourseModel(user_id=uid, course_id=cid, status='active'))
    elif uc.status == 'dropped':
        uc.status = 'active'
    if before != {'unit_id': row.unit_id, 'status': 'active'}:
        record(camp.id, uid, actor_id, 'course_assignment', before, {'unit_id': unit.id, 'status': 'active'}, cid)
    return row


def add_role(unit, uid, role, actor_id, *, course_id=None, attendance_required=None, selection=False, transfer_courses=False):
    if not selection:
        require_manager(unit.camp_session_id, actor_id)
    GroupService._lock_users(actor_id, uid)
    lock_camp(unit.camp_session_id)
    if role not in ('learner', 'mentor'):
        raise GroupError('职责仅支持 learner/mentor', status=400)
    member = CampMember.query.filter_by(camp_session_id=unit.camp_session_id, user_id=uid).first()
    if not member:
        raise GroupError('请先加入营期', status=409)
    profile = db.session.get(TrainingGroupProfile, unit.id)
    existing = any(r.role == role and u.id == unit.id for r, u in access.roles(unit.camp_session_id, uid))
    if role == 'learner' and not existing and profile.capacity is not None and not selection:
        count = len({m.user_id for r, m, u in access.role_query(unit.camp_session_id, role='learner').all() if u.id == unit.id})
        if count >= profile.capacity:
            raise GroupError('小组名额已满', machine='GROUP_CAPACITY_FULL')
    core.add_member(user_id=uid, **command(unit, actor_id, '培训组加入成员'))
    core.grant_role(user_id=uid, role=role, scope_type='course' if course_id else 'group',
                    scope_id=course_id or 0, **command(unit, actor_id, '授予培训职责'))
    if role == 'learner':
        member.learning_enabled = True
        if attendance_required is not None:
            if type(attendance_required) is not bool:
                raise GroupError('考勤义务须为布尔值', status=400)
            before = member.needs_attendance
            member.attendance_required = attendance_required
            record(unit.camp_session_id, uid, actor_id, 'attendance_obligation', {'required': before}, {'required': attendance_required})
        for cid in ([course_id] if course_id else profile.course_ids or []):
            assign_course(unit, uid, cid, actor_id, transfer=transfer_courses)


def remove_role(unit, uid, role, actor_id, *, course_id=None, selection=False):
    if not selection:
        require_manager(unit.camp_session_id, actor_id)
    GroupService._lock_users(actor_id, uid)
    lock_camp(unit.camp_session_id)
    core.end_role(user_id=uid, role=role, scope_type='course' if course_id else 'group', scope_id=course_id or 0,
                  **command(unit, actor_id, '结束培训职责'))
    if role == 'learner':
        for row in CampCourseAssignment.query.filter_by(camp_session_id=unit.camp_session_id,
                student_user_id=uid, unit_id=unit.id, status='active').all():
            if (course_id is None or course_id == row.course_id) and not any(r.role == 'learner' for r, u in access.roles(unit.camp_session_id, uid, unit_id=unit.id, course_id=row.course_id)):
                row.unit_id = None
                record(unit.camp_session_id, uid, actor_id, 'course_unassigned', {'unit_id': unit.id}, {'unit_id': None}, row.course_id)
    # 双职责仅结束所选职责；治理负责人参与关系必须保留至显式转交。
    if not access.roles(unit.camp_session_id, uid, unit_id=unit.id) and unit.owner_user_id != uid:
        core.end_membership(user_id=uid, **command(unit, actor_id, '结束无剩余职责的组参与'))


def recruit(sid, uid, mentor_id, actor_id, *, source='admin', allow_over=False, daily=False):
    """单轮招生位置。营期行锁串行化容量、抢人和改派；组操作与业务账本同事务。"""
    actor = db.session.get(UserModel, actor_id)
    require_live_account(actor)
    lock_users([actor_id, uid, mentor_id])
    camp = lock_camp(sid)
    manager = managed(sid, actor)
    if not daily and (camp.status != 'selecting' or not camp.ms_preference_deadline or datetime.now() < camp.ms_preference_deadline):
        raise GroupError('选导生仅在志愿截止后的选组期开放', status=409)
    student = CampMember.query.filter_by(camp_session_id=sid, user_id=uid).populate_existing().with_for_update().first()
    if not student or not student.is_learner:
        raise GroupError('目标不是本营学习成员', status=404)
    if uid == mentor_id:
        raise GroupError('不能选择自己作为招生导生', status=400)
    ledger = CampMentorMatch.query.filter_by(camp_session_id=sid, student_user_id=uid).first()
    if not manager:
        if (source != 'mentor_pick' or camp.status != 'selecting' or not camp.mentor_selection_enabled or
                not camp.ms_preference_deadline or datetime.now() < camp.ms_preference_deadline or
                not access.has_role(sid, actor_id, 'mentor')):
            raise GroupError('不在导生选人权限或窗口内', status=403)
        if mentor_id and mentor_id != actor_id:
            raise GroupError('仅可选择加入自己的招生组', status=403)
        if student.recruitment_unit_id and mentor_id:
            raise GroupError('学员已有招生归属', machine='RECRUITMENT_CONFLICT')
        if not mentor_id and (not ledger or ledger.source != 'mentor_pick' or ledger.mentor_user_id != actor_id):
            raise GroupError('只能释放本人勾选的学员', status=403)
    old_id = student.recruitment_unit_id
    unit = recruitment_group(sid, mentor_id, actor_id) if mentor_id else None
    if unit and old_id == unit.id:
        return unit
    if unit:
        profile = CampMentorProfile.query.filter_by(camp_session_id=sid, user_id=mentor_id).populate_existing().with_for_update().first()
        cap = profile.capacity if profile else (None if daily else 0)
        count = len({m.user_id for r, m, u in access.role_query(sid, role='learner').all() if u.id == unit.id})
        if cap is not None and count >= cap and not (manager and allow_over):
            raise GroupError('招生名额已满', machine='GROUP_CAPACITY_FULL')
    if old_id:
        old = db.session.get(CampUnit, old_id)
        token = _selection.set((actor_id, old.id, uid))
        try:
            remove_role(old, uid, 'learner', actor_id, selection=not manager)
        finally:
            _selection.reset(token)
    if unit:
        token = _selection.set((actor_id, unit.id, uid))
        try:
            add_role(unit, uid, 'learner', actor_id, selection=True)
        finally:
            _selection.reset(token)
    student.recruitment_unit_id = unit.id if unit else None
    # team_mentor_id 兼容属性从绑定推导，不双写历史列。
    if ledger and not mentor_id:
        db.session.delete(ledger)
    elif mentor_id:
        if not ledger:
            ledger = CampMentorMatch(camp_session_id=sid, student_user_id=uid, mentor_user_id=mentor_id)
            db.session.add(ledger)
        ledger.mentor_user_id, ledger.source, ledger.round = mentor_id, source, None
    record(sid, uid, actor_id, 'recruitment', {'unit_id': old_id}, {'unit_id': unit.id if unit else None,
           'source': source, 'allow_over': bool(allow_over)}, reason='调整招生位置')
    return unit


def sync_recruitment_profile(sid, mentor_id, actor_id, direction, course_ids, capacity):
    profile = TrainingGroupProfile.query.filter_by(camp_session_id=sid, recruitment_mentor_id=mentor_id).first()
    if not profile:
        return
    camp = lock_camp(sid)
    unit = db.session.get(CampUnit, profile.unit_id)
    learners_now = {m.user_id for r, m, u in access.role_query(sid, role='learner').all() if u.id == unit.id}
    if capacity is not None and capacity < len(learners_now):
        raise GroupError('容量不能低于组内当前学习人数', machine='GROUP_CAPACITY_FULL')
    profile.direction, profile.course_ids, profile.capacity = direction, validate_courses(camp, direction, course_ids), capacity
    detach_removed_courses(unit, profile.course_ids, actor_id)
    # 旧课程保留；新增课只为当前学习职责补入，冲突由同营同课约束明确拒绝。
    for uid in learners_now:
        for cid in profile.course_ids:
            assign_course(unit, uid, cid, actor_id, source='direction')


def end_participation(sid, uid, actor_id):
    require_manager(sid, actor_id)
    GroupService._lock_users(actor_id, uid)
    lock_camp(sid)
    units = CampUnit.query.join(GroupMembership, GroupMembership.group_id == CampUnit.group_id).filter(
        CampUnit.camp_session_id == sid, CampUnit.unit_type == 'mentor_team',
        GroupMembership.user_id == uid, GroupMembership.status == 'active').all()
    if any(u.owner_user_id == uid for u in units):
        raise GroupError('请先转交其负责的小组再退营', machine='GROUP_OWNER_TRANSFER_REQUIRED')
    # 招生导生退营须释放真实绑定，不能只清理已退为档案的 team_mentor_id 列。
    for profile in TrainingGroupProfile.query.filter_by(camp_session_id=sid, recruitment_mentor_id=uid).all():
        students = CampMember.query.filter_by(camp_session_id=sid, recruitment_unit_id=profile.unit_id).all()
        for student in students:
            recruit(sid, student.user_id, None, actor_id, daily=True)
        profile.recruitment_mentor_id = None
    for unit in units:
        core.end_membership(user_id=uid, **command(unit, actor_id, '成员退出培训营'))
    member = CampMember.query.filter_by(camp_session_id=sid, user_id=uid).first()
    if member:
        member.recruitment_unit_id = None


def detach_removed_courses(unit, current_ids, actor_id):
    for row in CampCourseAssignment.query.filter_by(camp_session_id=unit.camp_session_id, unit_id=unit.id, status='active').all():
        if row.course_id not in current_ids:
            row.unit_id = None
            record(unit.camp_session_id, row.student_user_id, actor_id, 'course_unassigned',
                   {'unit_id': unit.id}, {'unit_id': None}, row.course_id, reason='课程移出本组配置，原修读保留待分配责任')

"""培训组管理和明确的课程责任转交入口。"""
from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required
from exts import db
from models import (CampSession, CampUnit, CampMember, TrainingGroupProfile, UserModel,
                    GroupMembership, CampCourseAssignment, CampGroup)
from . import _current_user
from .camp_staff import camp_access
from services.camp_groups import training as service, training_access as access
from services.camp_groups import training_history as history
from services.groups import GroupError

bp = Blueprint('camp_training', __name__, url_prefix='/camp')


@bp.errorhandler(GroupError)
def error(exc):
    db.session.rollback()
    return jsonify(code=exc.status, message=str(exc), machine=exc.machine), exc.status


def positive(data, field):
    value = data.get(field)
    if type(value) is not int or value <= 0:
        raise GroupError(field + ' 须为正整数', status=400)
    return value


def unit_at(sid, unit_id):
    unit = db.session.get(CampUnit, unit_id)
    if not unit or unit.camp_session_id != sid or unit.unit_type != 'mentor_team' or not unit.group_id:
        raise GroupError('培训组不存在', status=404)
    return unit


@bp.route('/sessions/<int:sid>/training-groups')
@jwt_required()
def groups(sid):
    user = _current_user()
    camp = db.session.get(CampSession, sid)
    if not camp or camp.status == 'deleted':
        raise GroupError('营期不存在', status=404)
    is_manager = service.managed(sid, user)
    own_roles = access.roles(sid, user.id) if access.enabled(sid) else []
    own = {u.id for _, u in own_roles}
    member = CampMember.query.filter_by(camp_session_id=sid, user_id=user.id).first()
    if not is_manager and not member:
        raise GroupError('需要本营参与资格', status=403)
    rows = []
    for profile in TrainingGroupProfile.query.filter_by(camp_session_id=sid).all():
        unit = unit_at(sid, profile.unit_id)
        if not is_manager and unit.id not in own:
            continue
        assignments = access.role_query(sid, include_paused=True).filter(CampUnit.id == unit.id).all()
        users = {m.user_id for _, m, _ in assignments}
        names = {u.id: u.username for u in UserModel.query.filter(UserModel.id.in_(users)).all()} if users else {}
        rows.append(dict(unit_id=unit.id, name=unit.name, status=unit.status,
            owner_user_id=unit.owner_user_id, version=unit.camp_group.version,
            direction=profile.direction, course_ids=profile.course_ids, capacity=profile.capacity,
            recruitment_mentor_id=profile.recruitment_mentor_id,
            my_roles=sorted({r.role for r, u in own_roles if u.id == unit.id}),
            members=[dict(user_id=uid, username=names.get(uid, ''), roles=[dict(role=r.role,
                         scope_type=r.scope_type, scope_id=r.scope_id) for r, m, _ in assignments if m.user_id == uid])
                     for uid in sorted(users)]))
        if is_manager:
            rows[-1]['members'] = history.group_roster(unit)
        rows[-1]['history_included'] = is_manager
    return jsonify(code=200, groups=rows, ready=bool(camp.training_groups_ready))


@bp.route('/sessions/<int:sid>/training-groups', methods=['POST'])
@jwt_required()
@camp_access('member.manage')
def create(sid):
    data = request.get_json(silent=True) or {}
    with db.session.begin_nested():
        unit = service.create_group(sid, actor_id=_current_user().id,
            mentor_id=positive(data, 'mentor_user_id'), name=data.get('name'), direction=data.get('direction'),
            course_ids=data.get('course_ids'), capacity=data.get('capacity'))
    db.session.commit()
    return jsonify(code=200, unit_id=unit.id)


@bp.route('/sessions/<int:sid>/training-groups/<int:unit_id>/members', methods=['POST', 'DELETE'])
@jwt_required()
@camp_access('member.manage')
def members(sid, unit_id):
    data = request.get_json(silent=True) or {}
    uid = positive(data, 'user_id')
    role = data.get('role')
    if role not in ('learner', 'mentor'):
        raise GroupError('请选择学习或带教职责', status=400)
    course_id = positive(data, 'course_id') if data.get('course_id') is not None else None
    with db.session.begin_nested():
        unit = unit_at(sid, unit_id)
        service.lock_camp(sid)
        if data.get('expected_version') != unit.camp_group.version:
            raise GroupError('小组已变化，请刷新后重试', machine='GROUP_VERSION_CONFLICT')
        if request.method == 'POST':
            profile = db.session.get(TrainingGroupProfile, unit.id)
            if role == 'learner' and profile.recruitment_mentor_id:
                raise GroupError('招生组学员请通过归属导生指派入口添加', status=409)
            service.add_role(unit, uid, role, _current_user().id, course_id=course_id,
                             attendance_required=data.get('attendance_required'), transfer_courses=data.get('transfer_courses') is True)
        else:
            # 旧招生位置必须通过其专用改派入口，避免只改组却留下招生名单。
            member = CampMember.query.filter_by(camp_session_id=sid, user_id=uid).first()
            if role == 'learner' and member and member.recruitment_unit_id == unit.id:
                raise GroupError('此职责来自选导生，请使用归属导生改派或释放入口', status=409)
            service.remove_role(unit, uid, role, _current_user().id, course_id=course_id)
    db.session.commit()
    return jsonify(code=200, message='已更新职责')


@bp.route('/sessions/<int:sid>/training-groups/<int:unit_id>', methods=['PUT'])
@jwt_required()
@camp_access('member.manage')
def update(sid, unit_id):
    data = request.get_json(silent=True) or {}
    actor = _current_user()
    with db.session.begin_nested():
        if 'owner_user_id' in data:
            service.core._lock_users(actor.id, positive(data, 'owner_user_id'))
        camp = service.lock_camp(sid)
        unit = unit_at(sid, unit_id)
        group = CampGroup.query.filter_by(id=unit.group_id).populate_existing().with_for_update().one()
        if data.get('expected_version') != group.version:
            raise GroupError('小组已变化，请刷新后重试', machine='GROUP_VERSION_CONFLICT')
        profile = db.session.get(TrainingGroupProfile, unit_id)
        if profile.recruitment_mentor_id and any(k in data for k in ('owner_user_id', 'direction', 'course_ids', 'capacity', 'status')):
            raise GroupError('招生组请通过名片和招生管理调整；更换主招生导生须先释放招生位置', status=409)
        if 'status' in data and data['status'] not in ('active', 'paused'):
            raise GroupError('状态须为 active/paused', status=400)
        # 恢复与配置共用事务；先恢复才能通过组治理的写入门禁，失败时一起回滚。
        if data.get('status') == 'active':
            unit.status = 'active'
            db.session.flush()
        if 'owner_user_id' in data and data['owner_user_id'] != unit.owner_user_id:
            service.core.transfer_owner(user_id=data['owner_user_id'], **service.command(unit, actor.id, '转交组治理负责人'))
        if 'name' in data and data['name'] != unit.name:
            service.core.rename_group(name=data['name'], **service.command(unit, actor.id, '调整培训组名称'))
        if 'course_ids' in data or 'direction' in data:
            direction = data.get('direction', profile.direction)
            courses = service.validate_courses(camp, direction, data.get('course_ids', profile.course_ids))
            if direction != profile.direction or courses != profile.course_ids:
                if unit.status != 'active':
                    raise GroupError('请先恢复小组，再调整教学配置', machine='GROUP_READ_ONLY')
                profile.direction, profile.course_ids = direction, courses
                service.detach_removed_courses(unit, profile.course_ids, actor.id)
                for uid in {m.user_id for r, m, u in access.role_query(sid, role='learner').all() if u.id == unit_id}:
                    for cid in profile.course_ids:
                        service.assign_course(unit, uid, cid, actor.id)
        if 'capacity' in data:
            cap = data['capacity']
            count = len({m.user_id for r, m, u in access.role_query(sid, role='learner', include_paused=True).all() if u.id == unit_id})
            if cap is not None and (type(cap) is not int or not max(1, count) <= cap <= 30):
                raise GroupError('容量须在 1 至 30 之间，且不低于当前学习人数', status=400)
            profile.capacity = cap
        if 'status' in data:
            unit.status = data['status']
        # 配置也参与组版本控制，即使只改课程/容量也不能绕过并发检查。
        group.version += 1
        service.record(sid, actor.id, actor.id, 'group_configuration', after={'unit_id': unit_id, 'changes': data})
    db.session.commit()
    return jsonify(code=200, message='已更新小组')


@bp.route('/sessions/<int:sid>/course-assign/transfer', methods=['POST'])
@jwt_required()
@camp_access('member.manage')
def transfer_course(sid):
    data = request.get_json(silent=True) or {}
    uid, cid, target = (positive(data, key) for key in ('user_id', 'course_id', 'unit_id'))
    with db.session.begin_nested():
        service.core._lock_users(_current_user().id, uid)
        service.lock_camp(sid)
        row = CampCourseAssignment.query.filter_by(camp_session_id=sid, student_user_id=uid,
            course_id=cid, status='active').populate_existing().with_for_update().first()
        if not row or row.unit_id != data.get('from_unit_id'):
            raise GroupError('课程责任已变化，请刷新后重试', machine='COURSE_GROUP_CONFLICT')
        service.assign_course(unit_at(sid, target), uid, cid, _current_user().id, transfer=True)
    db.session.commit()
    return jsonify(code=200, message='课程责任已转交，原进度与成绩保留')


def progress_board_data(camp, user, page, page_size, mentor_filter):
    """按修读责任组分桶；同一课程分配只计一次，全营学员人数按人去重。"""
    from .camp import _team_progress_ctx, _course_block
    is_manager = service.managed(camp.id, user)
    historical = history.include_history(camp, user, request.args.get('include_history') == '1')
    roster = {m.user_id:m for m in history.members(camp.id, history=historical)}
    historical_mentors = history.mentor_units(camp.id) if historical else {}
    mentor_units = {u.id for r, u in access.roles(camp.id, user.id) if r.role == 'mentor'}
    all_units = {u.id: u for u in CampUnit.query.filter_by(camp_session_id=camp.id, unit_type='mentor_team').all()}
    profiles = {p.unit_id: p for p in TrainingGroupProfile.query.filter_by(camp_session_id=camp.id).all()}
    if mentor_filter not in (None, 'unassigned'):
        if not access.has_role(camp.id, mentor_filter, 'mentor') and mentor_filter not in historical_mentors:
            raise GroupError('该导生不在本营', status=400)
        filtered = historical_mentors.get(mentor_filter, set()) if historical else {u.id for r, u in access.roles(camp.id, mentor_filter) if r.role == 'mentor'}
    else:
        filtered = None
    assignment_query = CampCourseAssignment.query.filter_by(camp_session_id=camp.id)
    assignments = (assignment_query if historical else assignment_query.filter_by(status='active')).all()
    active_students = {m.user_id for m in roster.values() if m.is_learner}
    if historical:
        active_students |= {a.student_user_id for a in assignments}
    assignment_map = {(a.student_user_id,a.course_id):a for a in assignments}
    buckets = {}
    for row in assignments:
        if row.student_user_id not in active_students:
            continue
        unit_id = row.unit_id
        if not is_manager and (unit_id not in mentor_units or not access.can_teach(camp.id, user.id, row.student_user_id, row.course_id)):
            continue
        if mentor_filter == 'unassigned' and unit_id is not None or filtered is not None and unit_id not in filtered:
            continue
        buckets.setdefault(unit_id, []).append(row)
    if is_manager and mentor_filter in (None, 'unassigned'):
        represented = {a.student_user_id for a in assignments}
        if active_students - represented:
            buckets.setdefault(None, [])
    uids = {r.student_user_id for rows in buckets.values() for r in rows}
    empty_students = active_students - {a.student_user_id for a in assignments} if None in buckets else set()
    uids |= empty_students
    cids = {r.course_id for rows in buckets.values() for r in rows}
    ctx = _team_progress_ctx(camp, list(uids), list(cids))
    names = {u.id:u.username for u in UserModel.query.filter(UserModel.id.in_(uids | {u.owner_user_id for u in all_units.values()})).all()}
    groups, cert_count, chapter_count, complete_count = [], 0, 0, 0
    for unit_id in sorted(buckets, key=lambda uid: (uid is None, uid or 0)):
        rows = buckets[unit_id]
        unit = all_units.get(unit_id)
        profile = profiles.get(unit_id)
        course_ids = sorted({r.course_id for r in rows})
        per_user = {}
        for row in rows:
            per_user.setdefault(row.student_user_id, []).append(row.course_id)
        if unit_id is None:
            for uid in empty_students:
                per_user.setdefault(uid, [])
        students, gc, gt = [], 0, 0
        for uid, assigned in per_user.items():
            blocks = [_course_block(camp, cid, uid, ctx) for cid in assigned]
            for block in blocks:
                block['assignment_status'] = assignment_map[(uid,block['course_id'])].status
            sc = sum(b['certified_chapters'] for b in blocks)
            st = sum(b['total_chapters'] for b in blocks)
            gc += sc; gt += st
            complete_count += sum(b['course_status'] == 'completed' for b in blocks)
            assigned_blocks = {b['course_id']:b for b in blocks}
            blocks = [assigned_blocks.get(cid, {'course_id':cid, 'course_status':None, 'total_chapters':0, 'certified_chapters':0, 'chapters':[], 'not_assigned':True}) for cid in course_ids]
            students.append(dict(student_user_id=uid, username=names.get(uid,''),
                membership_status=roster[uid].status if uid in roster else 'removed',
                certified_rate=round(100*sc/st) if st else None, courses=blocks))
        cert_count += gc; chapter_count += gt
        groups.append(dict(unit_id=unit_id, mentor_user_id=unit.owner_user_id if unit else None,
            mentor_name=unit.name if unit else None, direction=profile.direction if profile else None,
            hint='课程责任待分配' if not unit else None,
            courses=[{'course_id':cid, 'course_title':ctx['courses'][cid].title if ctx['courses'].get(cid) else ''} for cid in course_ids],
            students=students, certified_rate=round(100*gc/gt) if gt else None))
    mentors = [{'mentor_user_id': m.user_id, 'mentor_name': db.session.get(UserModel,m.user_id).username,
                'direction': None} for m in CampMember.query.filter_by(camp_session_id=camp.id,is_mentor=True).all()]
    if historical:
        mentors = [dict(mentor_user_id=u.id,mentor_name=u.username,direction=None) for u in UserModel.query.filter(UserModel.id.in_(historical_mentors)).order_by(UserModel.id)]
    return dict(code=200, include_history=historical, can_include_history=is_manager, groups=groups[(page-1)*page_size:page*page_size], mentors=mentors,
        total=len(groups), page=page, page_size=page_size,
        summary=dict(scope='all' if mentor_filter is None else 'mentor', scope_label='全营' if mentor_filter is None else '筛选范围',
            group_count=sum(g['unit_id'] is not None for g in groups), student_count=len(uids),
            certified_chapters=cert_count, total_chapters=chapter_count, completed_courses=complete_count,
            certified_rate=round(100*cert_count/chapter_count) if chapter_count else None))

"""营期（Camp）系统蓝图。

角色门控（@camp_role）：
  - 营期管理（建营/成员/课程/出勤计划/座位）→ teacher+
  - 导生可操作（请假审批/发奖励）→ mentor+ 且仅限本团队（内部校验）
  - 学员操作（选课/请假）→ 仅营期成员
营期看板（混合考勤算法）见 Phase D 的 /camp/attendance/dashboard/<sid>。
"""
from collections import defaultdict, Counter
from datetime import date, time, timedelta, datetime

from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required

from exts import db
from models import (
    CampSession, CampMember, CampCourse, CampAttendancePlan,
    CampSeat, CampLeave, CheckRecord, CourseModel, UserCourseModel,
    MedalModel, MedalUserModel, UserModel, SeatModel,
)

from . import camp_role, audit_log, _current_user
from .notification import create_notification

bp = Blueprint("camp", __name__, url_prefix="/camp")


# ─────────────────────────────────────────────
# 辅助
# ─────────────────────────────────────────────

def _weekdays(start, end):
    """[start, end] 日期列表"""
    days, d = [], start
    while d <= end:
        days.append(d)
        d += timedelta(days=1)
    return days


def _gen_plan(camp, user_id):
    """为某学员按营期范围(×工作日)生成/补齐承诺出勤日。幂等。"""
    days = _weekdays(camp.start_date, camp.end_date)
    if camp.weekdays_only:
        days = [d for d in days if d.weekday() < 5]   # 周一~周五
    exist = {p.date for p in CampAttendancePlan.query.filter_by(
        camp_session_id=camp.id, user_id=user_id).all()}
    for d in days:
        if d not in exist:
            db.session.add(CampAttendancePlan(
                camp_session_id=camp.id, user_id=user_id, date=d,
                expected_check_in=camp.expected_check_in,
                min_daily_hours=camp.min_daily_hours,
            ))


def _visible_student_ids(camp_id, user):
    """导生=本团队学员；老师/超管=全营学员。"""
    if user.is_admin_like() or user.role == 'teacher':
        return [m.user_id for m in CampMember.query.filter_by(
            camp_session_id=camp_id, role='student').all()]
    return [m.user_id for m in CampMember.query.filter_by(
        camp_session_id=camp_id, role='student', team_mentor_id=user.id).all()]


def _in_my_team(camp_id, mentor, student_id):
    """student_id 是否是该 mentor 在本营的团队成员"""
    return CampMember.query.filter_by(
        camp_session_id=camp_id, role='student',
        user_id=student_id, team_mentor_id=mentor.id).first() is not None


def _eval_day(records, plan, on_leave):
    """对某学员某承诺日的 CheckRecord 列表算混合考勤状态（纯函数，可单测）。

    records : 该 (user,date) 的 CheckRecord 列表（可能为空）
    plan    : CampAttendancePlan（冗余 expected_check_in / min_daily_hours）
    on_leave: 该日是否命中已批准请假
    返回: {status, is_late, is_sufficient, first_check_in, total_hours, in_progress}
    status ∈ present / late / short_hours / late_and_short / absent / on_leave
    """
    if on_leave:
        return {"status": "on_leave", "is_late": None, "is_sufficient": None,
                "first_check_in": None, "total_hours": None, "in_progress": None}
    if not records:
        return {"status": "absent", "is_late": None, "is_sufficient": None,
                "first_check_in": None, "total_hours": 0, "in_progress": False}
    total = sum(r.duration or 0 for r in records)            # None 段按 0（未签退/脏段）
    ins = [r.check_in for r in records if r.check_in is not None]
    first = min(ins).time() if ins else None
    in_progress = any(r.check_out is None for r in records)  # 当日有未签退段
    # 迟到维度（无容忍）；expected_check_in 为 None → 不判迟到
    is_late = bool(first is not None and plan.expected_check_in is not None
                   and first > plan.expected_check_in)
    # 达标维度；min_daily_hours 为 None → 不判达标（视作达标）
    is_sufficient = (total >= plan.min_daily_hours) if plan.min_daily_hours else True
    if is_late and not is_sufficient:
        status = "late_and_short"
    elif is_late:
        status = "late"
    elif not is_sufficient:
        status = "short_hours"
    else:
        status = "present"
    return {"status": status, "is_late": is_late, "is_sufficient": is_sufficient,
            "first_check_in": min(ins).isoformat() if ins else None,
            "total_hours": round(total, 2), "in_progress": in_progress}


def _approved_leave_dates(camp_id, frm, to):
    """该营已批准、与 [frm,to] 相交的请假段 → set[(user_id, date)]。"""
    leaves = CampLeave.query.filter(
        CampLeave.camp_session_id == camp_id,
        CampLeave.status == "approved",
        CampLeave.start_date <= to,
        CampLeave.end_date >= frm).all()
    leave_set = set()
    for lv in leaves:
        d = lv.start_date
        while d <= lv.end_date and d <= to:
            if d >= frm:
                leave_set.add((lv.user_id, d))
            d += timedelta(days=1)
    return leave_set


def _session_dict(c):
    return {
        "id": c.id, "name": c.name, "camp_type": c.camp_type,
        "start_date": c.start_date.isoformat(), "end_date": c.end_date.isoformat(),
        "status": c.status,
        "expected_check_in": c.expected_check_in.isoformat() if c.expected_check_in else None,
        "min_daily_hours": c.min_daily_hours, "weekdays_only": c.weekdays_only,
        "member_count": CampMember.query.filter_by(camp_session_id=c.id).count(),
    }


# ─────────────────────────────────────────────
# 营期 CRUD
# ─────────────────────────────────────────────

@bp.route("/sessions", methods=["POST"])
@jwt_required()
@camp_role('teacher', 'super_admin')
@audit_log(operation="创建营期")
def session_create():
    d = request.json or {}
    name, start, end = d.get("name"), d.get("start_date"), d.get("end_date")
    if not name or not start or not end:
        return jsonify({"code": 400, "message": "缺少 name/start_date/end_date"}), 400
    try:
        camp = CampSession(
            name=name, camp_type=d.get("camp_type", "short_term"),
            start_date=date.fromisoformat(start), end_date=date.fromisoformat(end),
            expected_check_in=time.fromisoformat(d["expected_check_in"]) if d.get("expected_check_in") else None,
            min_daily_hours=d.get("min_daily_hours"),
            weekdays_only=d.get("weekdays_only", True),
        )
    except (ValueError, TypeError) as e:
        return jsonify({"code": 400, "message": f"参数格式错误: {e}"}), 400
    db.session.add(camp)
    db.session.commit()
    return jsonify({"code": 200, "message": "创建成功", "session_id": camp.id,
                    "session": _session_dict(camp)})


@bp.route("/sessions")
@jwt_required()
def session_list():
    user = _current_user()
    q = CampSession.query
    if not (user.is_admin_like() or user.role == 'teacher'):
        ids = [m.camp_session_id for m in CampMember.query.filter_by(user_id=user.id).all()]
        q = q.filter(CampSession.id.in_(ids)) if ids else q.filter(False)
    camps = q.order_by(CampSession.start_date.desc()).all()
    return jsonify({"code": 200, "sessions": [_session_dict(c) for c in camps]})


@bp.route("/sessions/<int:sid>")
@jwt_required()
def session_detail(sid):
    camp = CampSession.query.get(sid)
    if not camp:
        return jsonify({"code": 404, "message": "营期不存在"}), 404
    return jsonify({"code": 200, "session": _session_dict(camp)})


@bp.route("/sessions/<int:sid>", methods=["PUT"])
@jwt_required()
@camp_role('teacher', 'super_admin')
@audit_log(operation="修改营期")
def session_update(sid):
    camp = CampSession.query.get(sid)
    if not camp:
        return jsonify({"code": 404, "message": "营期不存在"}), 404
    d = request.json or {}
    for f in ["name", "camp_type", "status", "weekdays_only", "min_daily_hours"]:
        if f in d:
            setattr(camp, f, d[f])
    try:
        if d.get("start_date"):
            camp.start_date = date.fromisoformat(d["start_date"])
        if d.get("end_date"):
            camp.end_date = date.fromisoformat(d["end_date"])
        if d.get("expected_check_in"):
            camp.expected_check_in = time.fromisoformat(d["expected_check_in"])
    except (ValueError, TypeError) as e:
        return jsonify({"code": 400, "message": f"参数格式错误: {e}"}), 400
    db.session.commit()
    return jsonify({"code": 200, "message": "已更新", "session": _session_dict(camp)})


# ─────────────────────────────────────────────
# 成员
# ─────────────────────────────────────────────

@bp.route("/sessions/<int:sid>/members", methods=["POST"])
@jwt_required()
@camp_role('teacher', 'super_admin')
@audit_log(operation="分配营期成员")
def member_assign(sid):
    camp = CampSession.query.get(sid)
    if not camp:
        return jsonify({"code": 404, "message": "营期不存在"}), 404
    d = request.json or {}
    user_id, role = d.get("user_id"), d.get("role", "student")
    team_mentor_id = d.get("team_mentor_id")
    if not user_id:
        return jsonify({"code": 400, "message": "缺少 user_id"}), 400
    if not UserModel.query.get(user_id):
        return jsonify({"code": 404, "message": "用户不存在"}), 404
    if CampMember.query.filter_by(camp_session_id=sid, user_id=user_id).first():
        return jsonify({"code": 402, "message": "该用户已在营期中"}), 402
    m = CampMember(camp_session_id=sid, user_id=user_id, role=role,
                   team_mentor_id=team_mentor_id if role == 'student' else None)
    db.session.add(m)
    if role == 'student':
        _gen_plan(camp, user_id)          # 学员加入即生成承诺出勤日
    db.session.commit()
    return jsonify({"code": 200, "message": "已加入", "member_id": m.id})


@bp.route("/sessions/<int:sid>/members")
@jwt_required()
def member_list(sid):
    user = _current_user()
    visible = set(_visible_student_ids(sid, user))
    see_all = user.is_admin_like() or user.role == 'teacher'
    data = []
    for m in CampMember.query.filter_by(camp_session_id=sid).all():
        if m.role == 'student' and not see_all and m.user_id not in visible:
            continue                        # 导生只看本团队
        u = UserModel.query.get(m.user_id)
        data.append({
            "user_id": m.user_id, "username": u.username if u else "",
            "role": m.role, "team_mentor_id": m.team_mentor_id,
            "joined_at": m.joined_at.isoformat() if m.joined_at else None,
        })
    return jsonify({"code": 200, "members": data})


@bp.route("/sessions/<int:sid>/members/<int:uid>", methods=["DELETE"])
@jwt_required()
@camp_role('teacher', 'super_admin')
@audit_log(operation="移除营期成员")
def member_remove(sid, uid):
    m = CampMember.query.filter_by(camp_session_id=sid, user_id=uid).first()
    if not m:
        return jsonify({"code": 404, "message": "成员不存在"}), 404
    db.session.delete(m)
    CampAttendancePlan.query.filter_by(camp_session_id=sid, user_id=uid).delete()
    db.session.commit()
    return jsonify({"code": 200, "message": "已移除"})


# ─────────────────────────────────────────────
# 课程目录 + 选课
# ─────────────────────────────────────────────

@bp.route("/sessions/<int:sid>/courses", methods=["POST"])
@jwt_required()
@camp_role('teacher', 'super_admin')
def course_add(sid):
    d = request.json or {}
    course_id = d.get("course_id")
    if not course_id or not CourseModel.query.get(course_id):
        return jsonify({"code": 404, "message": "课程不存在"}), 404
    if CampCourse.query.filter_by(camp_session_id=sid, course_id=course_id).first():
        return jsonify({"code": 402, "message": "课程已在营期中"}), 402
    db.session.add(CampCourse(camp_session_id=sid, course_id=course_id,
                              sort_order=d.get("sort_order", 0)))
    db.session.commit()
    return jsonify({"code": 200, "message": "已加入"})


@bp.route("/sessions/<int:sid>/courses")
@jwt_required()
def course_list(sid):
    ccs = CampCourse.query.filter_by(camp_session_id=sid).order_by(CampCourse.sort_order).all()
    data = []
    for cc in ccs:
        c = CourseModel.query.get(cc.course_id)
        if c:
            data.append({"course_id": c.id, "title": c.title, "difficulty": c.difficulty})
    return jsonify({"code": 200, "courses": data})


@bp.route("/selection", methods=["POST"])
@jwt_required()
def selection_pick():
    user = _current_user()
    d = request.json or {}
    sid, course_id = d.get("camp_session_id"), d.get("course_id")
    if not sid or not course_id:
        return jsonify({"code": 400, "message": "缺少 camp_session_id/course_id"}), 400
    if not CampMember.query.filter_by(camp_session_id=sid, user_id=user.id, role='student').first():
        return jsonify({"code": 403, "message": "非该营期学员"}), 403
    if not CampCourse.query.filter_by(camp_session_id=sid, course_id=course_id).first():
        return jsonify({"code": 404, "message": "营期未开放该课程"}), 404
    uc = UserCourseModel.query.filter_by(user_id=user.id, course_id=course_id).first()
    if not uc:
        uc = UserCourseModel(user_id=user.id, course_id=course_id, status='active')
        db.session.add(uc)
    uc.camp_session_id = sid                      # 标记营期选课（触发既有 LearningProgress）
    db.session.commit()
    return jsonify({"code": 200, "message": "选课成功"})


@bp.route("/selection/mine")
@jwt_required()
def selection_mine():
    user = _current_user()
    sid = request.args.get("camp_session_id", type=int)
    q = UserCourseModel.query.filter_by(user_id=user.id)
    if sid:
        q = q.filter_by(camp_session_id=sid)
    data = []
    for uc in q.all():
        c = CourseModel.query.get(uc.course_id)
        if c:
            data.append({"course_id": c.id, "title": c.title, "camp_session_id": uc.camp_session_id})
    return jsonify({"code": 200, "courses": data})


# ─────────────────────────────────────────────
# 出勤计划
# ─────────────────────────────────────────────

@bp.route("/attendance/plan/<int:sid>", methods=["POST"])
@jwt_required()
@camp_role('teacher', 'super_admin')
@audit_log(operation="重生成营期出勤计划")
def plan_regenerate(sid):
    camp = CampSession.query.get(sid)
    if not camp:
        return jsonify({"code": 404, "message": "营期不存在"}), 404
    for m in CampMember.query.filter_by(camp_session_id=sid, role='student').all():
        _gen_plan(camp, m.user_id)
    db.session.commit()
    cnt = CampAttendancePlan.query.filter_by(camp_session_id=sid).count()
    return jsonify({"code": 200, "message": "已重生成", "plan_count": cnt})


# ─────────────────────────────────────────────
# 考勤看板（混合双维度：迟到维度 + 当日时长达标维度）
# ─────────────────────────────────────────────

@bp.route("/attendance/dashboard/<int:sid>")
@jwt_required()
@camp_role('mentor', 'teacher', 'super_admin')
def attendance_dashboard(sid):
    """学生×承诺日 状态矩阵 + 汇总。
    导生=本团队；老师/超管=全营；学员被 @camp_role 拦截(403)。
    ?from=&to= 缺省=营期起止；聚合 CheckRecord 按 (user_id,date)，不依赖 camp_session_id。"""
    camp = CampSession.query.get(sid)
    if not camp:
        return jsonify({"code": 404, "message": "营期不存在"}), 404
    user = _current_user()

    # 1. 范围（缺省=营期范围）
    try:
        frm = date.fromisoformat(request.args.get("from")) if request.args.get("from") else camp.start_date
        to = date.fromisoformat(request.args.get("to")) if request.args.get("to") else camp.end_date
    except ValueError:
        return jsonify({"code": 400, "message": "日期格式错误，需 YYYY-MM-DD"}), 400
    if frm > to:
        return jsonify({"code": 400, "message": "from 不能晚于 to"}), 400

    visible = _visible_student_ids(sid, user)
    empty_summary = {"present": 0, "late": 0, "short_hours": 0,
                     "late_and_short": 0, "absent": 0, "on_leave": 0,
                     "total": 0, "attendance_rate": None}
    if not visible:
        return jsonify({"code": 200,
                        "range": {"from": frm.isoformat(), "to": to.isoformat()},
                        "dates": [], "summary": empty_summary, "rows": []})

    # 2. 三次批量查询（避免 N×M），CheckRecord 按 (user_id,date) 聚合，无视 camp_session_id
    plans = CampAttendancePlan.query.filter(
        CampAttendancePlan.camp_session_id == sid,
        CampAttendancePlan.user_id.in_(visible),
        CampAttendancePlan.date.between(frm, to)).all()
    checks = CheckRecord.query.filter(
        CheckRecord.user_id.in_(visible),
        CheckRecord.date.between(frm, to)).all()
    leave_set = _approved_leave_dates(sid, frm, to)

    # 3. 分组
    checks_map = defaultdict(list)
    for r in checks:
        checks_map[(r.user_id, r.date)].append(r)
    users = {u.id: u.username for u in
             UserModel.query.filter(UserModel.id.in_(visible)).all()}
    mentors = {m.user_id: m.team_mentor_id for m in
               CampMember.query.filter_by(camp_session_id=sid, role='student').all()}

    # 4. 矩阵 + 汇总
    dates_set = set()
    by_user = defaultdict(list)
    gsummary = Counter()
    for p in plans:
        res = _eval_day(checks_map.get((p.user_id, p.date), []), p,
                        (p.user_id, p.date) in leave_set)
        by_user[p.user_id].append((p.date, res))
        dates_set.add(p.date)
        gsummary[res["status"]] += 1

    rows = []
    for uid, items in by_user.items():
        psum = Counter(r["status"] for _, r in items)
        planned = len(items)
        rows.append({
            "user_id": uid,
            "username": users.get(uid, ""),
            "team_mentor_id": mentors.get(uid),
            "daily": {d.isoformat(): r for d, r in items},
            "personal": {
                "present": psum.get("present", 0),
                "late": psum.get("late", 0),
                "short_hours": psum.get("short_hours", 0),
                "late_and_short": psum.get("late_and_short", 0),
                "absent": psum.get("absent", 0),
                "on_leave": psum.get("on_leave", 0),
                "planned_days": planned,
                "attendance_rate": round(psum.get("present", 0) / planned, 3) if planned else None,
            },
        })
    rows.sort(key=lambda r: r["user_id"])

    gtotal = sum(gsummary.values())
    summary = {
        "present": gsummary.get("present", 0),
        "late": gsummary.get("late", 0),
        "short_hours": gsummary.get("short_hours", 0),
        "late_and_short": gsummary.get("late_and_short", 0),
        "absent": gsummary.get("absent", 0),
        "on_leave": gsummary.get("on_leave", 0),
        "total": gtotal,
        "attendance_rate": round(gsummary.get("present", 0) / gtotal, 3) if gtotal else None,
    }
    return jsonify({
        "code": 200,
        "range": {"from": frm.isoformat(), "to": to.isoformat()},
        "dates": sorted(d.isoformat() for d in dates_set),
        "summary": summary,
        "rows": rows,
    })


# ─────────────────────────────────────────────
# 请假（学员提交 / 导生·老师审批）
# ─────────────────────────────────────────────

@bp.route("/leave", methods=["POST"])
@jwt_required()
def leave_submit():
    user = _current_user()
    d = request.json or {}
    sid, sd, ed = d.get("camp_session_id"), d.get("start_date"), d.get("end_date")
    if not sid or not sd or not ed:
        return jsonify({"code": 400, "message": "缺少参数"}), 400
    if not CampMember.query.filter_by(camp_session_id=sid, user_id=user.id).first():
        return jsonify({"code": 403, "message": "非该营期成员"}), 403
    try:
        lv = CampLeave(camp_session_id=sid, user_id=user.id,
                       start_date=date.fromisoformat(sd), end_date=date.fromisoformat(ed),
                       reason=d.get("reason", ""))
    except (ValueError, TypeError):
        return jsonify({"code": 400, "message": "日期格式错误"}), 400
    db.session.add(lv)
    db.session.flush()
    # 通知本营导生（无导生则通知老师）
    approvers = [m.user_id for m in CampMember.query.filter_by(camp_session_id=sid, role='mentor').all()]
    if not approvers:
        approvers = [m.user_id for m in CampMember.query.filter_by(camp_session_id=sid, role='student').all()]  # 占位，实际应通知老师
        approvers = []  # 老师走管理端，不在此推送
    for aid in approvers:
        create_notification(aid, "新的营期请假申请", f"{user.username} 申请请假 {sd}~{ed}",
                            category='camp', source_type='leave', source_id=lv.id, camp_session_id=sid)
    db.session.commit()
    return jsonify({"code": 200, "message": "已提交", "leave_id": lv.id})


@bp.route("/leave/<int:lid>/approve", methods=["POST"])
@jwt_required()
@camp_role('mentor', 'teacher', 'super_admin')
def leave_approve(lid):
    user = _current_user()
    lv = CampLeave.query.get(lid)
    if not lv:
        return jsonify({"code": 404, "message": "请假记录不存在"}), 404
    # 导生仅限本团队；老师/超管不限
    if not (user.is_admin_like() or user.role == 'teacher'):
        if not _in_my_team(lv.camp_session_id, user, lv.user_id):
            return jsonify({"code": 403, "message": "无权审批（非本团队）"}), 403
    d = request.json or {}
    lv.status = 'approved' if d.get("approve", True) else 'rejected'
    lv.approver_id = user.id
    lv.approved_at = datetime.now()
    db.session.commit()
    create_notification(lv.user_id, "请假审批结果",
                        f"你的请假申请已{'批准' if lv.status == 'approved' else '拒绝'}",
                        category='camp', source_type='leave', source_id=lv.id,
                        camp_session_id=lv.camp_session_id)
    db.session.commit()
    return jsonify({"code": 200, "message": "已审批", "status": lv.status})


@bp.route("/sessions/<int:sid>/leave")
@jwt_required()
@camp_role('mentor', 'teacher', 'super_admin')
def leave_list(sid):
    user = _current_user()
    q = CampLeave.query.filter_by(camp_session_id=sid)
    visible = set(_visible_student_ids(sid, user))
    see_all = user.is_admin_like() or user.role == 'teacher'
    data = []
    for lv in q.order_by(CampLeave.created_at.desc()).all():
        if not see_all and lv.user_id not in visible:
            continue
        u = UserModel.query.get(lv.user_id)
        data.append({
            "id": lv.id, "user_id": lv.user_id, "username": u.username if u else "",
            "start_date": lv.start_date.isoformat(), "end_date": lv.end_date.isoformat(),
            "reason": lv.reason, "status": lv.status, "created_at": lv.created_at.isoformat() if lv.created_at else None,
        })
    return jsonify({"code": 200, "leaves": data})


# ─────────────────────────────────────────────
# 奖励（导生·老师发放，写 medal_user+camp+issued_by）
# ─────────────────────────────────────────────

@bp.route("/reward", methods=["POST"])
@jwt_required()
@camp_role('mentor', 'teacher', 'super_admin')
def reward_issue():
    user = _current_user()
    d = request.json or {}
    sid, uid, medal_id = d.get("camp_session_id"), d.get("user_id"), d.get("medal_id")
    if not sid or not uid or not medal_id:
        return jsonify({"code": 400, "message": "缺少参数"}), 400
    if not (user.is_admin_like() or user.role == 'teacher'):
        if not _in_my_team(sid, user, uid):
            return jsonify({"code": 403, "message": "无权给该学员发奖励"}), 403
    if not MedalModel.query.get(medal_id):
        return jsonify({"code": 404, "message": "勋章不存在"}), 404
    mu = MedalUserModel(user_id=uid, medal_id=medal_id, camp_session_id=sid,
                        issued_by=user.id, description=d.get("description", ""))
    db.session.add(mu)
    db.session.flush()
    create_notification(uid, "获得营期奖励", "导生/老师给你发了一枚勋章",
                        category='camp', source_type='reward', source_id=mu.id, camp_session_id=sid)
    db.session.commit()
    return jsonify({"code": 200, "message": "已发放"})


# ─────────────────────────────────────────────
# 座位（复用物理 Seat，按营期分配）
# ─────────────────────────────────────────────

@bp.route("/seat/assign", methods=["POST"])
@jwt_required()
@camp_role('teacher', 'super_admin')
def seat_assign():
    d = request.json or {}
    sid, seat_id, uid = d.get("camp_session_id"), d.get("seat_id"), d.get("user_id")
    if not sid or not seat_id:
        return jsonify({"code": 400, "message": "缺少参数"}), 400
    if not SeatModel.query.get(seat_id):
        return jsonify({"code": 404, "message": "座位不存在"}), 404
    cs = CampSeat.query.filter_by(camp_session_id=sid, seat_id=seat_id).first()
    if not cs:
        cs = CampSeat(camp_session_id=sid, seat_id=seat_id)
        db.session.add(cs)
    cs.user_id = uid                            # null = 解绑
    db.session.commit()
    return jsonify({"code": 200, "message": "已分配"})


@bp.route("/sessions/<int:sid>/seats")
@jwt_required()
def seat_list(sid):
    rows = CampSeat.query.filter_by(camp_session_id=sid).all()
    data = []
    for cs in rows:
        s = SeatModel.query.get(cs.seat_id)
        u = UserModel.query.get(cs.user_id) if cs.user_id else None
        data.append({
            "seat_id": cs.seat_id, "label": s.label if s else None,
            "room_id": s.room_id if s else None, "user_id": cs.user_id,
            "username": u.username if u else None,
        })
    return jsonify({"code": 200, "seats": data})

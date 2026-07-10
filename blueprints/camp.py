"""营期（Camp）系统蓝图。

角色门控（@camp_role）：
  - 营期管理（建营/成员/课程/出勤计划/座位）→ teacher+
  - 导生可操作（请假审批/发奖励）→ mentor+ 且仅限本团队（内部校验）
  - 学员操作（选课/请假）→ 仅营期成员
营期看板（混合考勤算法）见 Phase D 的 /camp/attendance/dashboard/<sid>。
"""
import json
from collections import defaultdict, Counter
from datetime import date, time, timedelta, datetime

from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required

from exts import db
from models import (
    CampSession, CampMember, CampCourse, CampAttendancePlan,
    CampSeat, CampLeave, CampJoinRequest, CheckRecord, CourseModel, UserCourseModel,
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
        "is_featured": bool(c.is_featured),
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

def _assign_member(sid, user_id, team_mentor_id=None, auto_plan=True):
    """营期成员分配核心：role 由 user.role 派生 + team_mentor 校验。
    auto_plan=True 学员按工作日自动生成承诺日（member_assign 直接加成员兜底）；
    approve 端点传 False，改由调用方用学生手选日期建 plan。
    返回 (CampMember, None) 成功（未 commit）；或 (None, (message, code)) 失败。"""
    camp = CampSession.query.get(sid)
    if not camp:
        return None, ("营期不存在", 404)
    user = UserModel.query.get(user_id)
    if not user:
        return None, ("用户不存在", 404)
    # 营期角色由全局 role 派生（物理杜绝"全局学生当营期导生"等错配）
    if user.role not in ('student', 'mentor'):
        return None, ("教师/超管通过营期管理入口操作，不作为营期成员加入", 400)
    role = user.role
    if CampMember.query.filter_by(camp_session_id=sid, user_id=user_id).first():
        return None, ("该用户已在营期中", 402)
    # 归属导生仅学员可设，且必须是本营导生
    if role == 'student' and team_mentor_id:
        if not CampMember.query.filter_by(camp_session_id=sid, user_id=team_mentor_id, role='mentor').first():
            return None, ("指定的导生不在本营", 400)
    else:
        team_mentor_id = None
    m = CampMember(camp_session_id=sid, user_id=user_id, role=role, team_mentor_id=team_mentor_id)
    db.session.add(m)
    if auto_plan and role == 'student':
        _gen_plan(camp, user_id)          # 直接加成员：按工作日生成（兜底）
    return m, None


@bp.route("/sessions/<int:sid>/members", methods=["POST"])
@jwt_required()
@camp_role('teacher', 'super_admin')
@audit_log(operation="分配营期成员")
def member_assign(sid):
    d = request.json or {}
    user_id = d.get("user_id")
    if not user_id:
        return jsonify({"code": 400, "message": "缺少 user_id"}), 400
    m, err = _assign_member(sid, user_id, d.get("team_mentor_id"))
    if err:
        msg, code = err
        return jsonify({"code": code, "message": msg}), code
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

    # 2. 批量查询：plans 按 (user,date) 索引；CheckRecord 按 (user,date) 聚合
    plans = CampAttendancePlan.query.filter(
        CampAttendancePlan.camp_session_id == sid,
        CampAttendancePlan.user_id.in_(visible),
        CampAttendancePlan.date.between(frm, to)).all()
    plan_map = defaultdict(dict)   # {user_id: {date: plan}}
    for p in plans:
        plan_map[p.user_id][p.date] = p
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

    # 4. 矩阵（全范围日期 × 可见学员）+ 汇总：未承诺 unpledged，未来承诺日 pledged，过去承诺日 _eval_day
    today = date.today()
    all_days = _weekdays(frm, to)
    dates_set = set(all_days)
    by_user = defaultdict(list)
    gsummary = Counter()
    for uid in visible:
        pm = plan_map.get(uid, {})
        for d in all_days:
            p = pm.get(d)
            if not p:
                res = {"status": "unpledged", "is_late": None, "is_sufficient": None,
                       "first_check_in": None, "total_hours": 0, "in_progress": False}
            elif d > today:
                res = {"status": "pledged", "is_late": None, "is_sufficient": None,
                       "first_check_in": None, "total_hours": 0, "in_progress": False}
            else:
                res = _eval_day(checks_map.get((uid, d), []), p, (uid, d) in leave_set)
            by_user[uid].append((d, res))
            gsummary[res["status"]] += 1

    rows = []
    for uid, items in by_user.items():
        psum = Counter(r["status"] for _, r in items)
        pm = plan_map.get(uid, {})
        pledged = len(pm)
        elapsed_pledged = sum(1 for d in pm if d <= today)
        satisfied = psum.get("present", 0)
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
                "unpledged": psum.get("unpledged", 0),
                "pledged_pending": psum.get("pledged", 0),
                "pledged_days": pledged, "satisfied": satisfied,
                "planned_days": pledged,
                "attendance_rate": round(satisfied / elapsed_pledged, 3) if elapsed_pledged else None,
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


@bp.route("/attendance/mine")
@jwt_required()
def attendance_mine():
    """学员看自己在某营的考勤（仅 @jwt_required，where 钉自己，无法查他人）。
    ?camp_session_id=<sid> 必填。复用 _eval_day，算法与 dashboard 同。"""
    user = _current_user()
    sid = request.args.get("camp_session_id", type=int)
    if not sid:
        return jsonify({"code": 400, "message": "缺少 camp_session_id"}), 400
    camp = CampSession.query.get(sid)
    if not camp:
        return jsonify({"code": 404, "message": "营期不存在"}), 404
    frm, to = camp.start_date, camp.end_date
    plans = CampAttendancePlan.query.filter(
        CampAttendancePlan.camp_session_id == sid,
        CampAttendancePlan.user_id == user.id,
        CampAttendancePlan.date.between(frm, to)).all()
    plan_by_date = {p.date: p for p in plans}
    checks = CheckRecord.query.filter(
        CheckRecord.user_id == user.id,
        CheckRecord.date.between(frm, to)).all()
    leave_set = _approved_leave_dates(sid, frm, to)
    checks_map = defaultdict(list)
    for r in checks:
        checks_map[(r.user_id, r.date)].append(r)
    today = date.today()
    daily = {}
    psum = Counter()
    dates_set = set()
    for d in _weekdays(frm, to):               # 营期全范围所有天数
        p = plan_by_date.get(d)
        if not p:                              # 未承诺（不管未来/过去）
            res = {"status": "unpledged", "is_late": None, "is_sufficient": None,
                   "first_check_in": None, "total_hours": 0, "in_progress": False}
        elif d > today:                        # 未来承诺日：已承诺，待考勤（不算缺勤）
            res = {"status": "pledged", "is_late": None, "is_sufficient": None,
                   "first_check_in": None, "total_hours": 0, "in_progress": False}
        else:
            res = _eval_day(checks_map.get((user.id, d), []), p, (user.id, d) in leave_set)
        daily[d.isoformat()] = res
        dates_set.add(d)
        psum[res["status"]] += 1
    pledged = len(plans)
    elapsed_pledged = sum(1 for p in plans if p.date <= today)   # 已过的承诺日（达标率分母）
    satisfied = psum.get("present", 0)
    personal = {
        "present": psum.get("present", 0), "late": psum.get("late", 0),
        "short_hours": psum.get("short_hours", 0), "late_and_short": psum.get("late_and_short", 0),
        "absent": psum.get("absent", 0), "on_leave": psum.get("on_leave", 0),
        "unpledged": psum.get("unpledged", 0), "pledged_pending": psum.get("pledged", 0),
        "pledged_days": pledged, "satisfied": satisfied,
        "planned_days": pledged,            # 兼容旧前端字段
        "attendance_rate": round(satisfied / elapsed_pledged, 3) if elapsed_pledged else None,
    }
    return jsonify({
        "code": 200,
        "range": {"from": frm.isoformat(), "to": to.isoformat()},
        "dates": sorted(d.isoformat() for d in dates_set),
        "daily": daily,
        "personal": personal,
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


@bp.route("/leave/mine")
@jwt_required()
def leave_mine():
    """学员看自己的请假历史（仅 @jwt_required，where 钉自己）。
    ?camp_session_id=<sid> 可选，传则按营过滤。"""
    user = _current_user()
    sid = request.args.get("camp_session_id", type=int)
    q = CampLeave.query.filter_by(user_id=user.id)
    if sid:
        q = q.filter_by(camp_session_id=sid)
    data = []
    for lv in q.order_by(CampLeave.created_at.desc()).all():
        data.append({
            "id": lv.id, "camp_session_id": lv.camp_session_id,
            "start_date": lv.start_date.isoformat(), "end_date": lv.end_date.isoformat(),
            "reason": lv.reason, "status": lv.status,
            "created_at": lv.created_at.isoformat() if lv.created_at else None,
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


@bp.route("/medals")
@jwt_required()
@camp_role('mentor', 'teacher', 'super_admin')
def camp_medals():
    """营期可用勋章列表（导生发奖励时选勋章用；@camp_role 门控，不依赖 medal_management 权限）。"""
    medals = MedalModel.query.all()
    data = [{"id": m.id, "name": m.medal_name} for m in medals]
    return jsonify({"code": 200, "medals": data})


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


# ─────────────────────────────────────────────
# 营期主页指定 + 加入申请 + 团队改派
# ─────────────────────────────────────────────

def _camp_mentors(sid):
    """本营导生列表（供审批/改派选归属导生）"""
    out = []
    for m in CampMember.query.filter_by(camp_session_id=sid, role='mentor').all():
        u = UserModel.query.get(m.user_id)
        if u:
            out.append({"user_id": u.id, "username": u.username})
    return out


@bp.route("/featured")
@jwt_required()
def camp_featured():
    """用户端营期主页：返回后台指定的当前营期 + 当前用户是否成员 + 我的最新申请状态。"""
    user = _current_user()
    camp = CampSession.query.filter_by(is_featured=True).first()
    if not camp:
        return jsonify({"code": 200, "session": None, "is_member": False, "my_request": None})
    is_member = CampMember.query.filter_by(camp_session_id=camp.id, user_id=user.id).first() is not None
    my_req = (CampJoinRequest.query.filter_by(camp_session_id=camp.id, user_id=user.id)
              .order_by(CampJoinRequest.created_at.desc()).first())
    return jsonify({
        "code": 200,
        "session": _session_dict(camp),
        "is_member": is_member,
        "my_request": {"id": my_req.id, "status": my_req.status} if my_req else None,
    })


@bp.route("/sessions/<int:sid>/feature", methods=["PUT"])
@jwt_required()
@camp_role('teacher', 'super_admin')
@audit_log(operation="设为当前营期")
def camp_feature(sid):
    camp = CampSession.query.get(sid)
    if not camp:
        return jsonify({"code": 404, "message": "营期不存在"}), 404
    CampSession.query.filter(CampSession.is_featured.is_(True)).update({"is_featured": False})
    camp.is_featured = True
    db.session.commit()
    return jsonify({"code": 200, "message": "已设为当前营期"})


@bp.route("/sessions/<int:sid>/join-request", methods=["POST"])
@jwt_required()
@audit_log(operation="提交营期加入申请")
def join_request_submit(sid):
    """学员/导生自助提交加入申请（teacher/超管不申请，他们直接管营）。"""
    user = _current_user()
    camp = CampSession.query.get(sid)
    if not camp:
        return jsonify({"code": 404, "message": "营期不存在"}), 404
    if user.role != 'student':
        return jsonify({"code": 400, "message": "仅学员可申请加入营期；导生/老师由管理员直接分配"}), 400
    if CampMember.query.filter_by(camp_session_id=sid, user_id=user.id).first():
        return jsonify({"code": 402, "message": "你已是该营期成员"}), 402
    if CampJoinRequest.query.filter_by(camp_session_id=sid, user_id=user.id, status='pending').first():
        return jsonify({"code": 409, "message": "已有待审批的申请，请等待审核"}), 409
    d = request.json or {}
    # 学员手选承诺出勤日（JSON 数组），校验格式 + 在营期范围内
    selected_days = d.get("selected_days") or []
    valid = []
    try:
        for s in selected_days:
            dv = date.fromisoformat(str(s))
            if camp.start_date <= dv <= camp.end_date:
                valid.append(dv.isoformat())
    except (ValueError, TypeError):
        return jsonify({"code": 400, "message": "承诺出勤日格式错误"}), 400
    if not valid:
        return jsonify({"code": 400, "message": "请至少选择一个承诺出勤日"}), 400
    db.session.add(CampJoinRequest(camp_session_id=sid, user_id=user.id,
                                   reason=d.get("reason"), selected_days=json.dumps(valid)))
    db.session.commit()
    return jsonify({"code": 200, "message": "申请已提交，等待审批"})


@bp.route("/join-requests/mine")
@jwt_required()
def join_request_mine():
    user = _current_user()
    rows = CampJoinRequest.query.filter_by(user_id=user.id).order_by(CampJoinRequest.created_at.desc()).all()
    data = []
    for r in rows:
        c = CampSession.query.get(r.camp_session_id)
        data.append({
            "id": r.id, "camp_session_id": r.camp_session_id, "camp_name": c.name if c else None,
            "reason": r.reason, "status": r.status,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        })
    return jsonify({"code": 200, "requests": data})


@bp.route("/sessions/<int:sid>/join-requests")
@jwt_required()
@camp_role('teacher', 'super_admin')
def join_request_list(sid):
    """老师/超管看某营的加入申请（默认 pending，?status=all 看全部）。返回含本营导生列表供审批选。"""
    status = request.args.get("status", "pending")
    q = CampJoinRequest.query.filter_by(camp_session_id=sid)
    if status != "all":
        q = q.filter_by(status=status)
    rows = q.order_by(CampJoinRequest.created_at.desc()).all()
    data = []
    for r in rows:
        u = UserModel.query.get(r.user_id)
        data.append({
            "id": r.id, "user_id": r.user_id, "username": u.username if u else None,
            "email": u.email if u else None, "role": u.role if u else None,
            "reason": r.reason, "status": r.status,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        })
    return jsonify({"code": 200, "requests": data, "mentors": _camp_mentors(sid)})


@bp.route("/join-requests/<int:rid>/approve", methods=["POST"])
@jwt_required()
@camp_role('teacher', 'super_admin')
@audit_log(operation="批准营期申请")
def join_request_approve(rid):
    req = CampJoinRequest.query.get(rid)
    if not req:
        return jsonify({"code": 404, "message": "申请不存在"}), 404
    if req.status != 'pending':
        return jsonify({"code": 400, "message": "该申请已处理"}), 400
    d = request.json or {}
    # 学员审批时老师选定归属导生（body team_mentor_id）；auto_plan=False 改由手选日期建 plan
    m, err = _assign_member(req.camp_session_id, req.user_id, d.get("team_mentor_id"), auto_plan=False)
    if err:
        msg, code = err
        return jsonify({"code": code, "message": msg}), code
    # 用学员申请时手选的承诺日建 CampAttendancePlan（替代 _gen_plan 自动工作日）
    if m.role == 'student':
        camp = CampSession.query.get(req.camp_session_id)
        days = []
        try:
            days = json.loads(req.selected_days) if req.selected_days else []
        except (ValueError, TypeError):
            days = []
        if days:
            exist = {p.date for p in CampAttendancePlan.query.filter_by(
                camp_session_id=req.camp_session_id, user_id=req.user_id).all()}
            for ds in days:
                try:
                    dv = date.fromisoformat(ds)
                except ValueError:
                    continue
                if dv in exist:
                    continue
                db.session.add(CampAttendancePlan(
                    camp_session_id=req.camp_session_id, user_id=req.user_id, date=dv,
                    expected_check_in=camp.expected_check_in, min_daily_hours=camp.min_daily_hours))
        else:
            _gen_plan(camp, req.user_id)   # 兜底：申请没带手选日则按工作日
    req.status = 'approved'
    req.reviewed_by = _current_user().id
    req.reviewed_at = datetime.now()
    # 通知学生：入营申请已通过（同事务，commit 之前）
    camp = CampSession.query.get(req.camp_session_id)
    camp_name = camp.name if camp else '营期'
    mentor_name = None
    if getattr(m, 'team_mentor_id', None):      # 老师审批时可能未指定归属导生
        mu = UserModel.query.get(m.team_mentor_id)
        if mu:
            mentor_name = mu.username
    content = f"你的入营申请已通过，欢迎加入「{camp_name}」。"
    if mentor_name:
        content += f"你的导生是 {mentor_name}，可在营期内联系。"
    create_notification(req.user_id, "入营申请已通过", content,
                        category='camp', source_type='join_request',
                        source_id=req.id, camp_session_id=req.camp_session_id)
    db.session.commit()
    return jsonify({"code": 200, "message": "已批准并加入营期", "member_id": m.id})


@bp.route("/join-requests/<int:rid>/reject", methods=["POST"])
@jwt_required()
@camp_role('teacher', 'super_admin')
@audit_log(operation="拒绝营期申请")
def join_request_reject(rid):
    req = CampJoinRequest.query.get(rid)
    if not req:
        return jsonify({"code": 404, "message": "申请不存在"}), 404
    if req.status != 'pending':
        return jsonify({"code": 400, "message": "该申请已处理"}), 400
    req.status = 'rejected'
    req.reviewed_by = _current_user().id
    req.reviewed_at = datetime.now()
    # 通知学生：入营申请未通过（同事务，commit 之前；拒绝原因从 body 读，不入库）
    d = request.json or {}
    reason = (d.get("reason") or "").strip()
    camp = CampSession.query.get(req.camp_session_id)
    camp_name = camp.name if camp else '该营期'
    content = f"很遗憾，你对「{camp_name}」的入营申请未通过。"
    if reason:
        content += f"原因：{reason}。"
    content += "如有疑问请联系老师。"
    create_notification(req.user_id, "入营申请未通过", content,
                        category='camp', source_type='join_request',
                        source_id=req.id, camp_session_id=req.camp_session_id,
                        is_important=True)
    db.session.commit()
    return jsonify({"code": 200, "message": "已拒绝"})


@bp.route("/sessions/<int:sid>/members/<int:uid>", methods=["PUT"])
@jwt_required()
@camp_role('teacher', 'super_admin')
@audit_log(operation="改派营期成员导生")
def member_update(sid, uid):
    """改成员归属导生（仅学员行可改；日常团队改派入口）。"""
    m = CampMember.query.filter_by(camp_session_id=sid, user_id=uid).first()
    if not m:
        return jsonify({"code": 404, "message": "成员不存在"}), 404
    if m.role != 'student':
        return jsonify({"code": 400, "message": "仅学员可指定归属导生"}), 400
    d = request.json or {}
    team_mentor_id = d.get("team_mentor_id")
    if team_mentor_id:
        if not CampMember.query.filter_by(camp_session_id=sid, user_id=team_mentor_id, role='mentor').first():
            return jsonify({"code": 400, "message": "指定的导生不在本营"}), 400
    m.team_mentor_id = team_mentor_id
    db.session.commit()
    return jsonify({"code": 200, "message": "已更新"})

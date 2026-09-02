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

from exts import db, redis_client
from models import (
    CampSession, CampCycle, CampMember, CampCourse, CampAttendancePlan,
    CampMentorEligibilityBatch, CampMentorEligibility,
    CampSeat, CampLeave, CampJoinRequest, CheckRecord, CourseModel, UserCourseModel,
    MedalModel, MedalUserModel, UserModel, SeatModel,
    CampMentorProfile, CampMentorPreference, CampMentorMatch,
)

from . import camp_role, audit_log, _current_user
from .notification import create_notification
from .camp_ms import _apply_ms_fields, _validate_ms, _ms_dict, _ms_phase

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


def _sync_plans(camp):
    """营期日期/weekdays/成员变更后同步全部学员的 plan：删范围外（及工作日营的周末日）+ 补范围内缺的。幂等。
    返回同步后 plan 总数。（旧 plan_regenerate 只补不删，缩短营期后范围外脏 plan 残留继续判缺勤）"""
    out_q = CampAttendancePlan.query.filter(
        CampAttendancePlan.camp_session_id == camp.id,
        ~CampAttendancePlan.date.between(camp.start_date, camp.end_date)
    )
    if camp.weekdays_only:
        in_range = CampAttendancePlan.query.filter(
            CampAttendancePlan.camp_session_id == camp.id,
            CampAttendancePlan.date.between(camp.start_date, camp.end_date)
        ).all()
        weekend_ids = [p.id for p in in_range if p.date.weekday() >= 5]
        if weekend_ids:
            CampAttendancePlan.query.filter(CampAttendancePlan.id.in_(weekend_ids)).delete(
                synchronize_session=False)
    out_q.delete(synchronize_session=False)
    for m in CampMember.query.filter_by(camp_session_id=camp.id, role='student').all():
        _gen_plan(camp, m.user_id)
    return CampAttendancePlan.query.filter_by(camp_session_id=camp.id).count()


def _camp_writable(camp):
    """archived 营只读：禁止一切营期内写操作（列表/看板等读取不受限）。"""
    return camp.status != 'archived'


def _visible_student_ids(camp_id, user):
    """导生=本团队学员；老师/超管=全营学员。"""
    if user.is_admin():
        return [m.user_id for m in CampMember.query.filter_by(
            camp_session_id=camp_id, role='student').all()]
    return [m.user_id for m in CampMember.query.filter_by(
        camp_session_id=camp_id, role='student', team_mentor_id=user.id).all()]


def _in_my_team(camp_id, mentor, student_id):
    """student_id 是否是该 mentor 在本营的团队成员"""
    return CampMember.query.filter_by(
        camp_session_id=camp_id, role='student',
        user_id=student_id, team_mentor_id=mentor.id).first() is not None


def _eval_day(records, plan, on_leave, is_today=False):
    """对某学员某承诺日的 CheckRecord 列表算混合考勤状态（纯函数，可单测）。

    records : 该 (user,date) 的 CheckRecord 列表（可能为空）
    plan    : CampAttendancePlan（冗余 expected_check_in / min_daily_hours）
    on_leave: 该日是否命中已批准请假
    is_today: 该日是否为今天。当天有未签退段 → status=in_progress 不下判定
              （未签退段 duration 为 0，照算法会误判 short_hours，人还坐在那里）
    返回: {status, is_late, is_sufficient, first_check_in, total_hours, in_progress}
    status ∈ present / late / short_hours / late_and_short / absent / on_leave / in_progress
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
    if is_today and in_progress:
        return {"status": "in_progress", "is_late": None, "is_sufficient": None,
                "first_check_in": min(ins).isoformat() if ins else None,
                "total_hours": round(total, 2), "in_progress": True}
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
        "category": c.category,
        "cycle_id": c.cycle_id,
        "cycle_code": c.cycle.code if c.cycle else None,
        "cycle_name": c.cycle.name if c.cycle else None,
        "start_date": c.start_date.isoformat(), "end_date": c.end_date.isoformat(),
        "status": c.status,
        "expected_check_in": c.expected_check_in.isoformat() if c.expected_check_in else None,
        "min_daily_hours": c.min_daily_hours, "weekdays_only": c.weekdays_only,
        "is_featured": bool(c.is_featured),
        "member_count": CampMember.query.filter_by(camp_session_id=c.id).count(),
        # 选导生字段（未启用时 enabled=false，其余为 null）
        **_ms_dict(c),
    }


# ─────────────────────────────────────────────
# 教学周期（CampCycle）
# ─────────────────────────────────────────────

@bp.route("/cycles")
@jwt_required()
def cycle_list():
    """教学周期列表（全员可读；建营下拉用）。无起止日期，纯归类标签。"""
    cycles = CampCycle.query.order_by(CampCycle.sort_order, CampCycle.id.desc()).all()
    return jsonify({"code": 200, "cycles": [{
        "id": c.id, "code": c.code, "name": c.name,
        "session_count": CampSession.query.filter_by(cycle_id=c.id).count(),
    } for c in cycles]})


@bp.route("/cycles", methods=["POST"])
@jwt_required()
@camp_role()
@audit_log(operation="创建教学周期")
def cycle_create():
    """创建教学周期（super_admin）。body: {code, name, sort_order?}"""
    d = request.json or {}
    code, name = (d.get("code") or "").strip(), (d.get("name") or "").strip()
    if not code or not name:
        return jsonify({"code": 400, "message": "缺少 code/name"}), 400
    if CampCycle.query.filter_by(code=code).first():
        return jsonify({"code": 409, "message": "该周期 code 已存在"}), 409
    c = CampCycle(code=code, name=name, sort_order=d.get("sort_order", 0))
    db.session.add(c)
    db.session.commit()
    return jsonify({"code": 200, "message": "创建成功", "cycle_id": c.id})


# ─────────────────────────────────────────────
# 导生资格名单（Q-007：导入→确认→名单内定向可见→自行报名）
# ─────────────────────────────────────────────

def _eligibility_report(sid, emails):
    """dry-run：emails → (匹配用户, 未匹配, 已有资格) 三组。"""
    matched, unmatched = [], []
    for e in dict.fromkeys(x.strip().lower() for x in emails if x and x.strip()):
        u = UserModel.query.filter(db.func.lower(UserModel.email) == e).first()
        if u:
            matched.append(u)
        else:
            unmatched.append(e)
    have = {x.user_id for x in CampMentorEligibility.query.filter_by(camp_session_id=sid).all()}
    return matched, unmatched, have


@bp.route("/sessions/<int:sid>/mentor-eligibility/import-preview", methods=["POST"])
@jwt_required()
@camp_role()
def eligibility_preview(sid):
    """dry-run 预览：body {emails:[...]}，不落库。"""
    emails = (request.json or {}).get("emails") or []
    matched, unmatched, have = _eligibility_report(sid, emails)
    return jsonify({"code": 200, "data": {
        "matched": [{"user_id": u.id, "email": u.email, "username": u.username,
                     "already_eligible": u.id in have} for u in matched],
        "unmatched_emails": unmatched,
    }})


@bp.route("/sessions/<int:sid>/mentor-eligibility/import-confirm", methods=["POST"])
@jwt_required()
@camp_role()
@audit_log(operation="导入导生资格名单")
def eligibility_confirm(sid):
    """确认导入：幂等（UQ 跳过已存在）。允许 draft/upcoming 期操作。"""
    camp = CampSession.query.get(sid)
    if not camp:
        return jsonify({"code": 404, "message": "营期不存在"}), 404
    if camp.status not in ("draft", "upcoming"):
        return jsonify({"code": 400, "message": "名单导入仅限草稿/待开放阶段"}), 400
    emails = (request.json or {}).get("emails") or []
    matched, unmatched, have = _eligibility_report(sid, emails)
    batch = CampMentorEligibilityBatch(camp_session_id=sid, imported_by=_current_user().id)
    db.session.add(batch)
    db.session.flush()
    added = 0
    for u in matched:
        if u.id in have:
            continue
        db.session.add(CampMentorEligibility(batch_id=batch.id, camp_session_id=sid, user_id=u.id))
        have.add(u.id)
        added += 1
    db.session.commit()
    return jsonify({"code": 200, "message": f"已确认：新增 {added} 人，已有资格跳过 {len(matched)-added} 人，未匹配 {len(unmatched)} 人",
                    "data": {"added": added, "unmatched_emails": unmatched}})


@bp.route("/sessions/<int:sid>/mentor-eligibility")
@jwt_required()
@camp_role()
def eligibility_list(sid):
    """管理端查看名单与报名情况。"""
    rows = CampMentorEligibility.query.filter_by(camp_session_id=sid).all()
    member_ids = {m.user_id for m in CampMember.query.filter_by(camp_session_id=sid, role='mentor').all()}
    out = []
    for r in rows:
        u = UserModel.query.get(r.user_id)
        out.append({"user_id": r.user_id, "email": u.email if u else None,
                    "username": u.username if u else None, "registered": r.user_id in member_ids})
    return jsonify({"code": 200, "eligibility": out})


@bp.route("/sessions/<int:sid>/mentor-registration", methods=["POST"])
@jwt_required()
@audit_log(operation="导生报名入营")
def mentor_registration(sid):
    """名单内用户自行报名成为本营导生（无二次审核；幂等）。仅 upcoming 期。"""
    user = _current_user()
    camp = CampSession.query.get(sid)
    if not camp:
        return jsonify({"code": 404, "message": "营期不存在"}), 404
    if not CampMentorEligibility.query.filter_by(camp_session_id=sid, user_id=user.id).first():
        return jsonify({"code": 403, "message": "你不在本营导生资格名单内"}), 403
    if CampMember.query.filter_by(camp_session_id=sid, user_id=user.id).first():
        return jsonify({"code": 200, "message": "你已是本营成员，无需重复报名"}), 200
    if camp.status != "upcoming":
        return jsonify({"code": 400, "message": "导生报名仅在待开放阶段开放"}), 400
    db.session.add(CampMember(camp_session_id=sid, user_id=user.id, role="mentor"))
    db.session.commit()
    return jsonify({"code": 200, "message": "报名成功，现在可以布置你的导生名片了"})


# ─────────────────────────────────────────────
# 状态机（H-004 冻结版）：draft→upcoming→selecting→running→archived
# ─────────────────────────────────────────────

# 合法迁移表：动作 → (源状态, 目标状态)。upcoming→draft 允许撤回；结营不可逆。
CAMP_TRANSITIONS = {
    "publish":         ("draft", "upcoming"),    # 发布：导生资格导入+导生报名+名片布置
    "retract":         ("upcoming", "draft"),    # 撤回发布（手滑保护）
    "open_enrollment": ("upcoming", "selecting"),# 开放报名：学员入池+市集交志愿+指派锁定
    "open":            ("selecting", "running"), # 开营：课程/考勤/请假/奖励
    "close":           ("running", "archived"),  # 结营：只读归档（不可逆）
}


@bp.route("/sessions/<int:sid>/transitions", methods=["POST"])
@jwt_required()
@camp_role()
@audit_log(operation="营期状态迁移")
def session_transition(sid):
    """目标动作制状态迁移（条件更新防并发；status 不再经 PUT 直改）。body: {"action": "publish|retract|open_enrollment|open|close"}"""
    d = request.json or {}
    action = d.get("action")
    if action not in CAMP_TRANSITIONS:
        return jsonify({"code": 400, "message": f"未知动作；支持 {'/'.join(CAMP_TRANSITIONS)}"}), 400
    src_status, dst_status = CAMP_TRANSITIONS[action]
    # 条件更新：并发/重复提交时 rowcount=0 → 读库给出真实状态
    n = CampSession.query.filter(CampSession.id == sid, CampSession.status == src_status)\
        .update({"status": dst_status, "updated_at": datetime.now()})
    if not n:
        camp = CampSession.query.get(sid)
        if not camp:
            return jsonify({"code": 404, "message": "营期不存在"}), 404
        return jsonify({"code": 409, "message": f"迁移失败：当前状态为 {camp.status}，{action} 要求 {src_status}"}), 409
    db.session.commit()
    camp = CampSession.query.get(sid)
    return jsonify({"code": 200, "message": f"已{'发布' if action=='publish' else '撤回发布' if action=='retract' else '开放报名' if action=='open_enrollment' else '开营' if action=='open' else '结营'}",
                    "session": _session_dict(camp)})


# ─────────────────────────────────────────────
# 营期 CRUD
# ─────────────────────────────────────────────

@bp.route("/sessions", methods=["POST"])
@jwt_required()
@camp_role()
@audit_log(operation="创建营期")
def session_create():
    d = request.json or {}
    name, start, end = d.get("name"), d.get("start_date"), d.get("end_date")
    if not name or not start or not end:
        return jsonify({"code": 400, "message": "缺少 name/start_date/end_date"}), 400
    # 阶段 1：类型封闭枚举 + 必挂教学周期
    from models import CAMP_CATEGORY_DEFAULTS
    category = d.get("category", "learning")
    if category not in CAMP_CATEGORY_DEFAULTS:
        return jsonify({"code": 400, "message": f"category 仅支持 {'/'.join(CAMP_CATEGORY_DEFAULTS)}"}), 400
    cycle = CampCycle.query.get(d.get("cycle_id")) if d.get("cycle_id") else None
    if not cycle:
        return jsonify({"code": 400, "message": "缺少有效的 cycle_id（教学周期），请先经 POST /camp/cycles 创建"}), 400
    try:
        camp = CampSession(
            name=name, category=category, cycle_id=cycle.id,
            start_date=date.fromisoformat(start), end_date=date.fromisoformat(end),
            expected_check_in=time.fromisoformat(d["expected_check_in"]) if d.get("expected_check_in") else None,
            min_daily_hours=d.get("min_daily_hours"),
            weekdays_only=d.get("weekdays_only", True),
        )
        _apply_ms_fields(camp, d)          # 选导生配置（可选功能，未传即不启用）
    except (ValueError, TypeError) as e:
        return jsonify({"code": 400, "message": f"参数格式错误: {e}"}), 400
    err = _validate_ms(camp)
    if err:
        return jsonify({"code": 400, "message": err}), 400
    db.session.add(camp)
    db.session.commit()
    return jsonify({"code": 200, "message": "创建成功", "session_id": camp.id,
                    "session": _session_dict(camp)})


@bp.route("/sessions")
@jwt_required()
def session_list():
    user = _current_user()
    q = CampSession.query
    if not (user.is_admin()):
        # 学员/导生：仅自己参与的营，且 draft（未开放）不可见；archived 历史营保留
        ids = [m.camp_session_id for m in CampMember.query.filter_by(user_id=user.id).all()] + \
               [e.camp_session_id for e in CampMentorEligibility.query.filter_by(user_id=user.id).all()]
        q = q.filter(CampSession.id.in_(ids), CampSession.status != 'draft') if ids else q.filter(False)
    camps = q.order_by(CampSession.start_date.desc()).all()
    # 附当前用户是否成员：用户端「我的营期」据此过滤掉非成员营（管理端列表忽略此字段）
    member_ids = {m.camp_session_id for m in CampMember.query.filter_by(user_id=user.id).all()}
    return jsonify({"code": 200,
                    "sessions": [{**_session_dict(c), "is_member": c.id in member_ids} for c in camps]})


@bp.route("/sessions/<int:sid>")
@jwt_required()
def session_detail(sid):
    camp = CampSession.query.get(sid)
    if not camp:
        return jsonify({"code": 404, "message": "营期不存在"}), 404
    return jsonify({"code": 200, "session": _session_dict(camp)})


@bp.route("/sessions/<int:sid>", methods=["PUT"])
@jwt_required()
@camp_role()
@audit_log(operation="修改营期")
def session_update(sid):
    camp = CampSession.query.get(sid)
    if not camp:
        return jsonify({"code": 404, "message": "营期不存在"}), 404
    d = request.json or {}
    old_start, old_end, old_weekdays = camp.start_date, camp.end_date, camp.weekdays_only
    for f in ["name", "weekdays_only", "min_daily_hours"]:  # status 走 /transitions；camp_type 已退役
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
        db.session.rollback()
        return jsonify({"code": 400, "message": f"参数格式错误: {e}"}), 400
    if camp.start_date > camp.end_date:
        db.session.rollback()
        return jsonify({"code": 400, "message": "开始日期不能晚于结束日期"}), 400
    # 选导生配置（部分更新：只处理出现的键）；改过则重置过渡通知游标（见 _maybe_notify_transition）
    ms_touched = any(k in d for k in (
        "mentor_selection_enabled", "ms_preference_start", "ms_preference_deadline",
        "ms_round1_deadline", "ms_round2_deadline", "ms_tags"))
    if ms_touched:
        try:
            _apply_ms_fields(camp, d)
        except (ValueError, TypeError) as e:
            db.session.rollback()
            return jsonify({"code": 400, "message": f"参数格式错误: {e}"}), 400
        err = _validate_ms(camp)
        if err:
            db.session.rollback()
            return jsonify({"code": 400, "message": err}), 400
    # 同步冗余副本：改出勤时间/工时阈值后，已展开的 CampAttendancePlan 也跟着刷新，
    # 否则 _eval_day 仍按旧副本判定迟到/工时（见 _eval_day），管理员改的设置不生效。
    if d.get("expected_check_in") or "min_daily_hours" in d:
        plans = CampAttendancePlan.query.filter_by(camp_session_id=sid).all()
        for p in plans:
            if d.get("expected_check_in"):
                p.expected_check_in = camp.expected_check_in
            if "min_daily_hours" in d:
                p.min_daily_hours = camp.min_daily_hours
    # 日期范围/工作日口径变更后同步 plan（删范围外 + 补范围内缺的）
    plan_note = ""
    if (camp.start_date, camp.end_date, camp.weekdays_only) != (old_start, old_end, old_weekdays):
        cnt = _sync_plans(camp)
        plan_note = f"；承诺出勤日已同步（现 {cnt} 条，范围外已清理）"
    db.session.commit()
    if ms_touched:
        try:
            # 配置变更（含「提前截止」= 把 deadline 改成 now）后重置游标，允许阶段过渡通知按新时间线重发
            redis_client.delete(f"ms:phase_last:{camp.id}")
        except Exception:
            pass   # Redis 不可用不阻断营期编辑（通知触发层自身有降级）
    return jsonify({"code": 200, "message": "已更新" + plan_note, "session": _session_dict(camp)})


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
    if not _camp_writable(camp):
        return None, ("营期已归档，只读", 400)
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
@camp_role()
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
    see_all = user.is_admin()
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
@camp_role()
@audit_log(operation="移除营期成员")
def member_remove(sid, uid):
    camp = CampSession.query.get(sid)
    if not camp:
        return jsonify({"code": 404, "message": "营期不存在"}), 404
    if not _camp_writable(camp):
        return jsonify({"code": 400, "message": "营期已归档，只读"}), 400
    m = CampMember.query.filter_by(camp_session_id=sid, user_id=uid).first()
    if not m:
        return jsonify({"code": 404, "message": "成员不存在"}), 404
    db.session.delete(m)
    CampAttendancePlan.query.filter_by(camp_session_id=sid, user_id=uid).delete()
    # 连带清理：座位解绑（座位保留，人员清空）+ 未处理的加入申请（避免再批入已移除的人）
    for st in CampSeat.query.filter_by(camp_session_id=sid, user_id=uid).all():
        st.user_id = None
    CampJoinRequest.query.filter_by(camp_session_id=sid, user_id=uid, status='pending').delete()
    # 选导生连带清理：名片 / 该用户相关志愿 / 配对账本；
    # 移除的是导生时，其名下学员 team_mentor_id 置空（回未匹配池，二轮活跃则可再被选）
    CampMentorProfile.query.filter_by(camp_session_id=sid, user_id=uid).delete()
    CampMentorPreference.query.filter(
        CampMentorPreference.camp_session_id == sid,
        (CampMentorPreference.student_user_id == uid)
        | (CampMentorPreference.mentor_user_id == uid)).delete(synchronize_session=False)
    CampMentorMatch.query.filter(
        CampMentorMatch.camp_session_id == sid,
        (CampMentorMatch.mentor_user_id == uid) | (CampMentorMatch.student_user_id == uid)
    ).delete(synchronize_session=False)
    CampMember.query.filter(
        CampMember.camp_session_id == sid, CampMember.team_mentor_id == uid
    ).update({CampMember.team_mentor_id: None}, synchronize_session=False)
    db.session.commit()
    return jsonify({"code": 200, "message": "已移除"})


# ─────────────────────────────────────────────
# 课程目录 + 选课
# ─────────────────────────────────────────────

@bp.route("/sessions/<int:sid>/courses", methods=["POST"])
@jwt_required()
@camp_role()
def course_add(sid):
    camp = CampSession.query.get(sid)
    if not camp:
        return jsonify({"code": 404, "message": "营期不存在"}), 404
    if not _camp_writable(camp):
        return jsonify({"code": 400, "message": "营期已归档，只读"}), 400
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


@bp.route("/sessions/<int:sid>/courses/<int:cid>", methods=["DELETE"])
@jwt_required()
@camp_role()
@audit_log(operation="营期移除课程")
def course_remove(sid, cid):
    camp = CampSession.query.get(sid)
    if not camp:
        return jsonify({"code": 404, "message": "营期不存在"}), 404
    if not _camp_writable(camp):
        return jsonify({"code": 400, "message": "营期已归档，只读"}), 400
    cc = CampCourse.query.filter_by(camp_session_id=sid, course_id=cid).first()
    if not cc:
        return jsonify({"code": 404, "message": "该课程不在营期中"}), 404
    db.session.delete(cc)
    db.session.commit()
    return jsonify({"code": 200, "message": "已移除"})


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
    camp = CampSession.query.get(sid)
    if not camp or camp.status != 'active':
        return jsonify({"code": 400, "message": "营期未开放选课"}), 400
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
@camp_role()
@audit_log(operation="重生成营期出勤计划")
def plan_regenerate(sid):
    camp = CampSession.query.get(sid)
    if not camp:
        return jsonify({"code": 404, "message": "营期不存在"}), 404
    if not _camp_writable(camp):
        return jsonify({"code": 400, "message": "营期已归档，只读"}), 400
    cnt = _sync_plans(camp)
    db.session.commit()
    return jsonify({"code": 200, "message": "已重生成", "plan_count": cnt})


# ─────────────────────────────────────────────
# 考勤看板（混合双维度：迟到维度 + 当日时长达标维度）
# ─────────────────────────────────────────────

@bp.route("/attendance/dashboard/<int:sid>")
@jwt_required()
@camp_role('mentor')
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
                res = _eval_day(checks_map.get((uid, d), []), p, (uid, d) in leave_set,
                                is_today=(d == today))
            by_user[uid].append((d, res))
            gsummary[res["status"]] += 1

    rows = []
    for uid, items in by_user.items():
        psum = Counter(r["status"] for _, r in items)
        pm = plan_map.get(uid, {})
        pledged = len(pm)
        # 已过承诺日只算 d < today：今天还没过完（进行中/还没到齐），不进达标率分母
        elapsed_pledged = sum(1 for d in pm if d < today)
        # 出勤=时长达标（present+late）。弹性考勤语义：迟到但时长足够算出勤（需求4），
        # 迟到仅作附加标记；late_and_short 时长不足仍不算
        satisfied = psum.get("present", 0) + psum.get("late", 0)
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
                "in_progress": psum.get("in_progress", 0),
                "unpledged": psum.get("unpledged", 0),
                "pledged_pending": psum.get("pledged", 0),
                "pledged_days": pledged, "satisfied": satisfied,
                "planned_days": pledged,
                "elapsed_pledged": elapsed_pledged,
                "attendance_rate": round(satisfied / elapsed_pledged, 3) if elapsed_pledged else None,
            },
        })
    rows.sort(key=lambda r: r["user_id"])

    gtotal = sum(gsummary.values())
    # 营级出勤率与个人同口径：分子=时长达标出勤(present+late)，分母=Σ各学员已过承诺日。
    # 旧版分母是全部格子数（含未承诺空格与未来承诺日），进行中营期会被未来日稀释到异常偏低
    g_elapsed = sum(r["personal"]["elapsed_pledged"] for r in rows)
    g_attended = gsummary.get("present", 0) + gsummary.get("late", 0)
    summary = {
        "present": gsummary.get("present", 0),
        "late": gsummary.get("late", 0),
        "short_hours": gsummary.get("short_hours", 0),
        "late_and_short": gsummary.get("late_and_short", 0),
        "absent": gsummary.get("absent", 0),
        "on_leave": gsummary.get("on_leave", 0),
        "total": gtotal,
        "elapsed_total": g_elapsed,
        "attendance_rate": round(g_attended / g_elapsed, 3) if g_elapsed else None,
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
            res = _eval_day(checks_map.get((user.id, d), []), p, (user.id, d) in leave_set,
                            is_today=(d == today))
        daily[d.isoformat()] = res
        dates_set.add(d)
        psum[res["status"]] += 1
    pledged = len(plans)
    elapsed_pledged = sum(1 for p in plans if p.date < today)   # 已过完的承诺日（达标率分母，今天不计）
    # 出勤口径与 dashboard 一致：present+late（时长达标即出勤，迟到仅附加标记）
    satisfied = psum.get("present", 0) + psum.get("late", 0)
    personal = {
        "present": psum.get("present", 0), "late": psum.get("late", 0),
        "short_hours": psum.get("short_hours", 0), "late_and_short": psum.get("late_and_short", 0),
        "absent": psum.get("absent", 0), "on_leave": psum.get("on_leave", 0),
        "in_progress": psum.get("in_progress", 0),
        "unpledged": psum.get("unpledged", 0), "pledged_pending": psum.get("pledged", 0),
        "pledged_days": pledged, "satisfied": satisfied,
        "planned_days": pledged,            # 兼容旧前端字段
        "elapsed_pledged": elapsed_pledged,
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
    camp = CampSession.query.get(sid)
    if not camp or not _camp_writable(camp):
        return jsonify({"code": 400, "message": "营期已归档，不可请假"}), 400
    try:
        sd_d, ed_d = date.fromisoformat(sd), date.fromisoformat(ed)
    except (ValueError, TypeError):
        return jsonify({"code": 400, "message": "日期格式错误"}), 400
    today = date.today()
    if sd_d > ed_d:
        return jsonify({"code": 400, "message": "开始日期不能晚于结束日期"}), 400
    if sd_d < today:
        return jsonify({"code": 400, "message": "不能对已过去的日期请假（今天之前）"}), 400
    if ed_d > camp.end_date or sd_d < camp.start_date:
        return jsonify({"code": 400, "message": f"请假日期须在营期范围内（{camp.start_date} ~ {camp.end_date}）"}), 400
    lv = CampLeave(camp_session_id=sid, user_id=user.id,
                   start_date=sd_d, end_date=ed_d,
                   reason=d.get("reason", ""))
    db.session.add(lv)
    db.session.flush()
    # 通知审批人：优先本营导生；营里没有导生则通知老师/超管（否则请假提交后无人知晓）
    approvers = [m.user_id for m in CampMember.query.filter_by(camp_session_id=sid, role='mentor').all()]
    if not approvers:
        approvers = [u.id for u in UserModel.query.filter(
            UserModel.role.in_(['teacher', 'super_admin'])).all()]
    for aid in approvers:
        create_notification(aid, "新的营期请假申请", f"{user.username} 申请请假 {sd}~{ed}",
                            category='camp', source_type='leave', source_id=lv.id, camp_session_id=sid)
    db.session.commit()
    return jsonify({"code": 200, "message": "已提交", "leave_id": lv.id})


@bp.route("/leave/<int:lid>/approve", methods=["POST"])
@jwt_required()
@camp_role('mentor')
def leave_approve(lid):
    user = _current_user()
    lv = CampLeave.query.get(lid)
    if not lv:
        return jsonify({"code": 404, "message": "请假记录不存在"}), 404
    if lv.status != 'pending':
        return jsonify({"code": 400, "message": "该请假已处理，不能重复审批（如需改判请先撤回）"}), 400
    # 导生仅限本团队；老师/超管不限
    if not (user.is_admin()):
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


@bp.route("/leave/<int:lid>/revoke", methods=["POST"])
@jwt_required()
@camp_role('mentor')
@audit_log(operation="撤回请假审批")
def leave_revoke(lid):
    """撤回已批准的请假（如误批）：status 回 pending、清空审批人字段，重新进入待审批，
    之后可再次批准或拒绝。考勤按 status='approved' 实时聚合（_approved_leave_dates），
    撤回即生效，看板/我的考勤中该段自动回算，无需迁移历史。"""
    user = _current_user()
    lv = CampLeave.query.get(lid)
    if not lv:
        return jsonify({"code": 404, "message": "请假记录不存在"}), 404
    if lv.status != 'approved':
        return jsonify({"code": 400, "message": "仅已批准的请假可撤回"}), 400
    # 与审批同权限：导生仅限本团队；老师/超管不限
    if not (user.is_admin()):
        if not _in_my_team(lv.camp_session_id, user, lv.user_id):
            return jsonify({"code": 403, "message": "无权撤回（非本团队）"}), 403
    lv.status = 'pending'
    lv.approver_id = None
    lv.approved_at = None
    db.session.flush()
    create_notification(lv.user_id, "请假审批已撤回",
                        f"你 {lv.start_date.isoformat()}~{lv.end_date.isoformat()} 的请假批准已被撤回，将重新审核",
                        category='camp', source_type='leave', source_id=lv.id,
                        camp_session_id=lv.camp_session_id)
    db.session.commit()
    return jsonify({"code": 200, "message": "已撤回，该请假重新进入待审批"})


@bp.route("/sessions/<int:sid>/leave")
@jwt_required()
@camp_role('mentor')
def leave_list(sid):
    user = _current_user()
    q = CampLeave.query.filter_by(camp_session_id=sid)
    visible = set(_visible_student_ids(sid, user))
    see_all = user.is_admin()
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
@camp_role('mentor')
def reward_issue():
    user = _current_user()
    d = request.json or {}
    sid, uid, medal_id = d.get("camp_session_id"), d.get("user_id"), d.get("medal_id")
    if not sid or not uid or not medal_id:
        return jsonify({"code": 400, "message": "缺少参数"}), 400
    camp = CampSession.query.get(sid)
    if not camp or not _camp_writable(camp):
        return jsonify({"code": 400, "message": "营期已归档，只读"}), 400
    if not (user.is_admin()):
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
@camp_role('mentor')
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
@camp_role()
def seat_assign():
    d = request.json or {}
    sid, seat_id, uid = d.get("camp_session_id"), d.get("seat_id"), d.get("user_id")
    if not sid or not seat_id:
        return jsonify({"code": 400, "message": "缺少参数"}), 400
    camp = CampSession.query.get(sid)
    if not camp or not _camp_writable(camp):
        return jsonify({"code": 400, "message": "营期已归档，只读"}), 400
    if not SeatModel.query.get(seat_id):
        return jsonify({"code": 404, "message": "座位不存在"}), 404
    if uid:
        if not CampMember.query.filter_by(camp_session_id=sid, user_id=uid).first():
            return jsonify({"code": 404, "message": "该用户不是本营期成员"}), 404
        # 一人一座：先解绑本营其他座位，避免同一人占多个座
        for other in CampSeat.query.filter(CampSeat.camp_session_id == sid,
                                           CampSeat.user_id == uid,
                                           CampSeat.seat_id != seat_id).all():
            other.user_id = None
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
    """招募指针：返回当前招募中的营期 + 当前用户是否成员 + 我的最新申请状态。
    消费方为 /camp-home 招募页与 /camp 空状态分流；成员工作台不消费（走 session_list）。"""
    user = _current_user()
    # 只展示进行中的营；归档/草稿营不该再作为招募入口
    camp = CampSession.query.filter(CampSession.is_featured == True, CampSession.status.in_(('upcoming','selecting'))).first()
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
@camp_role()
@audit_log(operation="设为招募营期")
def camp_feature(sid):
    camp = CampSession.query.get(sid)
    if not camp:
        return jsonify({"code": 404, "message": "营期不存在"}), 404
    if camp.status not in ('upcoming', 'selecting'):
        return jsonify({"code": 400, "message": "仅待开放/选择阶段的营期可设为招募营期"}), 400
    CampSession.query.filter(CampSession.is_featured.is_(True)).update({"is_featured": False})
    camp.is_featured = True
    db.session.commit()
    return jsonify({"code": 200, "message": "已设为招募营期"})


@bp.route("/sessions/<int:sid>/join-request", methods=["POST"])
@jwt_required()
@audit_log(operation="提交营期加入申请")
def join_request_submit(sid):
    """学员/导生自助提交加入申请（teacher/超管不申请，他们直接管营）。"""
    user = _current_user()
    camp = CampSession.query.get(sid)
    if not camp:
        return jsonify({"code": 404, "message": "营期不存在"}), 404
    if user.is_admin():
        return jsonify({"code": 400, "message": "管理员无需申请加入营期；导生经资格名单报名或由管理员分配"}), 400
    if camp.status != 'selecting':
        return jsonify({"code": 400, "message": "该营期当前未开放报名（仅选择阶段可申请加入）"}), 400
    if CampMember.query.filter_by(camp_session_id=sid, user_id=user.id).first():
        return jsonify({"code": 402, "message": "你已是该营期成员"}), 402
    if CampJoinRequest.query.filter_by(camp_session_id=sid, user_id=user.id, status='pending').first():
        return jsonify({"code": 409, "message": "已有待审批的申请，请等待审核"}), 409
    d = request.json or {}
    # 学员手选承诺出勤日（JSON 数组），校验格式 + 范围内 + 未来日 + 工作日营不含周末
    selected_days = d.get("selected_days") or []
    today = date.today()
    valid = []
    try:
        for s in selected_days:
            dv = date.fromisoformat(str(s))
            if not (camp.start_date <= dv <= camp.end_date) or dv < today:
                continue
            if camp.weekdays_only and dv.weekday() >= 5:
                continue
            valid.append(dv.isoformat())
    except (ValueError, TypeError):
        return jsonify({"code": 400, "message": "承诺出勤日格式错误"}), 400
    if not valid:
        return jsonify({"code": 400, "message": "请至少选择一个有效的承诺出勤日（未来、营期范围内" +
                        ("、工作日" if camp.weekdays_only else "") + "）"}), 400
    # 个别无效日静默剔除（前端日期格已限可选范围，此处兜底）；去重排序后落库
    db.session.add(CampJoinRequest(camp_session_id=sid, user_id=user.id,
                                   reason=d.get("reason"), selected_days=json.dumps(sorted(set(valid)))))
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
@camp_role()
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
@camp_role()
@audit_log(operation="批准营期申请")
def join_request_approve(rid):
    req = CampJoinRequest.query.get(rid)
    if not req:
        return jsonify({"code": 404, "message": "申请不存在"}), 404
    if req.status != 'pending':
        return jsonify({"code": 400, "message": "该申请已处理"}), 400
    d = request.json or {}
    # 学员审批时老师选定归属导生（body team_mentor_id）；auto_plan=False 改由手选日期建 plan
    # 启用选导生的营期：忽略 body 导生——学员先进营无导生，归属由开营前的选导生活动决定
    # （老师确需给插班生预分配时，走成员管理 member_assign / member_update 显式指定）
    _camp = CampSession.query.get(req.camp_session_id)
    join_mentor = None if (_camp and _camp.mentor_selection_enabled) else d.get("team_mentor_id")
    m, err = _assign_member(req.camp_session_id, req.user_id, join_mentor, auto_plan=False)
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
    elif camp and camp.mentor_selection_enabled:
        content += "你的导生将通过开营前的选导生活动确定，请留意通知。"
    create_notification(req.user_id, "入营申请已通过", content,
                        category='camp', source_type='join_request',
                        source_id=req.id, camp_session_id=req.camp_session_id)
    db.session.commit()
    return jsonify({"code": 200, "message": "已批准并加入营期", "member_id": m.id})


@bp.route("/join-requests/<int:rid>/reject", methods=["POST"])
@jwt_required()
@camp_role()
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
@camp_role()
@audit_log(operation="改派营期成员导生")
def member_update(sid, uid):
    """改成员归属导生（仅学员行可改；日常团队改派入口）。"""
    camp = CampSession.query.get(sid)
    if not camp:
        return jsonify({"code": 404, "message": "营期不存在"}), 404
    if not _camp_writable(camp):
        return jsonify({"code": 400, "message": "营期已归档，只读"}), 400
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
    # 启用选导生的营期：改派同步回写配对账本（结果页/看板与 live 链接保持一致）；清空归属则删账本行
    if camp.mentor_selection_enabled:
        row = CampMentorMatch.query.filter_by(camp_session_id=sid, student_user_id=uid).first()
        if team_mentor_id:
            if row:
                row.mentor_user_id = team_mentor_id
                row.round = None
                row.source = 'admin'
            else:
                db.session.add(CampMentorMatch(camp_session_id=sid, mentor_user_id=team_mentor_id,
                                               student_user_id=uid, round=None, source='admin'))
        elif row:
            db.session.delete(row)
    db.session.commit()
    return jsonify({"code": 200, "message": "已更新"})

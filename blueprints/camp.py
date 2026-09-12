"""营期（Camp）系统蓝图。

角色门控（@camp_role）：
  - 营期管理（建营/成员/课程/出勤计划/座位）→ teacher+
  - 导生可操作（请假审批/发奖励）→ mentor+ 且仅限本团队（内部校验）
  - 学员操作（选课/请假）→ 仅营期成员
营期看板（混合考勤算法）见 Phase D 的 /camp/attendance/dashboard/<sid>。
"""
import csv
import io
import json
from collections import defaultdict, Counter
from datetime import date, time, timedelta, datetime

from sqlalchemy import or_

from flask import Blueprint, Response, request, jsonify
from flask_jwt_extended import jwt_required

from exts import db, redis_client
from models import (
    CampSession, CampCycle, CampPolicy, CampMember, CampCourse, CampAttendancePlan,
    CampSeat, CampLeave, CampJoinRequest, CheckRecord, CourseModel, UserCourseModel,
    MedalModel, MedalUserModel, UserModel, SeatModel,
    CampMentorProfile, CampMentorPreference, CampMentorMatch,
    CampChapterCertification, Chapter, LessonModel, LearningProgressModel,
    CAMP_CATEGORY_DEFAULTS,
)

from . import camp_role, audit_log, _current_user
from .notification import create_notification
from .camp_ms import (_apply_ms_fields, _validate_ms, _ms_dict, _ms_phase, _ms_tags_list,
                      _ms_directions, _inherit_direction_course)

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
    返回同步后 plan 总数。（旧 plan_regenerate 只补不删，缩短营期后范围外脏 plan 残留继续判缺勤）
    09-12 三模式：非 daily（按周累计/不考勤）无承诺日体系，不同步只返回现存计数。"""
    if not _pledge_daily(camp):
        return CampAttendancePlan.query.filter_by(camp_session_id=camp.id).count()
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


def _weekly_stats(camp, user_ids):
    """按周累计考勤（09-12 模式 C：学期校区）：CheckRecord 按周一~周日分桶，
    周内出勤天数（有打卡记录的日）+ 时长合计；范围=营期起止 ∩ 昨日封顶（今天不完整不计）。
    返回 {uid: {"weeks": [{label, start, end, days, hours}], "total_days", "total_hours"}}。
    周边界算法与 codecheck /weekly_records 同款（weekday() 推算周一）。"""
    today = date.today()
    frm = camp.start_date
    to = min(camp.end_date, max(frm, today - timedelta(days=1)))
    buckets = {uid: {} for uid in user_ids}      # uid -> {week_start: {"dates": set, "hours": float}}
    if user_ids and frm <= to:
        records = CheckRecord.query.filter(
            CheckRecord.user_id.in_(list(user_ids)),
            CheckRecord.date.between(frm, to)).all()
        for r in records:
            ws = r.date - timedelta(days=r.date.weekday())
            b = buckets[r.user_id].setdefault(ws, {"dates": set(), "hours": 0.0})
            b["dates"].add(r.date)
            b["hours"] += r.duration or 0
    out = {}
    for uid in user_ids:
        weeks = []
        for ws in sorted(buckets[uid]):
            weeks.append({
                "label": f"{ws.isocalendar()[0]}-W{ws.isocalendar()[1]:02d}",
                "start": ws.isoformat(), "end": (ws + timedelta(days=6)).isoformat(),
                "days": len(buckets[uid][ws]["dates"]),
                "hours": round(buckets[uid][ws]["hours"], 2),
            })
        out[uid] = {"weeks": weeks,
                    "total_days": sum(w["days"] for w in weeks),
                    "total_hours": round(sum(w["hours"] for w in weeks), 2)}
    return out


def _policy_dict(p, category):
    """CampPolicy 序列化（无策略行时回退该营类型的代码默认值，保证契约形状稳定）。
    capabilities（v1.3）：行值与类型默认值合并——行上显式 false 关、未提到的位回退类型默认，
    永远返回完整位图（前端按位渲染 tab / 后端门禁端点用）。
    attendance_mode（09-12 三模式）：daily=假期营每日承诺出勤 / weekly=学期校区按周累计；
    模式 B（学期远程·不考勤）由 capabilities.attendance=false 承载。"""
    caps = dict(CAMP_CATEGORY_DEFAULTS.get(category or 'learning', {}).get('capabilities') or {})
    if p and p.capabilities:
        try:
            caps.update(json.loads(p.capabilities))
        except (ValueError, TypeError):
            pass
    if p:
        return {
            "application": p.application, "formation": p.formation,
            "match_rule": p.match_rule, "project_limit": p.project_limit,
            "course_policy": p.course_policy, "capabilities": caps,
            "attendance_mode": p.attendance_mode if (p.attendance_mode in ('daily', 'weekly')) else 'daily',
        }
    d = {k: v for k, v in CAMP_CATEGORY_DEFAULTS.get(
        category or 'learning', {}).items() if k != 'label'}
    d["capabilities"] = caps
    d["attendance_mode"] = 'daily'
    return d


def _capabilities(camp):
    """营期能力位图完整值（camp.py 内部门禁用；project 营首期考勤/请假/座位全关，v1.3）。"""
    return _policy_dict(camp.policy, camp.category)["capabilities"]


def _capability_enabled(camp, name):
    return bool(_capabilities(camp).get(name))


def _attendance_mode(camp):
    """考勤模式（09-12 三模式）：考勤能力关（模式 B）一律视为无承诺日；
    开着时按 policy.attendance_mode（daily/weekly，脏值回退 daily）。"""
    if not _capability_enabled(camp, 'attendance'):
        return 'off'
    return _policy_dict(camp.policy, camp.category).get('attendance_mode') or 'daily'


def _pledge_daily(camp):
    """是否启用「承诺出勤日」体系（模式 A）：plan 生成/同步/报名收日的总门。
    修现存缺陷：此前 attendance 能力关的营直接加成员仍照建 plan。"""
    return _attendance_mode(camp) == 'daily'


def _session_dict(c):
    return {
        "id": c.id, "name": c.name, "camp_type": c.camp_type,
        "category": c.category,
        "cycle_id": c.cycle_id,
        "cycle_code": c.cycle.code if c.cycle else None,
        "cycle_name": c.cycle.name if c.cycle else None,
        "policy": _policy_dict(c.policy, c.category),
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

def _mentor_import_report(sid, emails):
    """dry-run：emails → (匹配用户, 未匹配邮箱, 已在营成员 id 集)。"""
    matched, unmatched = [], []
    for e in dict.fromkeys(x.strip().lower() for x in emails if x and x.strip()):
        u = UserModel.query.filter(db.func.lower(UserModel.email) == e).first()
        if u:
            matched.append(u)
        else:
            unmatched.append(e)
    member_ids = {m.user_id for m in CampMember.query.filter_by(camp_session_id=sid).all()}
    return matched, unmatched, member_ids


@bp.route("/sessions/<int:sid>/mentor-import/preview", methods=["POST"])
@jwt_required()
@camp_role()
def mentor_import_preview(sid):
    """导入导生·dry-run 预览：body {emails:[...]}，不落库（确认导入走 /members/batch role=mentor）。"""
    emails = (request.json or {}).get("emails") or []
    matched, unmatched, member_ids = _mentor_import_report(sid, emails)
    return jsonify({"code": 200, "data": {
        "matched": [{"user_id": u.id, "email": u.email, "username": u.username,
                     "already_member": u.id in member_ids} for u in matched],
        "unmatched_emails": unmatched,
    }})


@bp.route("/sessions/<int:sid>/mentor-import/candidates-by-level", methods=["POST"])
@jwt_required()
@camp_role()
def mentor_import_candidates_by_level(sid):
    """导入导生·按等级挑人（只读）：返回 level >= min_level 的候选邮箱，回填导入框；
    排除管理员与在营成员。2026-09-12 导生改自由报名（LV≥2 报名窗口 upcoming/selecting），
    本端点从「物化进资格池」改为纯选人器，资格名单机制退役。"""
    camp = CampSession.query.get(sid)
    if not camp:
        return jsonify({"code": 404, "message": "营期不存在"}), 404
    min_level = (request.json or {}).get("min_level", 2)
    # 下限锁 2（D-2）：LV1 是全体普通学员的默认等级，放行会把整营学生扫进导生候选
    if min_level not in (2, 3, 4):
        return jsonify({"code": 400, "message": "min_level 仅支持 2-4（LV1 为普通学员，不作导生候选）"}), 400
    member_ids = {m.user_id for m in CampMember.query.filter_by(camp_session_id=sid).all()}
    users = [u for u in UserModel.query.filter(UserModel.level >= min_level).all()
             if not u.is_admin() and u.id not in member_ids]
    return jsonify({"code": 200,
                    "data": {"min_level": min_level, "count": len(users),
                             "emails": [u.email for u in users]}})


@bp.route("/sessions/<int:sid>/mentor-registration", methods=["POST"])
@jwt_required()
@audit_log(operation="导生报名入营")
def mentor_registration(sid):
    """导生自由报名（2026-09-12 起，资格名单机制退役）：LV≥2 用户在报名窗口
    （upcoming/selecting）自助提交，走 join-request 由管理员审核入营；幂等。"""
    user = _current_user()
    camp = CampSession.query.get(sid)
    if not camp:
        return jsonify({"code": 404, "message": "营期不存在"}), 404
    if user.is_admin():
        return jsonify({"code": 400, "message": "管理员无需申请加入营期"}), 400
    if camp.category == 'project':
        return jsonify({"code": 400, "message": "项目营无导生身份，请在开放报名后以成员身份申请"}), 400
    if (user.level or 1) < 2:
        return jsonify({"code": 403, "message": "导生报名需 LV2 及以上"}), 403
    if CampMember.query.filter_by(camp_session_id=sid, user_id=user.id).first():
        return jsonify({"code": 200, "message": "你已是本营成员，无需重复报名"}), 200
    if camp.status not in ("upcoming", "selecting"):
        return jsonify({"code": 400, "message": "导生报名已截止（仅待开放/选择阶段开放）"}), 400
    if CampJoinRequest.query.filter_by(camp_session_id=sid, user_id=user.id, status='pending').first():
        return jsonify({"code": 200, "message": "已提交报名申请，等待管理员审核"}), 200
    db.session.add(CampJoinRequest(camp_session_id=sid, user_id=user.id,
                                   reason="导生报名", selected_days="[]",
                                   apply_role="mentor"))
    db.session.commit()
    return jsonify({"code": 200, "message": "报名已提交，管理员审核通过后即可布置导生名片"})


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
    # v1.3 阶段4：结营自动冻结档案（幂等；项目营快照含项目/里程碑/成果+资产回流打标）。
    # 延迟导入防循环依赖（camp_delivery 反向引用本模块的 _camp_writable）。
    archived = False
    if dst_status == 'archived':
        try:
            from .camp_delivery import freeze_camp_archive
            archived = freeze_camp_archive(camp, _current_user().id) is not None
        except Exception:
            db.session.rollback()   # 冻结失败不阻断结营本身；档案可事后手动补冻结
    # 09-12 用户拍板：开营即选导生收官——open 时若志愿截止仍在未来，一律压到当前时刻
    # （演示/实战杠杆：一步停掉选导生阶段直接进正式开营）；清 Redis 阶段游标让 done 通知可发。
    ms_closed = False
    if dst_status == 'running' and camp.mentor_selection_enabled:
        now = datetime.now()
        if camp.ms_preference_deadline and camp.ms_preference_deadline > now:
            camp.ms_preference_deadline = now
            db.session.commit()
            ms_closed = True
        try:
            redis_client.delete(f"ms:phase_last:{camp.id}")
        except Exception:
            pass
    msg = f"已{'发布' if action=='publish' else '撤回发布' if action=='retract' else '开放报名' if action=='open_enrollment' else '开营' if action=='open' else '结营'}"
    if archived:
        msg += "（档案已冻结）"
    if ms_closed:
        msg += "，选导生志愿已同步截止"
    return jsonify({"code": 200, "message": msg, "session": _session_dict(camp)})


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
    category = d.get("category", "learning")
    if category not in CAMP_CATEGORY_DEFAULTS:
        return jsonify({"code": 400, "message": f"category 仅支持 {'/'.join(CAMP_CATEGORY_DEFAULTS)}"}), 400
    cycle = CampCycle.query.get(d.get("cycle_id")) if d.get("cycle_id") else None
    if not cycle:
        return jsonify({"code": 400, "message": "缺少有效的 cycle_id（教学周期），请先经 POST /camp/cycles 创建"}), 400
    # 营期策略：按类型默认值落一行（营期行可覆盖，方案 §3.3）
    defaults = CAMP_CATEGORY_DEFAULTS[category]
    policy = CampPolicy(
        application=defaults['application'], formation=defaults['formation'],
        match_rule=defaults['match_rule'], project_limit=defaults['project_limit'],
        course_policy=defaults['course_policy'],
        capabilities=json.dumps(defaults.get('capabilities') or {}))
    try:
        camp = CampSession(
            name=name, category=category, cycle_id=cycle.id,
            start_date=date.fromisoformat(start), end_date=date.fromisoformat(end),
            expected_check_in=time.fromisoformat(d["expected_check_in"]) if d.get("expected_check_in") else None,
            min_daily_hours=d.get("min_daily_hours"),
            weekdays_only=d.get("weekdays_only", True),
            policy=policy,
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
    # 当前用户的营内身份（非管理员可见性判定 + 卡片身份标记共用）
    my_rows = CampMember.query.filter_by(user_id=user.id).all()
    member_ids = {m.camp_session_id for m in my_rows}
    my_roles = {m.camp_session_id: m.role for m in my_rows}
    if not (user.is_admin()):
        # 营期中心可见性（方案 §3.3，五态）：成员的营（含进行中与历史）
        # + 全员可见的 upcoming（即将开始；导生报名窗口，LV≥2 可自助报名）/ selecting（可报名）。
        # draft 永不可见；running/archived 仅成员可见。
        conds = [CampSession.status.in_(('upcoming', 'selecting'))]
        if member_ids:
            conds.append(CampSession.id.in_(member_ids))
        q = q.filter(or_(*conds), CampSession.status != 'draft')
    camps = q.order_by(CampSession.start_date.desc()).all()
    # 附当前用户是否成员 + 营内任职（CampMember.role：student/mentor）。
    # 身份解耦后导生/学员是营内身份而非全局角色，用户端工作台 tab 分流改读 my_role；
    # 非成员（导生报名窗口内的 upcoming/selecting 营）my_role 为 null。
    return jsonify({"code": 200,
                    "sessions": [{**_session_dict(c), "is_member": c.id in member_ids,
                                  "my_role": my_roles.get(c.id)} for c in camps]})


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
    if not _camp_writable(camp):
        return jsonify({"code": 400, "message": "已结营营期只读，信息修正请走档案修正流程"}), 400
    d = request.json or {}
    old_start, old_end, old_weekdays = camp.start_date, camp.end_date, camp.weekdays_only
    for f in ["name", "weekdays_only", "min_daily_hours"]:  # status 走 /transitions；camp_type 已退役
        if f in d:
            setattr(camp, f, d[f])
    if "cycle_id" in d:                       # 教学周期可改（需真实存在）
        cyc = CampCycle.query.get(d["cycle_id"])
        if not cyc:
            db.session.rollback()
            return jsonify({"code": 400, "message": "教学周期不存在"}), 400
        camp.cycle_id = cyc.id
    try:
        if d.get("start_date"):
            camp.start_date = date.fromisoformat(d["start_date"])
        if d.get("end_date"):
            camp.end_date = date.fromisoformat(d["end_date"])
        # 09-12 修：按键出现与否更新（原来只认真值——期望到岗一旦设置无法清空）
        if "expected_check_in" in d:
            camp.expected_check_in = (time.fromisoformat(d["expected_check_in"])
                                      if d.get("expected_check_in") else None)
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
    # 营期策略覆盖（部分更新；category 不可改——策略行随类型生成，改类型=换营，建新营）
    if isinstance(d.get("policy"), dict):
        policy = camp.policy or CampPolicy(
            **{k: v for k, v in CAMP_CATEGORY_DEFAULTS.get(camp.category or 'learning', {}).items()
               if k != 'label'})
        if not camp.policy:
            camp.policy = policy
        for f in ("application", "formation", "match_rule", "project_limit", "course_policy"):
            if f in d["policy"]:
                setattr(policy, f, d["policy"][f])
        # 09-12 考勤三模式配置：attendance_mode（daily/weekly）+ attendance_enabled（bool，
        # 只覆写 capabilities.attendance 位，其余位不动；False=模式 B 不考勤）
        pd_ = d["policy"]
        old_mode = _attendance_mode(camp)
        if "attendance_mode" in pd_:
            if pd_["attendance_mode"] not in ("daily", "weekly"):
                db.session.rollback()
                return jsonify({"code": 400, "message": "attendance_mode 仅支持 daily/weekly"}), 400
            policy.attendance_mode = pd_["attendance_mode"]
        if "attendance_enabled" in pd_:
            try:
                caps = json.loads(policy.capabilities) if policy.capabilities else {}
            except (ValueError, TypeError):
                caps = {}
            caps["attendance"] = bool(pd_["attendance_enabled"])
            policy.capabilities = json.dumps(caps)
        db.session.flush()
        # 切离「每日承诺出勤」体系 → 清空本营承诺日（按周/不考勤不再用；切回 daily 手动重生成）
        if old_mode == 'daily' and _attendance_mode(camp) != 'daily':
            CampAttendancePlan.query.filter_by(camp_session_id=sid).delete(synchronize_session=False)
    # 同步冗余副本：改出勤时间/工时阈值后，已展开的 CampAttendancePlan 也跟着刷新，
    # 否则 _eval_day 仍按旧副本判定迟到/工时（见 _eval_day），管理员改的设置不生效。
    if "expected_check_in" in d or "min_daily_hours" in d:
        plans = CampAttendancePlan.query.filter_by(camp_session_id=sid).all()
        for p in plans:
            if "expected_check_in" in d:
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

def _assign_member(sid, user_id, team_mentor_id=None, auto_plan=True, role="student"):
    """营期成员分配核心：role 由调用方显式指定 + team_mentor 校验。
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
    # 身份解耦（1a）：营内角色由调用方显式指定（默认学员），不再从全局 user.role 派生；
    # 仅拒绝 super_admin 入营期成员（管理动作走管理入口）。
    # v1.3：+ 'member'=项目营通用入池值（负责人身份在单元层 CampUnitMember.role=leader）。
    if user.is_admin():
        return None, ("教师/超管通过营期管理入口操作，不作为营期成员加入", 400)
    if role not in ('student', 'mentor', 'member'):
        return None, ("营内角色仅支持 student/mentor/member", 400)
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
    if auto_plan and role == 'student' and _pledge_daily(camp):
        _gen_plan(camp, user_id)          # 直接加成员：按工作日生成（兜底；按周/不考勤模式无承诺日）
    # 方向制继承（09-12）：学员归属导生 → 自动入读该方向绑定的课程（不 commit，随调用方事务）
    if role == 'student' and team_mentor_id:
        _inherit_direction_course(camp, user_id, team_mentor_id)
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
    m, err = _assign_member(sid, user_id, d.get("team_mentor_id"), role=d.get("role", "student"))
    if err:
        msg, code = err
        return jsonify({"code": code, "message": msg}), code
    db.session.commit()
    return jsonify({"code": 200, "message": "已加入", "member_id": m.id})


@bp.route("/sessions/<int:sid>/members/batch", methods=["POST"])
@jwt_required()
@camp_role()
@audit_log(operation="批量分配营期成员")
def member_assign_batch(sid):
    """事务批量加成员（v1.3 阶段3 收编 admin 前端逐人并发 POST）。逐项校验+逐项回报，
    契约红线：部分成功必须在回包逐项列出，不允许把部分成功显示为全部成功。
    body: {items: [{user_id, role?, team_mentor_id?}, ...]}；role 缺省按营 category
    （project=member / learning=student）。单事务提交，全部失败不落库。"""
    camp = CampSession.query.get(sid)
    if not camp:
        return jsonify({"code": 404, "message": "营期不存在"}), 404
    if not _camp_writable(camp):
        return jsonify({"code": 400, "message": "营期已归档，只读"}), 400
    items = (request.json or {}).get("items")
    if not isinstance(items, list) or not items:
        return jsonify({"code": 400, "message": "缺少 items 数组"}), 400
    if len(items) > 200:
        return jsonify({"code": 400, "message": "单次批量上限 200 人"}), 400
    default_role = 'member' if camp.category == 'project' else 'student'
    results, ok = [], 0
    for it in items:
        uid = it.get("user_id") if isinstance(it, dict) else None
        if not uid:
            results.append({"user_id": uid, "status": "failed", "message": "缺少 user_id"})
            continue
        m, err = _assign_member(sid, uid, it.get("team_mentor_id"),
                                role=it.get("role") or default_role)
        if err:
            msg, _code = err
            results.append({"user_id": uid, "status": "failed", "message": msg})
        else:
            results.append({"user_id": uid, "status": "added", "member_id": m.id})
            ok += 1
    if ok:
        db.session.commit()
    return jsonify({"code": 200, "message": f"已加入 {ok}/{len(items)} 人",
                    "added": ok, "results": results})


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
    # 09-12 方向制砍双源：learning 营课程由分类方向定义，不再手动挂课（CampCourse 留给项目营）
    if camp.category == 'learning':
        return jsonify({"code": 400, "message": "培训营课程由分类方向定义，请在营期设置的分类中绑定课程"}), 400
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
    if camp.category == 'learning':
        return jsonify({"code": 400, "message": "培训营课程由分类方向定义，无手动移除"}), 400
    cc = CampCourse.query.filter_by(camp_session_id=sid, course_id=cid).first()
    if not cc:
        return jsonify({"code": 404, "message": "该课程不在营期中"}), 404
    db.session.delete(cc)
    db.session.commit()
    return jsonify({"code": 200, "message": "已移除"})


@bp.route("/sessions/<int:sid>/courses")
@jwt_required()
def course_list(sid):
    camp = CampSession.query.get(sid)
    if not camp:
        return jsonify({"code": 404, "message": "营期不存在"}), 404
    data = []
    if camp.category == 'learning':
        # 方向制（09-12）：learning 营课程目录从分类方向派生（course 去重，形状不变）
        seen = set()
        for d in _ms_directions(camp):
            if d["course_id"] is None or d["course_id"] in seen:
                continue
            c = CourseModel.query.get(d["course_id"])
            if c:
                data.append({"course_id": c.id, "title": c.title, "difficulty": c.difficulty})
                seen.add(c.id)
    else:
        ccs = CampCourse.query.filter_by(camp_session_id=sid).order_by(CampCourse.sort_order).all()
        for cc in ccs:
            c = CourseModel.query.get(cc.course_id)
            if c:
                data.append({"course_id": c.id, "title": c.title, "difficulty": c.difficulty})
    return jsonify({"code": 200, "courses": data})


# 09-12 方向制：学生自主选课整体下线（POST /camp/selection 与 GET /camp/selection/mine 已删）。
# 入课唯一途径 = 归属导生继承方向课程（camp_ms._inherit_direction_course，挂 _assign_member/
# member_update/pick/assign/assign_batch 五处写入点）。


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
    if not _capability_enabled(camp, 'attendance'):
        return jsonify({"code": 400, "message": "本营期未启用考勤（能力开关关闭）"}), 400
    if not _camp_writable(camp):
        return jsonify({"code": 400, "message": "营期已归档，只读"}), 400
    if not _pledge_daily(camp):
        return jsonify({"code": 400, "message": "本营考勤模式为按周累计，无承诺出勤日"}), 400
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
    if not _capability_enabled(camp, 'attendance'):
        return jsonify({"code": 400, "message": "本营期未启用考勤（能力开关关闭）"}), 400
    user = _current_user()

    # 09-12 模式 C（学期校区·按周累计）：导生/老师看本团队学员的周分桶统计
    if _attendance_mode(camp) == 'weekly':
        visible = _visible_student_ids(sid, user)
        stats = _weekly_stats(camp, visible)
        users = {u.id: u for u in UserModel.query.filter(UserModel.id.in_(visible))} if visible else {}
        rows = [{"user_id": uid, "username": users[uid].username if uid in users else "",
                 **stats[uid]} for uid in visible]
        return jsonify({"code": 200, "mode": "weekly",
                        "range": {"from": camp.start_date.isoformat(),
                                  "to": camp.end_date.isoformat()},
                        "rows": rows})

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
        "code": 200, "mode": "daily",
        "range": {"from": frm.isoformat(), "to": to.isoformat()},
        "dates": sorted(d.isoformat() for d in dates_set),
        "summary": summary,
        "rows": rows,
    })


@bp.route("/attendance/export/<int:sid>")
@jwt_required()
@camp_role('mentor')
@audit_log(operation="导出营期考勤")
def attendance_export(sid):
    """考勤 CSV（utf-8-sig 带 BOM，Excel 可直接打开）：存档/线下核算用。
    可见范围同 dashboard（导生=本团队，老师/超管=全营）。
    daily=学员×日期矩阵+个人汇总列（?from=&to= 生效，缺省营期全范围）；
    weekly=学员×周分桶（每格 次数/小时）+累计列（range 钉营期，参数不生效）。"""
    camp = CampSession.query.get(sid)
    if not camp:
        return jsonify({"code": 404, "message": "营期不存在"}), 404
    if not _capability_enabled(camp, 'attendance'):
        return jsonify({"code": 400, "message": "本营期未启用考勤（能力开关关闭）"}), 400
    user = _current_user()

    buf = io.StringIO()
    w = csv.writer(buf)
    visible = _visible_student_ids(sid, user)
    users = {u.id: u.username for u in
             UserModel.query.filter(UserModel.id.in_(visible))} if visible else {}

    if _attendance_mode(camp) == 'weekly':
        stats = _weekly_stats(camp, visible)
        weeks = sorted({wk["label"] for st in stats.values() for wk in st["weeks"]})
        w.writerow(["学员ID", "学员", *weeks, "累计次数", "累计时长(h)"])
        for uid in visible:
            st = stats[uid]
            by_label = {wk["label"]: wk for wk in st["weeks"]}
            w.writerow([uid, users.get(uid, ""),
                        *[f"{by_label[lbl]['days']}次/{by_label[lbl]['hours']}h"
                          if lbl in by_label else "" for lbl in weeks],
                        st["total_days"], st["total_hours"]])
        fname = f"camp_{sid}_attendance_weekly.csv"
    else:
        try:
            frm = date.fromisoformat(request.args.get("from")) if request.args.get("from") else camp.start_date
            to = date.fromisoformat(request.args.get("to")) if request.args.get("to") else camp.end_date
        except ValueError:
            return jsonify({"code": 400, "message": "日期格式错误，需 YYYY-MM-DD"}), 400
        if frm > to:
            return jsonify({"code": 400, "message": "from 不能晚于 to"}), 400
        today = date.today()
        # 与 dashboard 同口径：plans 按 (user,date) 索引，CheckRecord 聚合，_eval_day 判日
        all_days = _weekdays(frm, to)
        plan_map = defaultdict(dict)
        if visible:
            for p in CampAttendancePlan.query.filter(
                    CampAttendancePlan.camp_session_id == sid,
                    CampAttendancePlan.user_id.in_(visible),
                    CampAttendancePlan.date.between(frm, to)).all():
                plan_map[p.user_id][p.date] = p
        checks_map = defaultdict(list)
        if visible:
            for r in CheckRecord.query.filter(
                    CheckRecord.user_id.in_(visible),
                    CheckRecord.date.between(frm, to)).all():
                checks_map[(r.user_id, r.date)].append(r)
        leave_set = _approved_leave_dates(sid, frm, to)
        # 矩阵格文案：9 态收敛（今天 absent 不下结论 → 待考勤），未承诺留空
        CELL_TEXT = {"present": "出勤", "late": "出勤·迟到", "short_hours": "未达标",
                     "late_and_short": "未达标·迟到", "absent": "缺勤", "on_leave": "请假",
                     "pledged": "待考勤", "in_progress": "进行中", "unpledged": ""}
        w.writerow(["学员ID", "学员", "出勤(准时)", "出勤(迟到)", "未达标", "缺勤", "请假", "达标率",
                    *[d.isoformat() for d in all_days]])
        for uid in visible:
            pm = plan_map.get(uid, {})
            cells, psum = [], Counter()
            for d in all_days:
                p = pm.get(d)
                if not p:
                    res = {"status": "unpledged"}
                elif d > today:
                    res = {"status": "pledged"}
                else:
                    res = _eval_day(checks_map.get((uid, d), []), p, (uid, d) in leave_set,
                                    is_today=(d == today))
                    if res["status"] == "absent" and d == today:
                        res = {"status": "pledged"}
                psum[res["status"]] += 1
                cells.append(CELL_TEXT.get(res["status"], res["status"]))
            satisfied = psum.get("present", 0) + psum.get("late", 0)
            elapsed = sum(1 for d in pm if d < today)
            rate = f"{round(satisfied / elapsed * 100)}%" if elapsed else ""
            w.writerow([uid, users.get(uid, ""),
                        psum.get("present", 0), psum.get("late", 0),
                        psum.get("short_hours", 0) + psum.get("late_and_short", 0),
                        psum.get("absent", 0), psum.get("on_leave", 0), rate, *cells])
        fname = f"camp_{sid}_attendance_daily.csv"

    resp = Response(buf.getvalue().encode("utf-8-sig"), mimetype="text/csv")
    resp.headers["Content-Disposition"] = f"attachment; filename={fname}"
    return resp


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
    # 09-12 模式 C（学期校区·按周累计）：无承诺日，返回周分桶统计
    if _attendance_mode(camp) == 'weekly':
        stats = _weekly_stats(camp, [user.id])[user.id]
        return jsonify({"code": 200, "mode": "weekly",
                        "range": {"from": camp.start_date.isoformat(), "to": camp.end_date.isoformat()},
                        **stats})
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
        "code": 200, "mode": "daily",
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
    if not _capability_enabled(camp, 'leave'):
        return jsonify({"code": 400, "message": "本营期未启用请假（能力开关关闭）"}), 400
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
    _lv_camp = CampSession.query.get(lv.camp_session_id)
    if _lv_camp and not _capability_enabled(_lv_camp, 'leave'):
        return jsonify({"code": 400, "message": "本营期未启用请假（能力开关关闭）"}), 400
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
    _rk_lv = CampLeave.query.get(lid)
    if _rk_lv:
        _rk_camp = CampSession.query.get(_rk_lv.camp_session_id)
        if _rk_camp and not _capability_enabled(_rk_camp, 'leave'):
            return jsonify({"code": 400, "message": "本营期未启用请假（能力开关关闭）"}), 400
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
    _camp_lv = CampSession.query.get(sid)
    if _camp_lv and not _capability_enabled(_camp_lv, 'leave'):
        return jsonify({"code": 400, "message": "本营期未启用请假（能力开关关闭）"}), 400
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
    if not _capability_enabled(camp, 'seat'):
        return jsonify({"code": 400, "message": "本营期未启用座位（能力开关关闭）"}), 400
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
    _st_camp = CampSession.query.get(sid)
    if _st_camp and not _capability_enabled(_st_camp, 'seat'):
        return jsonify({"code": 400, "message": "本营期未启用座位（能力开关关闭）"}), 400
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
        return jsonify({"code": 400, "message": "管理员无需申请加入营期"}), 400
    if camp.status != 'selecting':
        return jsonify({"code": 400, "message": "该营期当前未开放报名（仅选择阶段可申请加入）"}), 400
    if CampMember.query.filter_by(camp_session_id=sid, user_id=user.id).first():
        return jsonify({"code": 402, "message": "你已是该营期成员"}), 402
    if CampJoinRequest.query.filter_by(camp_session_id=sid, user_id=user.id, status='pending').first():
        return jsonify({"code": 409, "message": "已有待审批的申请，请等待审核"}), 409
    d = request.json or {}
    # 项目营（v1.3 阶段3）：apply_role='member'（通用入池值，负责人身份在单元层）；
    # 考勤能力未开时承诺出勤日不收集（前端 CampJoin 按 category+capability 分发表单）
    is_project = camp.category == 'project'
    apply_role = 'member' if is_project else 'student'
    # 09-12 三模式：承诺出勤日仅模式 A（假期营·每日）收集；按周累计（C）/不考勤（B）不收
    need_days = _pledge_daily(camp)
    # 09-12 砍意向大组：学员报名不再选组，组别随归属导生继承（导生组=名片 tags）；
    # preferred_tag 列保留存历史行，新申请不再写入（客户端误传也忽略）
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
    if need_days and not valid:
        return jsonify({"code": 400, "message": "请至少选择一个有效的承诺出勤日（未来、营期范围内" +
                        ("、工作日" if camp.weekdays_only else "") + "）"}), 400
    # 个别无效日静默剔除（前端日期格已限可选范围，此处兜底）；去重排序后落库
    db.session.add(CampJoinRequest(camp_session_id=sid, user_id=user.id,
                                   reason=d.get("reason"),
                                   apply_role=apply_role,
                                   selected_days=json.dumps(sorted(set(valid)))))
    db.session.commit()
    return jsonify({"code": 200, "message": "申请已提交，等待审批"})


@bp.route("/sessions/<int:sid>/join-request/cancel", methods=["POST"])
@jwt_required()
def join_request_cancel(sid):
    """撤回本人待审批的加入申请（导生报名/学员入营通用——手滑提交可反悔，2026-09-03 用户要求）。
    只允许撤 pending：已审批（approved/rejected）的历史不可撤；撤回后可重新提交（新行）。
    按 营+人 定位本人最新 pending 行，前端无需跟踪申请 id。"""
    user = _current_user()
    row = CampJoinRequest.query.filter_by(
        camp_session_id=sid, user_id=user.id, status='pending'
    ).order_by(CampJoinRequest.created_at.desc()).first()
    if not row:
        return jsonify({"code": 404, "message": "没有可撤回的待审批申请"}), 404
    row.status = 'cancelled'
    db.session.commit()
    return jsonify({"code": 200, "message": "已撤回申请"})


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
            "reason": r.reason,
            "status": r.status, "apply_role": r.apply_role or "student",
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
            "reason": r.reason,
            "status": r.status, "apply_role": r.apply_role or "student",
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
    m, err = _assign_member(req.camp_session_id, req.user_id, join_mentor, auto_plan=False,
                            role=(req.apply_role or "student"))
    if err:
        msg, code = err
        return jsonify({"code": code, "message": msg}), code
    # 用学员申请时手选的承诺日建 CampAttendancePlan（替代 _gen_plan 自动工作日）
    # 09-12 三模式：仅模式 A（每日承诺出勤）建 plan；按周累计/不考勤跳过
    plan_camp = CampSession.query.get(req.camp_session_id) if m.role == 'student' else None
    if plan_camp and _pledge_daily(plan_camp):
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
                    expected_check_in=plan_camp.expected_check_in, min_daily_hours=plan_camp.min_daily_hours))
        else:
            _gen_plan(plan_camp, req.user_id)   # 兜底：申请没带手选日则按工作日
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
    # 方向制继承（09-12）：改派到新导生 → 继承其方向课程（旧课程行保留为学习历史）
    if team_mentor_id:
        _inherit_direction_course(camp, uid, team_mentor_id)
    db.session.commit()
    return jsonify({"code": 200, "message": "已更新"})


# ─────────────────────────────────────────────
# 方向制学习（2026-09-12，migrate_24）：团队进度 + 导生按章认证
# ─────────────────────────────────────────────

def _direction_of_mentor(camp, mentor_uid):
    """导生的方向定义：名片 tags[0] → _ms_directions 匹配；无名片/无课程返回 None。"""
    p = CampMentorProfile.query.filter_by(camp_session_id=camp.id, user_id=mentor_uid).first()
    if not p or not p.tags:
        return None
    try:
        tags = json.loads(p.tags)
    except (ValueError, TypeError):
        return None
    if not (isinstance(tags, list) and tags):
        return None
    return next((d for d in _ms_directions(camp) if d["name"] == str(tags[0])), None)


def _chapters_payload(camp, course_id, student_uid):
    """章节平铺 + 学员自报完成比 + 认证态。返回 (chapters, certified_count)。"""
    chs = (Chapter.query.filter_by(course_id=course_id)
           .order_by(Chapter.order, Chapter.id).all())
    lesson_total = defaultdict(int)
    for l in LessonModel.query.filter_by(course_id=course_id).all():
        lesson_total[l.chapter_id] += 1
    done_rows = LearningProgressModel.query.filter(
        LearningProgressModel.user_id == student_uid,
        LearningProgressModel.course_id == course_id,
        LearningProgressModel.status == LearningProgressModel.STATUS_COMPLETED).all()
    lesson_done = defaultdict(int)
    for r in done_rows:
        lesson_done[r.chapter_id] += 1
    certs = {c.chapter_id: c for c in CampChapterCertification.query.filter_by(
        camp_session_id=camp.id, student_user_id=student_uid).all()
        if c.course_id == course_id}
    out = []
    for ch in chs:
        cert = certs.get(ch.id)
        out.append({
            "chapter_id": ch.id, "name": ch.name, "order": ch.order,
            "lessons": lesson_total.get(ch.id, 0),
            "lessons_completed": min(lesson_done.get(ch.id, 0), lesson_total.get(ch.id, 0)),
            "certified": cert is not None,
            "certified_at": cert.certified_at.strftime("%Y-%m-%d %H:%M") if cert else None,
            "certified_by": cert.mentor_user_id if cert else None,
        })
    return out, sum(1 for c in out if c["certified"])


@bp.route("/sessions/<int:sid>/team/progress")
@jwt_required()
@camp_role('mentor')
def team_progress(sid):
    """导生视角：本团队每学员的方向课程章节进度 + 认证态（按章认证的读端点）。"""
    camp = CampSession.query.get(sid)
    if not camp:
        return jsonify({"code": 404, "message": "营期不存在"}), 404
    user = _current_user()
    direction = _direction_of_mentor(camp, user.id)
    base = {"direction": direction["name"] if direction else None,
            "course_id": direction["course_id"] if direction else None}
    if not direction or direction["course_id"] is None:
        return jsonify({"code": 200, **base, "students": [],
                        "message": "尚未设置方向或方向未绑定课程"})
    course = CourseModel.query.get(direction["course_id"])
    students = (CampMember.query.filter_by(camp_session_id=sid, role='student')
                .order_by(CampMember.user_id).all())
    data = []
    for s in students:
        if not _in_my_team(sid, user, s.user_id) and not user.is_admin():
            continue
        chapters, certified = _chapters_payload(camp, direction["course_id"], s.user_id)
        u = UserModel.query.get(s.user_id)
        uc = UserCourseModel.query.filter_by(
            user_id=s.user_id, course_id=direction["course_id"]).first()
        data.append({
            "student_user_id": s.user_id, "username": u.username if u else "",
            "chapters": chapters, "certified_chapters": certified,
            "total_chapters": len(chapters),
            "course_status": uc.status if uc else None,
        })
    return jsonify({"code": 200, **base, "course_title": course.title if course else "",
                    "students": data})


@bp.route("/sessions/<int:sid>/team/progress/certify", methods=["POST", "DELETE"])
@jwt_required()
@camp_role('mentor')
@audit_log(operation="认证学员章节进度")
def team_progress_certify(sid):
    """导生按章认证（幂等）/撤销。body: {student_user_id, chapter_id}。
    全章认证齐 → user_course.status 自动置 completed（汇总态，撤销不回滚）。"""
    camp = CampSession.query.get(sid)
    if not camp:
        return jsonify({"code": 404, "message": "营期不存在"}), 404
    if not _camp_writable(camp):
        return jsonify({"code": 400, "message": "营期已归档，只读"}), 400
    user = _current_user()
    d = request.json or {}
    student_uid, chapter_id = d.get("student_user_id"), d.get("chapter_id")
    if not student_uid or not chapter_id:
        return jsonify({"code": 400, "message": "缺少 student_user_id/chapter_id"}), 400
    if not user.is_admin() and not _in_my_team(sid, user, student_uid):
        return jsonify({"code": 403, "message": "仅本团队学员可认证"}), 403
    direction = _direction_of_mentor(camp, user.id)
    if not direction or direction["course_id"] is None:
        return jsonify({"code": 400, "message": "你尚未设置方向（或方向未绑定课程）"}), 400
    ch = Chapter.query.filter_by(id=chapter_id, course_id=direction["course_id"]).first()
    if not ch:
        return jsonify({"code": 404, "message": "章节不在你的方向课程内"}), 404
    row = CampChapterCertification.query.filter_by(
        camp_session_id=sid, student_user_id=student_uid, chapter_id=chapter_id).first()
    if request.method == "DELETE":
        if not row:
            return jsonify({"code": 404, "message": "该章节尚未认证"}), 404
        db.session.delete(row)
        db.session.commit()
        return jsonify({"code": 200, "message": "已撤销认证"})
    if not row:
        db.session.add(CampChapterCertification(
            camp_session_id=sid, student_user_id=student_uid, chapter_id=chapter_id,
            course_id=direction["course_id"], mentor_user_id=user.id))
    # 全章认证齐 → 课程级 completed（死常量启用；撤销不回滚）
    chapters, certified = _chapters_payload(camp, direction["course_id"], student_uid)
    if chapters and certified >= len(chapters):
        uc = UserCourseModel.query.filter_by(
            user_id=student_uid, course_id=direction["course_id"]).first()
        if uc and uc.status == UserCourseModel.STATUS_ACTIVE:
            uc.status = UserCourseModel.STATUS_COMPLETED
            create_notification(student_uid, "学习进度已认证",
                                f"「{camp.name}」全部章节已由导生认证，课程学习完成。",
                                category='camp', source_type='mentor_selection',
                                source_id=camp.id, camp_session_id=sid, is_important=True)
    db.session.commit()
    return jsonify({"code": 200, "message": "已认证"})


@bp.route("/sessions/<int:sid>/my-direction")
@jwt_required()
def my_direction(sid):
    """学员视角：我的方向（随归属导生继承）+ 课程 + 章节认证进度 + 自报完成比。"""
    camp = CampSession.query.get(sid)
    if not camp:
        return jsonify({"code": 404, "message": "营期不存在"}), 404
    user = _current_user()
    m = CampMember.query.filter_by(camp_session_id=sid, user_id=user.id).first()
    if not m or m.role != 'student':
        return jsonify({"code": 403, "message": "仅本营学员可查看"}), 403
    if not m.team_mentor_id:
        return jsonify({"code": 200, "direction": None, "course": None,
                        "hint": "尚未归属导生——开放报名后选择导生，将自动继承其方向与课程"})
    direction = _direction_of_mentor(camp, m.team_mentor_id)
    if not direction or direction["course_id"] is None:
        return jsonify({"code": 200, "direction": direction["name"] if direction else None,
                        "course": None,
                        "hint": "归属导生尚未设置方向，请联系导生完善名片"})
    course = CourseModel.query.get(direction["course_id"])
    chapters, certified = _chapters_payload(camp, direction["course_id"], user.id)
    uc = UserCourseModel.query.filter_by(
        user_id=user.id, course_id=direction["course_id"]).first()
    mentor = UserModel.query.get(m.team_mentor_id)
    return jsonify({"code": 200, "direction": direction["name"],
                    "mentor_name": mentor.username if mentor else "",
                    "course": {"course_id": course.id, "title": course.title,
                               "difficulty": course.difficulty} if course else None,
                    "chapters": chapters, "certified_chapters": certified,
                    "total_chapters": len(chapters),
                    "course_status": uc.status if uc else None})

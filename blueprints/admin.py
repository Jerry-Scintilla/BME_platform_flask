"""
管理员聚合看板蓝图

GET /admin/overview —— 后台首页一次性聚合各业务域统计
（用户总数 / 今日新增 / 今日打卡 / 文章 / 课程 / 勋章发放 / 小组 / 营期 / 待审批配额），
供 admin 管理面板首页（DashboardComponent）渲染真实数据，替代原先的硬编码假数据。

只读接口：挂 @jwt_required + @check_permission('system_management')；super_admin 经 is_admin_like() 直通。
"""
from datetime import date

from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required, get_jwt_identity
from sqlalchemy import func

from exts import db
from models import (
    UserModel,
    ArticleModel,
    CourseModel,
    MedalUserModel,
    CourseGroup,
    CampSession,
    CheckRecord,
    LLMQuotaRequestModel,
    CampJoinRequest,
    CampLeave,
    CampMember,
    CampAttendancePlan,
    CampUnit,
    ProjectApplicationVersion,
    CampMilestone,
    CampSubmissionVersion,
    SeatModel,
)
from . import check_permission, audit_log, _current_user

bp = Blueprint("admin", __name__, url_prefix="/admin")


@bp.route("/users/<int:user_id>/role", methods=["PUT"])
@jwt_required()
@check_permission('system_management')
@audit_log(operation="设置用户全局角色")
def admin_set_user_role(user_id):
    """设置用户全局角色与管理员标签。

    身份解耦（Phase 1a）后任命/撤销管理员的唯一入口。
    body: {"role": "super_admin" | "user", "admin_tag": "teacher" | "developer" | null}
    """
    data = request.get_json(silent=True) or {}
    role = data.get('role')
    admin_tag = data.get('admin_tag')

    if role not in ('super_admin', 'user'):
        return jsonify({"code": 400, "message": "role 仅支持 super_admin / user"}), 400
    if role != 'super_admin':
        admin_tag = None
    elif admin_tag not in ('teacher', 'developer', None):
        return jsonify({"code": 400, "message": "admin_tag 仅支持 teacher / developer"}), 400

    target = UserModel.query.get(user_id)
    if not target:
        return jsonify({"code": 404, "message": "用户不存在"}), 404

    current = _current_user()
    if current and current.id == target.id and role != 'super_admin':
        return jsonify({"code": 400, "message": "不能撤销自己的管理员权限"}), 400

    old_role = target.role
    old_tag = target.admin_tag
    target.role = role
    target.admin_tag = admin_tag
    db.session.commit()

    return jsonify({
        "code": 200,
        "message": "角色已更新",
        "data": {
            "user_id": target.id,
            "username": target.username,
            "old_role": old_role, "role": target.role,
            "old_admin_tag": old_tag, "admin_tag": target.admin_tag,
        },
    })


@bp.route("/users/<int:user_id>/level", methods=["PUT"])
@jwt_required()
@check_permission('system_management')
@audit_log(operation="调整用户等级")
def admin_set_user_level(user_id):
    """调整用户等级（LV1-4）。等级地基：现阶段手动，评价引擎属阶段 3。"""
    level = (request.get_json(silent=True) or {}).get('level')
    if level not in (1, 2, 3, 4):
        return jsonify({"code": 400, "message": "level 仅支持 1-4"}), 400
    target = UserModel.query.get(user_id)
    if not target:
        return jsonify({"code": 404, "message": "用户不存在"}), 404
    old = target.level
    target.level = level
    db.session.commit()
    return jsonify({"code": 200, "message": f"等级已从 LV{old} 调整为 LV{level}",
                    "data": {"user_id": target.id, "username": target.username, "old_level": old, "level": level}})


@bp.route("/users/level/batch", methods=["POST"])
@jwt_required()
@check_permission('system_management')
@audit_log(operation="批量升级用户等级")
def admin_level_batch():
    """批量升级一级（09-16）：body {user_ids: [...]}，每人在当前等级上 +1；
    LV4 不再上升（逐项 failed 回报），camp/members 批量同款契约——逐项校验逐项
    列明、单事务提交、全部失败不落库；上限 200 人。"""
    user_ids = (request.get_json(silent=True) or {}).get('user_ids')
    if not isinstance(user_ids, list) or not user_ids:
        return jsonify({"code": 400, "message": "缺少 user_ids 数组"}), 400
    if len(user_ids) > 200:
        return jsonify({"code": 400, "message": "单次批量上限 200 人"}), 400
    results, ok = [], 0
    for uid in user_ids:
        target = UserModel.query.get(uid)
        if not target:
            results.append({"user_id": uid, "status": "failed", "message": "用户不存在"})
            continue
        old = target.level or 1
        if old >= 4:
            results.append({"user_id": uid, "username": target.username,
                            "status": "failed", "message": "已是最高等级 LV4"})
            continue
        target.level = old + 1
        results.append({"user_id": uid, "username": target.username, "status": "upgraded",
                        "old_level": old, "level": old + 1})
        ok += 1
    if ok:
        db.session.commit()
    return jsonify({"code": 200, "message": f"已升级 {ok}/{len(user_ids)} 人",
                    "upgraded": ok, "results": results})


@bp.route("/users/level/batch_set", methods=["POST"])
@jwt_required()
@check_permission('system_management')
@audit_log(operation="批量设定用户等级")
def admin_level_batch_set():
    """批量设为指定等级（09-17）：body {user_ids: [...], level: 1-4}；
    已在目标等级的逐项 skipped（不算失败），与 batch 同款契约——逐项校验逐项
    列明、单事务提交、全部失败不落库；上限 200 人。"""
    data = request.get_json(silent=True) or {}
    user_ids = data.get('user_ids')
    level = data.get('level')
    if level not in (1, 2, 3, 4):
        return jsonify({"code": 400, "message": "level 仅支持 1-4"}), 400
    if not isinstance(user_ids, list) or not user_ids:
        return jsonify({"code": 400, "message": "缺少 user_ids 数组"}), 400
    if len(user_ids) > 200:
        return jsonify({"code": 400, "message": "单次批量上限 200 人"}), 400
    results, ok, skipped = [], 0, 0
    for uid in user_ids:
        target = UserModel.query.get(uid)
        if not target:
            results.append({"user_id": uid, "status": "failed", "message": "用户不存在"})
            continue
        old = target.level or 1
        if old == level:
            skipped += 1
            results.append({"user_id": uid, "username": target.username, "status": "skipped",
                            "old_level": old, "level": level})
            continue
        target.level = level
        results.append({"user_id": uid, "username": target.username, "status": "set",
                        "old_level": old, "level": level})
        ok += 1
    if ok:
        db.session.commit()
    message = f"已调整 {ok}/{len(user_ids)} 人"
    if skipped:
        message += f"（{skipped} 人已是 LV{level}）"
    return jsonify({"code": 200, "message": message,
                    "updated": ok, "results": results})


@bp.route("/users/<int:user_id>", methods=["PUT"])
@jwt_required()
@check_permission('system_management')
@audit_log(operation="编辑用户资料")
def admin_update_user(user_id):
    """用户管理·合并编辑：body 可含 username / role / admin_tag / level（均可选，至少一项）。

    吸收 UserManage 编辑弹窗的全部字段；上方 role / level 两个单点端点保留兼容
    （营期端调级仍在用）。角色规则与单点端点一致：两级枚举 + 不能撤销自己的管理员。
    """
    data = request.get_json(silent=True) or {}
    target = UserModel.query.get(user_id)
    if not target:
        return jsonify({"code": 404, "message": "用户不存在"}), 404

    changes = {}

    if 'username' in data:
        username = str(data.get('username') or '').strip()
        if not (1 <= len(username) <= 50):
            return jsonify({"code": 400, "message": "用户名需为 1-50 个字符"}), 400
        if username != target.username:
            changes['username'] = (target.username, username)
            target.username = username

    if 'role' in data:
        role = data.get('role')
        if role not in ('super_admin', 'user'):
            return jsonify({"code": 400, "message": "role 仅支持 super_admin / user"}), 400
        current = _current_user()
        if current and current.id == target.id and role != 'super_admin':
            return jsonify({"code": 400, "message": "不能撤销自己的管理员权限"}), 400
        if role != target.role:
            changes['role'] = (target.role, role)
            target.role = role

    if 'admin_tag' in data:
        admin_tag = data.get('admin_tag')
        if admin_tag not in ('teacher', 'developer', None):
            return jsonify({"code": 400, "message": "admin_tag 仅支持 teacher / developer"}), 400
        if target.role != 'super_admin':
            admin_tag = None   # 非管理员不保留内部标签（与单点端点同规则）
        if admin_tag != target.admin_tag:
            changes['admin_tag'] = (target.admin_tag, admin_tag)
            target.admin_tag = admin_tag

    if 'level' in data:
        level = data.get('level')
        if level not in (1, 2, 3, 4):
            return jsonify({"code": 400, "message": "level 仅支持 1-4"}), 400
        if level != target.level:
            changes['level'] = (target.level, level)
            target.level = level

    if not changes:
        return jsonify({"code": 400, "message": "没有可更新字段（username/role/admin_tag/level）"}), 400

    db.session.commit()
    return jsonify({"code": 200, "message": "已保存",
                    "data": {"user_id": target.id,
                             "changes": {k: {"from": v[0], "to": v[1]} for k, v in changes.items()}}})


@bp.route("/users/<int:user_id>/status", methods=["PUT"])
@jwt_required()
@check_permission('system_management')
@audit_log(operation="封禁/解封用户")
def admin_set_user_status(user_id):
    """封禁（banned）/解封（active）。

    封禁语义（2026-09-11 用户定，取代删除——user.id 被 25+ 表引用）：
    禁登录 + 存量 token 在 app.before_request 入口统一拦截（403）；
    文章/营期归属/勋章等一切内容保留，解封即完全恢复。
    保护：不能操作自己；super_admin 不可封（先降级再封）。
    """
    status = (request.get_json(silent=True) or {}).get('status')
    if status not in ('active', 'banned'):
        return jsonify({"code": 400, "message": "status 仅支持 active / banned"}), 400
    target = UserModel.query.get(user_id)
    if not target:
        return jsonify({"code": 404, "message": "用户不存在"}), 404
    current = _current_user()
    if current and current.id == target.id:
        return jsonify({"code": 400, "message": "不能封禁自己"}), 400
    if target.role == 'super_admin':
        return jsonify({"code": 400, "message": "不能封禁管理员，请先将其降级为普通用户"}), 400
    if target.status == status:
        return jsonify({"code": 200, "message": "状态未变化", "data": {"user_id": target.id, "status": status}})

    old = target.status
    target.status = status
    db.session.commit()
    msg = f"已封禁 {target.username}（内容与归属保留，可随时解封）" if status == 'banned' else f"已解封 {target.username}"
    return jsonify({"code": 200, "message": msg,
                    "data": {"user_id": target.id, "username": target.username,
                             "old_status": old, "status": status}})


@bp.route("/overview", methods=["GET"])
@jwt_required()
@check_permission('system_management')
def admin_overview():
    """后台首页聚合统计"""
    today = date.today()

    user_total = UserModel.query.count()
    user_new_today = UserModel.query.filter(
        func.date(UserModel.join_time) == today).count()
    checkin_today = db.session.query(CheckRecord.user_id).filter(
        CheckRecord.date == today).distinct().count()
    article_count = ArticleModel.query.count()
    course_count = CourseModel.query.filter(
        CourseModel.status != CourseModel.STATUS_DELETED).count()
    medal_granted = MedalUserModel.query.count()
    group_count = CourseGroup.query.count()
    camp_count = CampSession.query.count()
    pending_quota = LLMQuotaRequestModel.query.filter_by(
        status=LLMQuotaRequestModel.STATUS_PENDING).count()

    return jsonify({
        "code": 200,
        "data": {
            "user_total": user_total,
            "user_new_today": user_new_today,
            "checkin_today": checkin_today,
            "article_count": article_count,
            "course_count": course_count,
            "medal_granted": medal_granted,
            "group_count": group_count,
            "camp_count": camp_count,
            "pending_quota": pending_quota,
        }
    })


@bp.route("/workbench/summary")
@jwt_required()
@check_permission('system_management')
def workbench_summary():
    """管理员工作台摘要（2026-09-21 IA 重构，方案 §12.2A）。

    回答「今天要处理什么 / 哪些营在跑 / 哪里有风险」：
    - pending：跨域待办计数（只计数，名单明细由各业务端点分页返回）；
    - running_camps：进行中营期摘要 + 各自待办数；
    - risks：规则型风险（D-10：只做可明确判断的规则，不做健康分）。
    各子域独立容错——单域查询失败回 0/空，不拖垮整页（R-03）。
    """
    from datetime import datetime, timedelta

    def safe(fn, default):
        try:
            return fn()
        except Exception:
            return default

    def _delivery_pending_count(before=None):
        # 待审交付 = team 模式全量 + member 模式负责人份额（与 delivery_admin 审队口径一致）
        def base(q):
            return q.filter(CampSubmissionVersion.created_at < before) if before else q

        team_q = CampSubmissionVersion.query.filter(
            CampSubmissionVersion.status == 'submitted',
            CampSubmissionVersion.milestone_id.in_(
                db.session.query(CampMilestone.id)
                .join(CampUnit, CampMilestone.unit_id == CampUnit.id)
                .filter(CampUnit.unit_type == 'project',
                        CampMilestone.submit_mode == 'team')))
        member_q = (CampSubmissionVersion.query
                    .join(CampMilestone, CampSubmissionVersion.milestone_id == CampMilestone.id)
                    .join(CampUnit, CampMilestone.unit_id == CampUnit.id)
                    .filter(CampUnit.unit_type == 'project',
                            CampMilestone.submit_mode == 'member',
                            CampSubmissionVersion.status == 'submitted',
                            CampSubmissionVersion.submitted_by == CampUnit.owner_user_id))
        return base(team_q).count() + base(member_q).count()

    def _feedback_ticket_pending():
        # 工单模型随 6A 落地（migrate_50）；未建表前该域回 0，不阻塞工作台其余摘要
        from models import FeedbackTicket
        return FeedbackTicket.query.filter(
            FeedbackTicket.status.in_(['new', 'triaged', 'reopened'])).count()

    pending = {
        "camp_join": safe(lambda: CampJoinRequest.query.filter_by(status='pending').count(), 0),
        "camp_leave": safe(lambda: CampLeave.query.filter_by(status='pending').count(), 0),
        "project_application": safe(
            lambda: ProjectApplicationVersion.query.filter_by(status='pending').count(), 0),
        "project_delivery": safe(lambda: _delivery_pending_count(), 0),
        "quota_request": safe(lambda: LLMQuotaRequestModel.query.filter_by(
            status=LLMQuotaRequestModel.STATUS_PENDING).count(), 0),
        "feedback_ticket": safe(_feedback_ticket_pending, 0),
    }
    oldest = {
        "camp_join": safe(lambda: db.session.query(func.min(CampJoinRequest.created_at))
                          .filter_by(status='pending').scalar(), None),
        "camp_leave": safe(lambda: db.session.query(func.min(CampLeave.created_at))
                           .filter_by(status='pending').scalar(), None),
    }

    running_camps = []
    risks = []

    def _camp_caps(camp):
        # capabilities 是 JSON 字符串列：走 _policy_dict 与类型默认值合并（勿裸读列）
        from .camp import _policy_dict
        return _policy_dict(camp.policy, camp.category).get("capabilities") or {}

    for camp in safe(lambda: CampSession.query.filter_by(status='running')
                     .order_by(CampSession.start_date.desc()).all(), []):
        sid = camp.id
        caps = _camp_caps(camp)
        join_n = safe(lambda: CampJoinRequest.query.filter_by(
            camp_session_id=sid, status='pending').count(), 0)
        leave_n = safe(lambda: CampLeave.query.filter_by(
            camp_session_id=sid, status='pending').count(), 0)
        unmatched = safe(lambda: CampMember.query.filter_by(
            camp_session_id=sid, role='student').filter(
            CampMember.team_mentor_id.is_(None)).count(), 0)
        running_camps.append({
            "id": sid, "name": camp.name, "category": camp.category,
            "cycle_name": camp.cycle.name if camp.cycle else None,
            "start_date": camp.start_date.isoformat(), "end_date": camp.end_date.isoformat(),
            "member_count": safe(lambda: CampMember.query.filter_by(
                camp_session_id=sid).count(), 0),
            "pending_join": join_n, "pending_leave": leave_n, "unmatched": unmatched,
        })
        # 规则：启用考勤但未生成考勤计划（跑起来却没铺考勤）
        if camp.category == 'learning' and caps.get('attendance', True):
            plan_n = safe(lambda: CampAttendancePlan.query.filter_by(camp_session_id=sid).count(), 0)
            if not plan_n:
                risks.append({"camp_id": sid, "camp_name": camp.name,
                              "rule": "attendance_not_configured",
                              "detail": "已启用考勤但营内没有考勤计划（承诺出勤日未生成）"})
        # 规则：培训营 running 仍有未归属学员（选导生未收尾）
        if camp.category == 'learning' and unmatched:
            risks.append({"camp_id": sid, "camp_name": camp.name,
                          "rule": "students_unmatched",
                          "detail": f"{unmatched} 名学员未归属导生"})

    # 规则：即将开营（≤14 天）但还没有成员
    horizon = date.today() + timedelta(days=14)
    upcoming = safe(lambda: CampSession.query.filter(
        CampSession.status == 'upcoming',
        CampSession.start_date <= horizon).all(), [])
    for camp in upcoming:
        member_n = safe(lambda: CampMember.query.filter_by(camp_session_id=camp.id).count(), 0)
        if not member_n:
            risks.append({"camp_id": camp.id, "camp_name": camp.name,
                          "rule": "opening_without_members",
                          "detail": f"{camp.start_date.isoformat()} 开营但尚无成员"})

    # 规则：启用座位能力但平台没有物理座位资源（全局一次性提示）
    seat_cap_camps = safe(lambda: CampSession.query.filter(
        CampSession.status.in_((['upcoming', 'selecting', 'running']))).all(), [])
    if any(_camp_caps(c).get('seat') for c in seat_cap_camps):
        seat_total = safe(lambda: SeatModel.query.count(), 0)
        if not seat_total:
            risks.append({"camp_id": None, "camp_name": None,
                          "rule": "no_physical_seats",
                          "detail": "有营期启用座位能力，但平台尚未录入任何物理座位"})

    # 规则：待审交付超 72 小时未处理（项目营）
    stale_deadline = datetime.now() - timedelta(hours=72)
    stale = safe(lambda: _delivery_pending_count(before=stale_deadline), 0)
    if stale:
        risks.append({"camp_id": None, "camp_name": None,
                      "rule": "stale_delivery_reviews",
                      "detail": f"{stale} 份待审交付材料已超过 72 小时未处理"})

    return jsonify({"code": 200, "data": {
        "pending": pending,
        "oldest_pending_at": {k: v.isoformat() if v else None for k, v in oldest.items()},
        "running_camps": running_camps,
        "risks": risks,
    }})

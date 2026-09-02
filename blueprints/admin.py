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

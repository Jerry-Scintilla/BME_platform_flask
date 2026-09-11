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

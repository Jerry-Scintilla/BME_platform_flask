"""管理端日程服务蓝图（面板批次 A，只读，2026-09-25）。

六个 GET：overview / reminders(+detail) / captures(+detail) / settings。
鉴权：/admin/ 前缀由 request_guard 门禁（banned 拦截 + super_admin 收口），
蓝图再挂 jwt + check_permission 双保险（开发计划 §6：不能只靠前端 staffOnly）。

红线：只读零副作用——本蓝图不调用任何会建 profile、触发扫描/恢复/重试、
写通知或改配置的路径；序列化白名单在 services/schedule/admin_queries.py。
错误口径：参数/枚举 400、无权限 403（guard 层 401/403）、不存在 404。
"""
from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required

from exts import db
from services.schedule import NotFound, VersionConflict
from services.schedule import admin_queries
from services.schedule.admin_queries import (
    CAPTURE_STATUSES, REMINDER_STATUSES, REASON_CODES)

from . import audit_log, check_permission

bp = Blueprint("admin_schedule", __name__, url_prefix="/admin/schedule")


def _error(status, message):
    return jsonify({"code": status, "message": message}), status


@bp.route("/overview")
@jwt_required()
@check_permission('system_management')
def overview():
    """运行概览：四指标 + 三服务状态 + 风险摘要；各子域独立降级（source_status）。"""
    return jsonify({"code": 200, "data": admin_queries.build_overview()})


@bp.route("/reminders")
@jwt_required()
@check_permission('system_management')
def reminders_list():
    """提醒记录（元数据列表）。参数：from/to（≤31 天默认 7）、status、reason、
    id、bucket（overdue/exhausted）、page/page_size（钳 100）。"""
    try:
        data = admin_queries.list_reminders(request.args.get)
    except ValueError as exc:
        return _error(400, str(exc))
    return jsonify({"code": 200, "data": data,
                    "enums": {"status": REMINDER_STATUSES, "reason": REASON_CODES}})


@bp.route("/reminders/<int:reminder_id>")
@jwt_required()
@check_permission('system_management')
def reminders_detail(reminder_id):
    data = admin_queries.get_reminder(reminder_id)
    if data is None:
        return _error(404, "提醒不存在")
    return jsonify({"code": 200, "data": data})


@bp.route("/captures")
@jwt_required()
@check_permission('system_management')
def captures_list():
    """AI 录入记录（元数据列表）。参数同 reminders，bucket 支持 stalled。"""
    try:
        data = admin_queries.list_captures(request.args.get)
    except ValueError as exc:
        return _error(400, str(exc))
    return jsonify({"code": 200, "data": data,
                    "enums": {"status": CAPTURE_STATUSES}})


@bp.route("/captures/<int:capture_id>")
@jwt_required()
@check_permission('system_management')
def captures_detail(capture_id):
    data = admin_queries.get_capture(capture_id)
    if data is None:
        return _error(404, "录入不存在")
    return jsonify({"code": 200, "data": data})


@bp.route("/settings")
@jwt_required()
@check_permission('system_management')
def settings_view():
    """白名单有效配置：只读项 + 在线配置区（B3：三个低耦合参数的期望/生效值）。"""
    return jsonify({"code": 200, "data": admin_queries.build_settings()})


@bp.route("/settings", methods=["PATCH"])
@jwt_required()
@check_permission('system_management')
@audit_log(operation="修改日程服务在线配置")
def settings_update():
    """发布在线配置新版本（B3，§9.2）：body {updates: [{key, value, expected_
    version, reason}]}。逐键乐观锁，任一冲突整批 409；value=null 表示恢复默认。
    三个白名单键在请求路径即时生效（无需重启）。"""
    from services.schedule import runtime_config
    payload = request.get_json(silent=True) or {}
    updates = payload.get("updates")
    if not isinstance(updates, list) or not updates:
        return _error(400, "缺少 updates 数组")
    from . import _current_user
    admin = _current_user()
    if admin is None or not admin.is_admin():
        return _error(403, "需要管理员权限")
    try:
        changes = runtime_config.apply_updates(updates, admin)
        db.session.commit()
    except ValueError as exc:
        db.session.rollback()
        return _error(400, str(exc))
    except VersionConflict as exc:
        db.session.rollback()
        return _error(409, str(exc))
    return jsonify({"code": 200, "data": {"changes": changes,
                                          "editable": runtime_config.editable_view()}})

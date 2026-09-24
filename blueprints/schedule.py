"""个人日程蓝图（2026-09-24 · AI 智能日程管理模块 Phase 1，migrate_54）。

面向全体用户的私人日程（规划文档 §11 的 Phase 1 子集）：任务/固定日程/执行块
CRUD + 聚合视图 + 偏好。归属一律从登录态推导，不接受客户端传 owner_id；
teacher/super_admin 身份不自动获得他人日程访问权（§12 权限口径）。

错误口径：400 参数或状态迁移不合法、404 不存在或越权（不区分两者）、
409 乐观锁版本冲突（expected_version 机制）；成功统一 {"code":200,...}。

提醒无 CRUD 端点：纯服务端物料化（对象写操作联动重算），投递由
schedule_scheduler 定时扫描（services/schedule/reminders.py）。
Phase 2 的 captures/plans/undo、Phase 3 的 reports/subscriptions 不在本期。
"""
from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required

from exts import db

from services.schedule import NotFound, VersionConflict
from services.schedule import calendar, preferences

from . import _current_user

bp = Blueprint("schedule", __name__, url_prefix="/schedule")


def _owner_id():
    """登录态推导归属；JWT 有效但用户已不存在按 401 处理。"""
    user = _current_user()
    if user is None:
        return None
    return user.id


def _error(status, message):
    return jsonify({"code": status, "message": message}), status


def _payload():
    return request.get_json(silent=True) or {}


def _expected_version(payload, field='expected_version'):
    if 'expected_version' not in payload:
        raise ValueError("缺少 expected_version（乐观锁）")
    try:
        return int(payload['expected_version'])
    except (TypeError, ValueError):
        raise ValueError("expected_version 应为整数")


def _pagination(default_per_page=20):
    try:
        page = max(1, int(request.args.get("page", 1)))
        per_page = min(50, max(1, int(request.args.get("per_page", default_per_page))))
    except (ValueError, TypeError):
        raise ValueError("分页参数错误")
    return page, per_page


# ── 聚合视图 ──────────────────────────────────────────────────────────────

@bp.route("/agenda", methods=["GET"])
@jwt_required()
def agenda():
    """区间视图（今日/周历共用）：events + blocks（含任务名）+ conflicts + day_stats。"""
    owner = _owner_id()
    if owner is None:
        return _error(401, "登录状态无效")
    from datetime import date
    date_from = request.args.get("from") or date.today().strftime('%Y-%m-%d')
    date_to = request.args.get("to") or date_from
    try:
        data = calendar.get_agenda(owner, date_from, date_to)
    except ValueError as exc:
        return _error(400, str(exc))
    return jsonify({"code": 200, "data": data})


# ── 任务 ─────────────────────────────────────────────────────────────────

@bp.route("/tasks", methods=["GET"])
@jwt_required()
def tasks_list():
    """任务列表：bucket=all/unscheduled/overdue/scheduled/today_due/done/cancelled，
    q 标题模糊，真分页（per_page 钳 50）。"""
    owner = _owner_id()
    if owner is None:
        return _error(401, "登录状态无效")
    try:
        page, per_page = _pagination()
        data = calendar.list_tasks(owner, page=page, per_page=per_page,
                                   bucket=request.args.get("bucket", "all"),
                                   q=(request.args.get("q") or "").strip())
    except ValueError as exc:
        return _error(400, str(exc))
    return jsonify({"code": 200, "data": data})


@bp.route("/tasks", methods=["POST"])
@jwt_required()
def tasks_create():
    owner = _owner_id()
    if owner is None:
        return _error(401, "登录状态无效")
    try:
        data = calendar.create_task(owner, _payload())
        db.session.commit()
    except ValueError as exc:
        db.session.rollback()
        return _error(400, str(exc))
    return jsonify({"code": 200, "data": data})


@bp.route("/tasks/<int:task_id>", methods=["GET"])
@jwt_required()
def task_detail(task_id):
    owner = _owner_id()
    if owner is None:
        return _error(401, "登录状态无效")
    try:
        data = calendar.get_task_detail(owner, task_id)
    except NotFound:
        return _error(404, "任务不存在")
    return jsonify({"code": 200, "data": data})


@bp.route("/tasks/<int:task_id>", methods=["PATCH"])
@jwt_required()
def task_update(task_id):
    owner = _owner_id()
    if owner is None:
        return _error(401, "登录状态无效")
    try:
        data = calendar.update_task(owner, task_id, _payload(), _expected_version(_payload()))
        db.session.commit()
    except ValueError as exc:
        db.session.rollback()
        return _error(400, str(exc))
    except VersionConflict as exc:
        db.session.rollback()
        return _error(409, str(exc))
    except NotFound:
        db.session.rollback()
        return _error(404, "任务不存在")
    return jsonify({"code": 200, "data": data})


@bp.route("/tasks/<int:task_id>/actions", methods=["POST"])
@jwt_required()
def task_actions(task_id):
    """动作白名单：complete/reopen/cancel/progress（状态机见 calendar.task_action）。"""
    owner = _owner_id()
    if owner is None:
        return _error(401, "登录状态无效")
    payload = _payload()
    action = payload.get("action")
    if action not in ('complete', 'reopen', 'cancel', 'progress'):
        return _error(400, "不支持的操作")
    try:
        data = calendar.task_action(owner, task_id, action,
                                    {'remaining_minutes': payload.get('remaining_minutes'),
                                     'actual_minutes': payload.get('actual_minutes'),
                                     'note': payload.get('note')})
        db.session.commit()
    except ValueError as exc:
        db.session.rollback()
        return _error(400, str(exc))
    except NotFound:
        db.session.rollback()
        return _error(404, "任务不存在")
    return jsonify({"code": 200, "data": data})


# ── 固定日程 ─────────────────────────────────────────────────────────────

@bp.route("/events", methods=["POST"])
@jwt_required()
def events_create():
    owner = _owner_id()
    if owner is None:
        return _error(401, "登录状态无效")
    try:
        data = calendar.create_event(owner, _payload())
        db.session.commit()
    except ValueError as exc:
        db.session.rollback()
        return _error(400, str(exc))
    return jsonify({"code": 200, "data": data})


@bp.route("/events/<int:event_id>", methods=["PATCH"])
@jwt_required()
def events_update(event_id):
    owner = _owner_id()
    if owner is None:
        return _error(401, "登录状态无效")
    try:
        data = calendar.update_event(owner, event_id, _payload(), _expected_version(_payload()))
        db.session.commit()
    except ValueError as exc:
        db.session.rollback()
        return _error(400, str(exc))
    except VersionConflict as exc:
        db.session.rollback()
        return _error(409, str(exc))
    except NotFound:
        db.session.rollback()
        return _error(404, "日程不存在")
    return jsonify({"code": 200, "data": data})


@bp.route("/events/<int:event_id>", methods=["DELETE"])
@jwt_required()
def events_delete(event_id):
    """软删（status='cancelled'）并作废未投递提醒。"""
    owner = _owner_id()
    if owner is None:
        return _error(401, "登录状态无效")
    try:
        data = calendar.cancel_event(owner, event_id)
        db.session.commit()
    except ValueError as exc:
        db.session.rollback()
        return _error(400, str(exc))
    except NotFound:
        db.session.rollback()
        return _error(404, "日程不存在")
    return jsonify({"code": 200, "data": data})


# ── 执行块 ───────────────────────────────────────────────────────────────

@bp.route("/blocks", methods=["POST"])
@jwt_required()
def blocks_create():
    """手动安排任务执行时间：{task_id, start_at, end_at, locked}。"""
    owner = _owner_id()
    if owner is None:
        return _error(401, "登录状态无效")
    try:
        data = calendar.create_block(owner, _payload())
        db.session.commit()
    except ValueError as exc:
        db.session.rollback()
        return _error(400, str(exc))
    except NotFound:
        db.session.rollback()
        return _error(404, "任务不存在或不可安排")
    return jsonify({"code": 200, "data": data})


@bp.route("/blocks/<int:block_id>", methods=["PATCH"])
@jwt_required()
def blocks_update(block_id):
    """手动移动/锁定时间块（Phase 1 无拖拽，前端弹窗提交新起止）。"""
    owner = _owner_id()
    if owner is None:
        return _error(401, "登录状态无效")
    try:
        data = calendar.update_block(owner, block_id, _payload(), _expected_version(_payload()))
        db.session.commit()
    except ValueError as exc:
        db.session.rollback()
        return _error(400, str(exc))
    except VersionConflict as exc:
        db.session.rollback()
        return _error(409, str(exc))
    except NotFound:
        db.session.rollback()
        return _error(404, "时间块不存在")
    return jsonify({"code": 200, "data": data})


@bp.route("/blocks/<int:block_id>", methods=["DELETE"])
@jwt_required()
def blocks_delete(block_id):
    owner = _owner_id()
    if owner is None:
        return _error(401, "登录状态无效")
    try:
        data = calendar.cancel_block(owner, block_id)
        db.session.commit()
    except NotFound:
        db.session.rollback()
        return _error(404, "时间块不存在")
    return jsonify({"code": 200, "data": data})


# ── 偏好 ─────────────────────────────────────────────────────────────────

@bp.route("/preferences", methods=["GET"])
@jwt_required()
def preferences_get():
    """首次访问自动建行（默认值见模型）；Phase 1 不暴露 timezone 修改。"""
    owner = _owner_id()
    if owner is None:
        return _error(401, "登录状态无效")
    profile = preferences.get_or_create_profile(owner)
    db.session.commit()          # 首次建行也要落库（幂等：唯一键兜底）
    return jsonify({"code": 200, "data": profile.to_dict()})


@bp.route("/preferences", methods=["PATCH"])
@jwt_required()
def preferences_update():
    owner = _owner_id()
    if owner is None:
        return _error(401, "登录状态无效")
    try:
        profile = preferences.get_or_create_profile(owner)
        preferences.apply_profile_update(profile, _payload(), _expected_version(_payload()))
        db.session.commit()
    except ValueError as exc:
        db.session.rollback()
        return _error(400, str(exc))
    except VersionConflict as exc:
        db.session.rollback()
        return _error(409, str(exc))
    return jsonify({"code": 200, "data": profile.to_dict()})

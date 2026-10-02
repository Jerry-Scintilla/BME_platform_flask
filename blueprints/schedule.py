"""个人日程蓝图（2026-09-24 · Phase 1 手动闭环；同日 Phase 2 增意图与排程）。

面向全体用户的私人日程：任务/固定日程/执行块 CRUD + 聚合视图 + 偏好 +
意图录入（captures）与排程方案（plans）。归属一律从登录态推导，不接受客户端
传 owner_id；teacher/super_admin 身份不自动获得他人日程访问权（§12 权限口径）。

错误口径：400 参数或状态迁移不合法、404 不存在或越权（不区分两者）、
409 乐观锁版本冲突、429 意图日限、503 意图开关关闭。

锁序规范（防死锁，见 services/schedule/__init__.py）：POST /tasks 与
POST/PATCH /events 会触发规划器，必须先 lock_profile 再写对象行。

提醒无 CRUD 端点：纯服务端物料化（对象写操作联动重算），投递由
schedule_scheduler 定时扫描（services/schedule/reminders.py）。
Phase 3 的 reports/subscriptions 不在本期。
"""
from flask import Blueprint, current_app, jsonify, request
from flask_jwt_extended import jwt_required

from exts import db
from models import ScheduleCapture, SchedulePlan

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
    """创建任务；suggest 模式下同事务自动安排执行块（响应带 plan 摘要）。
    锁序：先 lock_profile 再 create_task（规划器入口锁）。"""
    owner = _owner_id()
    if owner is None:
        return _error(401, "登录状态无效")
    from services.schedule import planner as planner_svc
    from services.schedule.preferences import lock_profile
    try:
        profile = lock_profile(owner)          # 锁序规范：规划器相关写前先持
        data = calendar.create_task(owner, _payload())
        plan_summary = None
        if (planner_svc.planner_enabled()
                and data['task']['deadline_precision'] != 'none'):
            task_row = db.session.query(calendar.ScheduleTask).get(data['task']['id'])
            scheduling = planner_svc.auto_schedule(owner, [task_row], profile=profile)
            if scheduling.get('plan') is not None:
                plan_summary = {
                    'id': scheduling['plan'].id, 'mode': scheduling['mode'],
                    'reason': scheduling['reason'],
                    'blocks': [{'start_at': op['start_at'].strftime('%Y-%m-%d %H:%M'),
                                'end_at': op['end_at'].strftime('%Y-%m-%d %H:%M')}
                               for op in scheduling['ops'] if op['op'] == 'create_block'],
                    'unscheduled': scheduling['unscheduled']}
            else:
                plan_summary = {'id': None, 'mode': 'none',
                                'reason': scheduling['reason'], 'blocks': [],
                                'unscheduled': scheduling['unscheduled']}
        data['plan'] = plan_summary
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
    """创建固定日程；与既有 AI 未锁定块冲突时触发有限重排（响应带 replan 摘要）。
    锁序：先 lock_profile 再 create_event。"""
    owner = _owner_id()
    if owner is None:
        return _error(401, "登录状态无效")
    data, replan = _create_event_with_replan(owner, _payload())
    if isinstance(data, tuple):
        return data
    return jsonify({"code": 200, "data": {**data, 'replan': replan}})


def _create_event_with_replan(owner, payload):
    from services.schedule import planner as planner_svc
    from services.schedule.preferences import lock_profile
    try:
        profile = lock_profile(owner)
        data = calendar.create_event(owner, payload)
        replan = None
        if planner_svc.planner_enabled():
            event_row = db.session.query(calendar.ScheduleEvent).get(data['event']['id'])
            outcome = planner_svc.replan_conflicts(owner, event_row, profile=profile)
            if outcome:
                replan = {
                    'plan_id': outcome['plan'].id, 'mode': outcome['mode'],
                    'moved': sum(1 for op in outcome['ops'] if op['op'] == 'move_block'),
                    'cancelled': sum(1 for op in outcome['ops'] if op['op'] == 'cancel_block'),
                    'reason': outcome['plan'].reason}
        db.session.commit()
        return data, replan
    except ValueError as exc:
        db.session.rollback()
        return _error(400, str(exc)), None


@bp.route("/events/<int:event_id>", methods=["PATCH"])
@jwt_required()
def events_update(event_id):
    owner = _owner_id()
    if owner is None:
        return _error(401, "登录状态无效")
    from services.schedule import planner as planner_svc
    from services.schedule.preferences import lock_profile
    try:
        profile = lock_profile(owner)
        data = calendar.update_event(owner, event_id, _payload(), _expected_version(_payload()))
        replan = None
        if planner_svc.planner_enabled():
            event_row = db.session.query(calendar.ScheduleEvent).get(data['event']['id'])
            outcome = planner_svc.replan_conflicts(owner, event_row, profile=profile)
            if outcome:
                replan = {
                    'plan_id': outcome['plan'].id, 'mode': outcome['mode'],
                    'moved': sum(1 for op in outcome['ops'] if op['op'] == 'move_block'),
                    'cancelled': sum(1 for op in outcome['ops'] if op['op'] == 'cancel_block'),
                    'reason': outcome['plan'].reason}
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
    return jsonify({"code": 200, "data": {**data, 'replan': replan}})


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


# ── 意图录入（Phase 2：说一句，帮我安排） ─────────────────────────────────

@bp.route("/captures", methods=["POST"])
@jwt_required()
def captures_create():
    """文字录入 → 建行（幂等）→ 线程池异步处理 → 前端短轮询 GET。
    request_id 由客户端生成（uuid），网络重试复用同一键不重复建行。"""
    owner = _owner_id()
    if owner is None:
        return _error(401, "登录状态无效")
    # S08 整改：门禁只走 runtime_config 单一真相（环境熔断与平台覆盖的优先级
    # 在 effective() 内统一），显示与请求门禁不再各查各的。
    from services.schedule.runtime_config import intent_enabled
    if not intent_enabled():
        return _error(503, "智能录入暂未开放")
    payload = _payload()
    text = (payload.get("text") or "").strip()
    if not text or len(text) > 2000:
        return _error(400, "请输入 1-2000 字的描述")
    request_id = str(payload.get("request_id") or "")[:64]

    from services.schedule import capture as capture_svc

    # ① 幂等查重最优先（S05 整改）：同 request_id 的网络重试必须能拿回既有
    # 请求的结果——不能先撞额度墙回 429（额度用尽后重试即被拒，拿不到 202）。
    if request_id:
        existing = ScheduleCapture.query.filter_by(
            user_id=owner, request_id=request_id).first()
        if existing is not None:
            return jsonify({"code": 200,
                            "data": {"capture": existing.to_dict(), "submitted": False}})

    # ② 用户级日限（单位=提交次数）：GET 预检快速失败；新建路径的原子扣额
    # 在 ③（incr 越限即回退），Redis 故障降级放行（article_v2 同口径）
    from exts import redis_client
    from datetime import date as _date
    from services.schedule.runtime_config import intent_daily_limit
    redis_key = f"schedule_intent:{owner}:{_date.today().strftime('%Y%m%d')}"
    try:
        used = redis_client.get(redis_key)
        if used is not None and int(used) >= intent_daily_limit():
            return _error(429, "今日智能录入次数已用完，可手动创建任务")
    except Exception:
        pass

    # ③ 建行 + 原子扣额：incr 越限回滚回退计数（并发不同键在边界也只放行
    # N 个；同键并发撞唯一约束走幂等返回，仅胜者计数）
    row, created = capture_svc.create_capture(owner, text, request_id)
    if row is None:
        return _error(400, "录入创建失败")
    if created:
        try:
            used = redis_client.incr(redis_key)
            if used == 1:
                redis_client.expire(redis_key, 86400)
            if used > intent_daily_limit():
                try:
                    redis_client.decr(redis_key)
                except Exception:
                    pass
                db.session.rollback()
                return _error(429, "今日智能录入次数已用完，可手动创建任务")
        except Exception:
            pass
        db.session.commit()
        capture_svc.submit_capture(current_app, row.id)
        return jsonify({"code": 200, "data": {"capture": row.to_dict(), "submitted": True}}), 202
    db.session.rollback()
    return jsonify({"code": 200, "data": {"capture": row.to_dict(), "submitted": False}})


@bp.route("/captures/<int:capture_id>", methods=["GET"])
@jwt_required()
def captures_detail(capture_id):
    """轮询端点：验权后顺带惰性恢复本人卡死行（后台兜底见调度器 sweep_stale）。
    S01 整改：必须先按 owner 查到行才允许触发恢复，他人访问不产生任何写入。"""
    owner = _owner_id()
    if owner is None:
        return _error(401, "登录状态无效")
    row = ScheduleCapture.query.filter_by(id=capture_id, user_id=owner).first()
    if row is None:
        return _error(404, "录入不存在")
    from services.schedule import capture as capture_svc
    capture_svc.lazy_recover(capture_id, owner)
    return jsonify({"code": 200, "data": row.to_dict(with_items=True)})


@bp.route("/captures/<int:capture_id>/resolve", methods=["POST"])
@jwt_required()
def captures_resolve(capture_id):
    """歧义补答：同步执行（不再调 LLM），一次请求返回终态。
    body: {answers: {"<item index>": {"<field>": "<完整值或 __unset__>"}}}"""
    owner = _owner_id()
    if owner is None:
        return _error(401, "登录状态无效")
    answers = _payload().get("answers")
    if not isinstance(answers, dict) or not answers:
        return _error(400, "请先选择或填写补充信息")
    from services.schedule import capture as capture_svc
    try:
        row = capture_svc.resolve_capture(owner, capture_id, answers)
        db.session.commit()
    except ValueError as exc:
        db.session.rollback()
        return _error(400, str(exc))
    except VersionConflict as exc:
        db.session.rollback()
        return _error(409, str(exc))
    except NotFound:
        db.session.rollback()
        return _error(404, "录入不存在")
    return jsonify({"code": 200, "data": row.to_dict(with_items=True)})


# ── 排程方案（Phase 2：应用/撤销） ────────────────────────────────────────

@bp.route("/plans", methods=["GET"])
@jwt_required()
def plans_recent():
    """最近方案列表（撤销入口兜底；limit 钳 20）。"""
    owner = _owner_id()
    if owner is None:
        return _error(401, "登录状态无效")
    try:
        limit = min(20, max(1, int(request.args.get("limit", 10))))
    except (ValueError, TypeError):
        return _error(400, "limit 参数错误")
    rows = (SchedulePlan.query.filter(SchedulePlan.user_id == owner)
            .order_by(SchedulePlan.id.desc()).limit(limit).all())
    return jsonify({"code": 200, "data": [p.to_dict() for p in rows]})


@bp.route("/plans/<int:plan_id>/apply", methods=["POST"])
@jwt_required()
def plans_apply(plan_id):
    """manual 模式的方案确认：逐实体复验版本基线，任一不符 409 重新生成。
    锁序：先 profile 再 plan。"""
    owner = _owner_id()
    if owner is None:
        return _error(401, "登录状态无效")
    from services.schedule import planner as planner_svc
    from services.schedule.preferences import lock_profile
    try:
        lock_profile(owner)
        result = planner_svc.apply_proposed(owner, plan_id)
        db.session.commit()
    except VersionConflict as exc:
        db.session.rollback()
        return _error(409, str(exc))
    except NotFound:
        db.session.rollback()
        return _error(404, "方案不存在")
    return jsonify({"code": 200,
                    "data": {"plan": result["plan"].to_dict(), "changes_n": result["changes_n"]}})


@bp.route("/plans/<int:plan_id>/revert", methods=["POST"])
@jwt_required()
def plans_revert(plan_id):
    """撤销方案：带版本检查的部分补偿（skipped 项明示原因）。
    锁序：先 profile 再 plan。"""
    owner = _owner_id()
    if owner is None:
        return _error(401, "登录状态无效")
    from services.schedule import planner as planner_svc
    from services.schedule.preferences import lock_profile
    try:
        lock_profile(owner)
        result = planner_svc.revert_plan(owner, plan_id)
        db.session.commit()
    except VersionConflict as exc:
        db.session.rollback()
        return _error(409, str(exc))
    except NotFound:
        db.session.rollback()
        return _error(404, "方案不存在")
    return jsonify({"code": 200, "data": result})

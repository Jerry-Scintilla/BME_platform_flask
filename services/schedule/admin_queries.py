"""管理端日程查询服务（面板批次 A，只读零副作用）。

红线（开发计划 §6/§10）：
- 独立序列化器显式白名单拷贝——绝不返回 text/title_snapshot/items_json/
  diff_json/last_error 原文/error 文案/密钥；NULL 一律原样（前端显示未采集/未知）；
- 禁止调用任何带副作用的便捷方法：_verify/compute_trigger（会 get_or_create_
  profile 建行）、capture.lazy_recover（会改状态）、create_notification 等；
- 概览/列表/工作台风险共用本模块口径；各统计子域独立降级（失败 unavailable，
  空结果才是 0）；响应带 as_of + timezone。

时间口径：与日程域一致 naive 本地时间（Asia/Shanghai）+ datetime.now()；
「今日」边界在 Python 侧计算（day_window 随响应返回），不用 func.date() 杀索引。
"""
import json
from datetime import datetime, timedelta

from flask import current_app

from exts import db
from models import (ScheduleCapture, SchedulePlan, ScheduleReminder,
                    ScheduleServiceRuntime)

# 源常量 import 复用（不复制魔数；管理端只读不修改它们）
from .capture import STALE_AFTER
from .reminders import MAX_ATTEMPTS, SCAN_BATCH_LIMIT

TIMEZONE_KEY = 'ATTENDANCE_REPORT_TIMEZONE'

REMINDER_STATUSES = ('pending', 'delivered', 'cancelled', 'expired', 'failed')
CAPTURE_STATUSES = ('pending', 'processing', 'done', 'failed', 'clarify_needed')
REASON_CODES = ('target_gone', 'stale_version', 'target_inactive', 'no_deadline', 'missed')
MAX_RANGE_DAYS = 31
DEFAULT_RANGE_DAYS = 7
PAGE_SIZE_CAP = 100


def _fmt(dt):
    return dt.strftime('%Y-%m-%d %H:%M:%S') if dt else None


def read(app_logger_key, fn):
    """子域独立降级（admin.py workbench read() 同范式）。"""
    try:
        return fn()
    except Exception:
        current_app.logger.exception("schedule admin source unavailable: %s", app_logger_key)
        db.session.rollback()
        return None


# ── 序列化器（显式白名单） ────────────────────────────────────────────────

def serialize_reminder(row, *, detail=False):
    data = {
        'id': row.id, 'user_id': row.user_id,
        'target_type': row.target_type, 'target_id': row.target_id,
        'target_version': row.target_version, 'kind': row.kind,
        'trigger_at': _fmt(row.trigger_at), 'status': row.status,
        'status_reason_code': row.status_reason_code,
        'delivered_at': _fmt(row.delivered_at),
        'attempts': row.attempts,                    # 界面语义=失败尝试次数
        'next_retry_at': _fmt(row.next_retry_at),
        'last_error_code': row.last_error_code,
    }
    if detail:
        data.update({
            'notification_id': row.notification_id,
            'created_at': _fmt(row.created_at), 'updated_at': _fmt(row.updated_at),
        })
        from models import ScheduleBlock, ScheduleEvent, ScheduleTask
        model = {'event': ScheduleEvent, 'task': ScheduleTask,
                 'block': ScheduleBlock}.get(row.target_type)
        alive, current_version = None, None
        if model:
            obj = db.session.get(model, row.target_id)     # 裸读，零副作用
            alive = obj is not None
            current_version = obj.version if obj is not None else None
        data['target_alive'] = alive
        data['target_current_version'] = current_version
    return data


def serialize_capture(row, *, detail=False):
    data = {
        'id': row.id, 'user_id': row.user_id, 'input_type': row.input_type,
        'status': row.status, 'error_code': row.error_code,
        'created_at': _fmt(row.created_at), 'started_at': _fmt(row.started_at),
        'finished_at': _fmt(row.finished_at), 'elapsed_ms': row.elapsed_ms,
        'created_count': row.created_count, 'clarify_count': row.clarify_count,
        'failed_count': row.failed_count,
    }
    if detail:
        items_total = None
        plan_ids = None
        if row.items_json:
            try:
                parsed = json.loads(row.items_json)
                items = parsed.get('items') or []
                items_total = len(items)
                plan_ids = parsed.get('plan_ids') or None
            except (ValueError, TypeError):
                items_total = None
        data.update({'request_id': row.request_id, 'updated_at': _fmt(row.updated_at),
                     'items_total': items_total, 'plan_ids': plan_ids})
    return data


# ── 概览 ──────────────────────────────────────────────────────────────────

def _day_window(now):
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return day_start, day_start + timedelta(days=1)


def metric_capture_users(day_start, day_end):
    return db.session.query(db.func.count(db.func.distinct(ScheduleCapture.user_id))) \
        .filter(ScheduleCapture.created_at >= day_start,
                ScheduleCapture.created_at < day_end).scalar()


def metric_capture_status_today(day_start, day_end):
    rows = (db.session.query(ScheduleCapture.status, db.func.count(ScheduleCapture.id))
            .filter(ScheduleCapture.created_at >= day_start,
                    ScheduleCapture.created_at < day_end)
            .group_by(ScheduleCapture.status).all())
    return {status: count for status, count in rows}


def metric_reminder_backlog(now):
    """到期未生成通知（§4）：trigger_at≤now 且 status in (pending, failed)，
    拆 pending / 可自动重试 / 重试耗尽三段。未来 pending 不算积压。"""
    base = [ScheduleReminder.trigger_at <= now,
            ScheduleReminder.status.in_(('pending', 'failed'))]
    pending = db.session.query(db.func.count(ScheduleReminder.id)).filter(
        *base, ScheduleReminder.status == 'pending').scalar()
    retryable = db.session.query(db.func.count(ScheduleReminder.id)).filter(
        *base, ScheduleReminder.status == 'failed',
        ScheduleReminder.attempts < MAX_ATTEMPTS,
        ScheduleReminder.next_retry_at <= now).scalar()
    exhausted = db.session.query(db.func.count(ScheduleReminder.id)).filter(
        *base, ScheduleReminder.status == 'failed',
        ScheduleReminder.attempts >= MAX_ATTEMPTS).scalar()
    return {'pending': pending, 'retryable': retryable, 'exhausted': exhausted,
            'total': pending + retryable + exhausted}


def metric_manual_review(now):
    """需人工排查（§4）= 重试耗尽提醒 + 停滞录入（只读复用 STALE_AFTER，
    绝不触发 lazy_recover）。"""
    exhausted = db.session.query(db.func.count(ScheduleReminder.id)).filter(
        ScheduleReminder.status == 'failed',
        ScheduleReminder.attempts >= MAX_ATTEMPTS).scalar()
    threshold = now - STALE_AFTER
    stalled = db.session.query(db.func.count(ScheduleCapture.id)).filter(
        ScheduleCapture.status.in_(('pending', 'processing')),
        ScheduleCapture.updated_at < threshold).scalar()
    return {'exhausted_reminders': exhausted, 'stalled_captures': stalled,
            'total': exhausted + stalled}


def scan_service_status(now):
    """提醒扫描服务状态（§5.1 五态；优先级 关闭>未观测>异常>配置不一致>正常）。"""
    enabled = bool(current_app.config.get('SCHEDULE_REMINDER_SCAN_ENABLED', True))
    interval = max(5, int(current_app.config.get('SCHEDULE_REMINDER_SCAN_INTERVAL_SECONDS', 30)))
    base = {'key': 'reminder_scan', 'enabled': enabled, 'expected_interval': interval}
    if not enabled:
        return {**base, 'state': 'disabled'}
    row = (ScheduleServiceRuntime.query
           .filter(ScheduleServiceRuntime.service_key == 'reminder_scan')
           .order_by(ScheduleServiceRuntime.updated_at.desc(), ScheduleServiceRuntime.id.desc())
           .first())
    if row is None:
        return {**base, 'state': 'unobserved'}
    window = timedelta(seconds=max(3 * row.interval_seconds, 120))
    result = {**base, 'observed_instance': row.instance_id[:8],
              'observed_interval': row.interval_seconds,
              'last_started_at': _fmt(row.last_started_at),
              'last_finished_at': _fmt(row.last_finished_at),
              'last_success_at': _fmt(row.last_success_at),
              'last_outcome': row.last_outcome, 'safe_error_code': row.safe_error_code}
    try:
        result['last_batch_counts'] = json.loads(row.last_batch_counts or '{}')
    except (ValueError, TypeError):
        result['last_batch_counts'] = None
    if row.last_outcome == 'failed' and row.updated_at >= now - window:
        result.update(state='abnormal', cause='recent_failure')
    elif (row.last_outcome == 'running' and row.last_started_at
          and row.last_started_at < now - window):
        result.update(state='abnormal', cause='run_timeout')
    elif row.last_success_at is None or now - row.last_success_at > window:
        result.update(state='abnormal', cause='heartbeat_stale')
    elif (bool(row.enabled_snapshot) != enabled or row.interval_seconds != interval):
        result.update(state='config_mismatch',
                      detail=f"扫描实例自报 enabled={int(bool(row.enabled_snapshot))}"
                             f"/interval={row.interval_seconds}，与当前进程 "
                             f"enabled={int(enabled)}/interval={interval} 不一致")
    else:
        result.update(state='ok')
    return result


def intent_service_status():
    """AI 理解服务卡：请求驱动无心跳——观测=最近完成的 capture。
    enabled 读在线配置（B3：DB 覆盖 > env/默认）。"""
    from .runtime_config import intent_enabled
    enabled = bool(intent_enabled())
    last = db.session.query(db.func.max(ScheduleCapture.finished_at)).scalar()
    return {'key': 'intent', 'enabled': enabled, 'state': 'ok' if enabled else 'disabled',
            'last_finished_at': _fmt(last), 'observed': 'capture 表最近完成时刻'}


def planner_service_status():
    """自动排程服务卡：观测=最近 applied 方案（created_at 每次 capture 都建行，
    证不了排程引擎，必须用 applied_at）。enabled 读在线配置（B3）。"""
    from .runtime_config import planner_enabled
    enabled = bool(planner_enabled())
    last = db.session.query(db.func.max(SchedulePlan.applied_at)) \
        .filter(SchedulePlan.status == 'applied').scalar()
    return {'key': 'planner', 'enabled': enabled, 'state': 'ok' if enabled else 'disabled',
            'last_applied_at': _fmt(last), 'observed': '最近应用方案时刻'}


def build_overview():
    now = datetime.now()
    day_start, day_end = _day_window(now)
    source_status = {}

    def guard(key, fn):
        value = read(key, fn)
        if value is None:
            source_status[key] = 'unavailable'
        return value

    capture_users = guard('capture_users', lambda: metric_capture_users(day_start, day_end))
    capture_status = guard('capture_status', lambda: metric_capture_status_today(day_start, day_end))
    backlog = guard('reminder_backlog', lambda: metric_reminder_backlog(now))
    manual = guard('manual_review', lambda: metric_manual_review(now))
    services = guard('services', lambda: {
        'reminder_scan': scan_service_status(now),
        'intent': intent_service_status(),
        'planner': planner_service_status(),
    })
    risks = list_workbench_risks()          # 内部自带降级（失败返回 []）

    return {
        'as_of': _fmt(now),
        'timezone': current_app.config.get(TIMEZONE_KEY, 'Asia/Shanghai'),
        'day_window': {'from': _fmt(day_start), 'to': _fmt(day_end)},
        'metrics': {
            'capture_users_today': capture_users,
            'capture_status_today': capture_status,
            'reminder_backlog': backlog,
            'manual_review': manual,
        },
        'services': services,
        'risks': risks,
        'source_status': source_status,
    }


# ── 列表与详情 ────────────────────────────────────────────────────────────

def _parse_range(args_get):
    """from/to（YYYY-MM-DD，默认近 7 天，跨度 ≤31；camp.py from/to 同款口径）。"""
    from datetime import date
    try:
        frm = date.fromisoformat(args_get('from')) if args_get('from') else None
        to = date.fromisoformat(args_get('to')) if args_get('to') else None
    except ValueError:
        raise ValueError("日期格式错误，需 YYYY-MM-DD")
    today = date.today()
    frm = frm or today - timedelta(days=DEFAULT_RANGE_DAYS - 1)
    to = to or today
    if frm > to:
        raise ValueError("from 不能晚于 to")
    if (to - frm).days + 1 > MAX_RANGE_DAYS:
        raise ValueError(f"查询范围不能超过 {MAX_RANGE_DAYS} 天")
    return datetime.combine(frm, datetime.min.time()), datetime.combine(to + timedelta(days=1), datetime.min.time())


def _parse_page(args_get):
    page = max(1, args_get('page', 1, type=int) or 1)
    page_size = min(PAGE_SIZE_CAP, max(1, args_get('page_size', 20, type=int) or 20))
    return page, page_size


def list_reminders(args_get):
    """提醒记录（元数据）：trigger_at 时间范围（含 to 当日）+ status/reason/
    id 精查 + bucket 虚拟筛选（overdue=到期未投、exhausted=重试耗尽）。"""
    lo, hi = _parse_range(args_get)
    page, page_size = _parse_page(args_get)
    query = ScheduleReminder.query.filter(ScheduleReminder.trigger_at >= lo,
                                          ScheduleReminder.trigger_at < hi)
    status = args_get('status')
    if status:
        if status not in REMINDER_STATUSES:
            raise ValueError("status 取值非法")
        query = query.filter(ScheduleReminder.status == status)
    reason = args_get('reason')
    if reason:
        if reason not in REASON_CODES:
            raise ValueError("reason 取值非法")
        query = query.filter(ScheduleReminder.status_reason_code == reason)
    rid = args_get('id', type=int)
    if rid:
        query = query.filter(ScheduleReminder.id == rid)
    bucket = args_get('bucket')
    now = datetime.now()
    if bucket == 'overdue':
        query = query.filter(ScheduleReminder.trigger_at <= now,
                             ScheduleReminder.status.in_(('pending', 'failed')))
    elif bucket == 'exhausted':
        query = query.filter(ScheduleReminder.status == 'failed',
                             ScheduleReminder.attempts >= MAX_ATTEMPTS)
    elif bucket:
        raise ValueError("bucket 取值非法")
    total = query.count()
    rows = (query.order_by(ScheduleReminder.id.desc())
            .offset((page - 1) * page_size).limit(page_size).all())
    return {'items': [serialize_reminder(r) for r in rows], 'total': total,
            'page': page, 'page_size': page_size}


def get_reminder(reminder_id):
    row = db.session.get(ScheduleReminder, reminder_id)
    if row is None:
        return None
    return serialize_reminder(row, detail=True)


def list_captures(args_get):
    """AI 录入记录（元数据）：created_at 范围 + status/id + bucket=stalled。"""
    lo, hi = _parse_range(args_get)
    page, page_size = _parse_page(args_get)
    query = ScheduleCapture.query.filter(ScheduleCapture.created_at >= lo,
                                         ScheduleCapture.created_at < hi)
    status = args_get('status')
    if status:
        if status not in CAPTURE_STATUSES:
            raise ValueError("status 取值非法")
        query = query.filter(ScheduleCapture.status == status)
    cid = args_get('id', type=int)
    if cid:
        query = query.filter(ScheduleCapture.id == cid)
    bucket = args_get('bucket')
    now = datetime.now()
    if bucket == 'stalled':
        query = query.filter(ScheduleCapture.status.in_(('pending', 'processing')),
                             ScheduleCapture.updated_at < now - STALE_AFTER)
    elif bucket:
        raise ValueError("bucket 取值非法")
    total = query.count()
    rows = (query.order_by(ScheduleCapture.id.desc())
            .offset((page - 1) * page_size).limit(page_size).all())
    return {'items': [serialize_capture(r) for r in rows], 'total': total,
            'page': page, 'page_size': page_size}


def get_capture(capture_id):
    row = db.session.get(ScheduleCapture, capture_id)
    if row is None:
        return None
    return serialize_capture(row, detail=True)


# ── 工作台风险（三条，域内自带降级） ──────────────────────────────────────

def list_workbench_risks():
    """三条可定位风险（§7）：同 rule 聚合一行附 count；查询失败返回 []（由
    overview/summary 侧标记该域 unavailable，不拖垮其他域）。"""
    risks = []
    now = datetime.now()

    scan = read('risk_scan', lambda: scan_service_status(now))
    if scan is not None and scan.get('state') == 'abnormal':
        cause_text = {'recent_failure': '最近一轮扫描失败', 'run_timeout': '扫描运行超时',
                      'heartbeat_stale': '扫描心跳过期'}.get(scan.get('cause'), '扫描异常')
        risks.append({'camp_id': None, 'camp_name': None, 'rule': 'schedule_scan_stale',
                      'detail': f"日程提醒扫描异常：{cause_text}", 'count': 1,
                      'target': {'tab': 'records', 'type': 'reminder', 'bucket': 'overdue'}})
    exhausted = read('risk_exhausted', lambda: db.session.query(
        db.func.count(ScheduleReminder.id)).filter(
        ScheduleReminder.status == 'failed',
        ScheduleReminder.attempts >= MAX_ATTEMPTS).scalar())
    if exhausted:
        risks.append({'camp_id': None, 'camp_name': None, 'rule': 'schedule_reminder_exhausted',
                      'detail': f"{exhausted} 条提醒重试耗尽，需人工排查", 'count': exhausted,
                      'target': {'tab': 'records', 'type': 'reminder', 'bucket': 'exhausted'}})
    stalled = read('risk_stalled', lambda: db.session.query(
        db.func.count(ScheduleCapture.id)).filter(
        ScheduleCapture.status.in_(('pending', 'processing')),
        ScheduleCapture.updated_at < now - STALE_AFTER).scalar())
    if stalled:
        risks.append({'camp_id': None, 'camp_name': None, 'rule': 'schedule_capture_stalled',
                      'detail': f"{stalled} 条 AI 录入处理停滞超过 {int(STALE_AFTER.total_seconds() / 60)} 分钟",
                      'count': stalled,
                      'target': {'tab': 'records', 'type': 'capture', 'bucket': 'stalled'}})
    return risks


# ── 有效配置（白名单只读解析） ────────────────────────────────────────────

def build_settings():
    """白名单键的有效值/来源/组件/是否需重启。密钥只出「已配置」布尔。
    作用域注意（§2 现状 5）：这是本 API 进程的有效值，各后台进程启动时各自
    读环境变量，不宣称全局生效。"""
    import os
    cfg = current_app.config

    intent_model = cfg.get('SCHEDULE_INTENT_MODEL')
    if intent_model:
        model_source = 'env'
    elif cfg.get('AI_TOPIC_MODEL'):
        intent_model = cfg.get('AI_TOPIC_MODEL')
        model_source = 'env(AI_TOPIC_MODEL 回退)'
    else:
        intent_model = 'deepseek-chat'
        model_source = 'default'

    groups = [
        {'section': '提醒扫描', 'items': [
            {'key': 'SCHEDULE_REMINDER_SCAN_ENABLED', 'value': bool(cfg.get('SCHEDULE_REMINDER_SCAN_ENABLED', True)),
             'source': 'env' if os.getenv('SCHEDULE_REMINDER_SCAN_ENABLED') else 'default',
             'component': 'schedule_scheduler', 'restart_needed': True},
            {'key': 'SCHEDULE_REMINDER_SCAN_INTERVAL_SECONDS',
             'value': max(5, int(cfg.get('SCHEDULE_REMINDER_SCAN_INTERVAL_SECONDS', 30))),
             'source': 'env' if os.getenv('SCHEDULE_REMINDER_SCAN_INTERVAL_SECONDS') else 'default',
             'component': 'schedule_scheduler', 'restart_needed': True},
            {'key': 'SCAN_BATCH_LIMIT（单轮扫描上限）', 'value': SCAN_BATCH_LIMIT,
             'source': 'code', 'component': 'reminders.scan_due_reminders', 'restart_needed': True},
        ]},
        {'section': 'AI 意图理解', 'items': [
            {'key': 'SCHEDULE_INTENT_ENABLED', 'value': bool(cfg.get('SCHEDULE_INTENT_ENABLED', True)),
             'source': 'env' if os.getenv('SCHEDULE_INTENT_ENABLED') else 'default',
             'component': 'POST /schedule/captures', 'restart_needed': False},
            {'key': 'SCHEDULE_INTENT_MODEL', 'value': intent_model, 'source': model_source,
             'component': 'intent.parse_capture', 'restart_needed': False},
            {'key': 'SCHEDULE_INTENT_LLM_TIMEOUT',
             'value': int(cfg.get('SCHEDULE_INTENT_LLM_TIMEOUT', 45)),
             'source': 'env' if os.getenv('SCHEDULE_INTENT_LLM_TIMEOUT') else 'default',
             'component': 'intent.parse_capture', 'restart_needed': False},
            {'key': 'SCHEDULE_INTENT_DAILY_LIMIT（用户/日）',
             'value': int(cfg.get('SCHEDULE_INTENT_DAILY_LIMIT', 50)),
             'source': 'env' if os.getenv('SCHEDULE_INTENT_DAILY_LIMIT') else 'default',
             'component': 'POST /schedule/captures', 'restart_needed': False},
        ]},
        {'section': '自动排程', 'items': [
            {'key': 'SCHEDULE_PLANNER_ENABLED', 'value': bool(cfg.get('SCHEDULE_PLANNER_ENABLED', True)),
             'source': 'env' if os.getenv('SCHEDULE_PLANNER_ENABLED') else 'default',
             'component': 'planner（POST /tasks、事件重排）', 'restart_needed': False},
        ]},
        {'section': '投递重试（代码常量）', 'items': [
            {'key': 'MAX_ATTEMPTS（失败重试上限）', 'value': MAX_ATTEMPTS,
             'source': 'code', 'component': 'reminders._process_one', 'restart_needed': True},
        ]},
    ]
    secrets = [
        {'key': 'DEEPSEEK_API_KEY', 'configured': bool(os.getenv('DEEPSEEK_API_KEY')),
         'value': None, 'note': '密钥只显示配置状态，修改走 .env 并重启'},
    ]
    from .runtime_config import editable_view
    return {'as_of': _fmt(datetime.now()),
            'scope_note': '只读项为本 API 进程有效值（后台进程启动时各自读取环境变量）；'
                          '「在线配置」区的三个参数发布后即时生效（请求路径直读 DB）',
            'groups': groups, 'secrets': secrets,
            'editable': editable_view()}

"""提醒账本：物料化（随写事务删旧插新）与到期扫描投递（独立小事务）。

物料化策略：对象（event/task/block）每次写操作后由 calendar 调
rematerialize_target —— 删除该目标旧的 pending/cancelled 行，按对象【当前
version】重插 pending 行。target_version 是发送前复核的依据：对象再变版本
即变，旧提醒自然 expired，不会误发。

防重三道防线（站内通道恰好一次）：
1) 唯一键 uq_schedule_reminder_dedup 兜底重复提交（begin_nested 撞键跳过）；
2) 扫描取行 FOR UPDATE(skip_locked)——两个扫描实例并发时后到者跳过或重读
   状态直接放行；
3) create_notification 与状态翻转在同一事务内提交：崩溃要么都没发生要么都
   已发生，不存在「通知已建而状态仍 pending」的重复窗口。

错过不补发（规划文档 §7.3）：开始提醒错过 30 分钟、截止提醒错过 24 小时
直接 expired；服务恢复后不集中轰炸。
"""
from datetime import datetime, timedelta

from sqlalchemy.exc import IntegrityError

from exts import db
from models import ScheduleBlock, ScheduleEvent, ScheduleReminder, ScheduleTask

from .preferences import get_or_create_profile, parse_hhmm

SCAN_BATCH_LIMIT = 200        # 单轮处理上限（余量下一轮 30s 后继续）
MAX_ATTEMPTS = 5              # failed 最多尝试次数，超过留人工排查
RETRY_DELAY = timedelta(seconds=60)
START_MISS_WINDOW = timedelta(minutes=30)   # 开始提醒错过即失效窗口
DUE_MISS_WINDOW = timedelta(hours=24)       # 截止提醒错过即失效窗口

TARGET_MODELS = {'event': ScheduleEvent, 'task': ScheduleTask, 'block': ScheduleBlock}


def _lead_minutes(target_type, obj, profile):
    """事项级提前量优先，否则 profile 默认（block 无事项级字段）。"""
    own = getattr(obj, 'reminder_minutes', None)
    return own if own is not None else profile.default_reminder_minutes


def _task_due_anchor(task, profile):
    """任务截止锚点：date 精度用 profile 日终边界（不伪称 23:59）。"""
    if task.deadline_precision == 'datetime' and task.due_at:
        return task.due_at
    if task.deadline_precision == 'date' and task.due_date:
        return datetime.combine(task.due_date, parse_hhmm(profile.day_end_time, '日终边界'))
    return None


def compute_trigger(target_type, obj, profile=None, now=None):
    """计算提醒触发时刻；返回 (kind, trigger_at) 或 None（不生成）。

    clamp 规则：trigger_at = max(锚点-提前量, now)——临近/略过锚点的事项立即
    提醒一次（扫描侧还有错过窗口兜底，不会追发远古事项）。"""
    now = now or datetime.now()
    profile = profile or get_or_create_profile(obj.owner_id)

    if target_type == 'task':
        anchor = _task_due_anchor(obj, profile)
        kind = 'due'
    elif target_type in ('event', 'block'):
        anchor = obj.start_at
        kind = 'start'
    else:
        raise ValueError(f"未知提醒目标类型 {target_type}")
    if anchor is None:
        return None

    miss = DUE_MISS_WINDOW if kind == 'due' else START_MISS_WINDOW
    if now > anchor + miss:
        return None                      # 错过太久：不生成（写了也不会投递）

    lead = timedelta(minutes=_lead_minutes(target_type, obj, profile))
    # 微秒 floor：MySQL DATETIME 秒精度按四舍五入存（44.8s→45s），不归零会出现
    # 「clamp 到当下却比 now 晚半秒」的假未来时刻
    return kind, max(anchor - lead, now).replace(microsecond=0)


def _target_title(obj):
    """提醒标题快照：event/task 有 title，block 用所属任务标题。"""
    title = getattr(obj, 'title', None)
    if title:
        return title
    task = getattr(obj, 'task', None)
    return task.title if task is not None else f'日程对象 #{obj.id}'


def rematerialize_target(target_type, obj, profile=None, now=None):
    """对象写操作后重算提醒（随调用方事务，不 commit）。返回是否生成了新提醒。"""
    now = now or datetime.now()
    ScheduleReminder.query.filter(
        ScheduleReminder.target_type == target_type,
        ScheduleReminder.target_id == obj.id,
        ScheduleReminder.status.in_(('pending', 'cancelled')),
    ).delete(synchronize_session=False)

    computed = compute_trigger(target_type, obj, profile=profile, now=now)
    if computed is None:
        return False
    kind, trigger_at = computed
    try:
        with db.session.begin_nested():   # 唯一键撞车（同版本重放）只回滚本插入
            db.session.add(ScheduleReminder(
                user_id=obj.owner_id,
                target_type=target_type, target_id=obj.id,
                target_version=obj.version, kind=kind, trigger_at=trigger_at,
                title_snapshot=_target_title(obj), status='pending'))
    except IntegrityError:
        return False
    return True


def cancel_target(target_type, target_id):
    """目标完成/取消/删除时作废其未投递提醒（pending → cancelled）。"""
    ScheduleReminder.query.filter(
        ScheduleReminder.target_type == target_type,
        ScheduleReminder.target_id == target_id,
        ScheduleReminder.status == 'pending',
    ).update({'status': 'cancelled'}, synchronize_session=False)


# ── 到期扫描与投递 ────────────────────────────────────────────────────────

def _verify(row, now, profile_cache):
    """发送前复核（规划文档 §7.3）。返回 (ok, 过期原因, 标题, 锚点时刻)。

    正常路径这些行早被 cancel/materialize 清理，这里是调度侧双保险：
    目标没了 / 版本旧了 / 目标状态已死 / 错过太久 → expired 不投递。"""
    model = TARGET_MODELS.get(row.target_type)
    obj = model.query.get(row.target_id) if model else None
    if obj is None or getattr(obj, 'owner_id', None) != row.user_id:
        return False, 'target_gone', row.title_snapshot, None
    if obj.version != row.target_version:
        return False, 'stale_version', obj.title, None

    if row.target_type == 'task':
        if obj.status != 'open':
            return False, 'target_inactive', obj.title, None
        profile = profile_cache.get(obj.owner_id)
        if profile is None:
            profile = get_or_create_profile(obj.owner_id)
            profile_cache[obj.owner_id] = profile
        anchor = _task_due_anchor(obj, profile)
        if anchor is None:
            return False, 'no_deadline', obj.title, None
    else:
        alive_status = 'active' if row.target_type == 'event' else 'planned'
        if obj.status != alive_status:
            return False, 'target_inactive', obj.title, None
        anchor = obj.start_at

    miss = DUE_MISS_WINDOW if row.kind == 'due' else START_MISS_WINDOW
    if now > anchor + miss:
        return False, 'missed', obj.title, anchor
    return True, None, obj.title, anchor


def _process_one(reminder_id, now, profile_cache):
    """单条提醒的独立小事务：行锁 → 复核 → 同事务建通知+翻转状态 → commit。"""
    from blueprints.notification import create_notification   # 延迟导入避开蓝图包初始化环

    try:
        row = (db.session.query(ScheduleReminder)
               .filter(ScheduleReminder.id == reminder_id)
               .with_for_update(skip_locked=True)
               .populate_existing()      # 强制刷新：预查询的 identity map 对象可能是陈旧状态
               .first())
        if row is None:                        # 被并发实例锁住——本轮跳过
            db.session.rollback()
            return None
        if row.status not in ('pending', 'failed'):
            db.session.commit()
            return None
        if row.status == 'pending' and row.trigger_at > now:
            db.session.commit()
            return None
        if row.status == 'failed' and not (row.next_retry_at and row.next_retry_at <= now):
            db.session.commit()
            return None

        ok, reason, title, anchor = _verify(row, now, profile_cache)
        if not ok:
            row.status = 'expired'
            row.last_error = reason
            row.status_reason_code = reason       # 安全枚举（管理端只出码不出原文）
            db.session.commit()
            return 'expired'

        verb = '截止' if row.kind == 'due' else '开始'
        notification = create_notification(
            user_id=row.user_id, title='日程提醒',
            content=f'「{title}」将于 {anchor.strftime("%m-%d %H:%M")} {verb}',
            category='schedule', source_type='schedule_reminder', source_id=row.id)
        db.session.flush()
        row.status = 'delivered'
        row.delivered_at = now
        row.notification_id = notification.id
        row.last_error = None
        row.last_error_code = None                # 重试成功投递后清旧错误码
        db.session.commit()
        return 'delivered'
    except Exception as exc:
        db.session.rollback()
        try:
            row = db.session.get(ScheduleReminder, reminder_id)
            if row is not None and row.status != 'delivered':
                row.status = 'failed'
                row.attempts += 1
                row.last_error = str(exc)[:500] or 'unknown'
                row.last_error_code = 'notification_write_failed'
                row.next_retry_at = now + RETRY_DELAY
                db.session.commit()
        except Exception:
            db.session.rollback()
        return 'failed'


def scan_due_reminders(now=None):
    """扫描到期提醒并投递站内通知。调度器与冒烟脚本直接调用。

    本函数是日程服务里【唯一】自行 commit 的地方（事务边界见包 docstring）。
    返回 {'delivered': n, 'expired': n, 'failed': n}。"""
    now = now or datetime.now()
    due_ids = [r.id for r in (ScheduleReminder.query
                              .filter(ScheduleReminder.status == 'pending',
                                      ScheduleReminder.trigger_at <= now)
                              .order_by(ScheduleReminder.trigger_at)
                              .limit(SCAN_BATCH_LIMIT).all())]
    retry_ids = [r.id for r in (ScheduleReminder.query
                                .filter(ScheduleReminder.status == 'failed',
                                        ScheduleReminder.attempts < MAX_ATTEMPTS,
                                        ScheduleReminder.next_retry_at <= now)
                                .limit(SCAN_BATCH_LIMIT).all())]
    stats = {'delivered': 0, 'expired': 0, 'failed': 0}
    profile_cache = {}
    for reminder_id in due_ids + retry_ids:
        outcome = _process_one(reminder_id, now, profile_cache)
        if outcome:
            stats[outcome] += 1
    return stats

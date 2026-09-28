"""内部工作台·持久提醒（设计方案 §10.3，M3）。

提醒是持久任务不是页面回调：任务改期/完成/取消/换人时旧提醒按版本自然失效
（regenerate 先取消全部 pending 再按新版本写入，B06）；后台扫描器原子认领、
同事务发通知并标记 sent、失败退避重试、claimed 超时回收（C04），程序重启后
继续处理未完成记录。

object_version 语义随 slot：due_soon/overdue/follow_up = 事项 version；
response_due = 回应请求 id（uq (item,user,slot,object_version) 兜底去重）。
全部函数只 add/update 不 commit——与业务变更或扫描循环同事务。
"""
from datetime import datetime, timedelta

from exts import db
from models import WorkReminder, WorkTask, WorkItem

# 到期前提醒提前量（小时）；到期后转 overdue 提醒（§10.3 一次提醒）
DUE_SOON_HOURS = 24
# 认领超时回收窗口（分钟）：进程崩溃后 claimed 行回 pending（C04）
CLAIM_STALE_MINUTES = 10
# 失败退避序列（分钟）；超过次数上限转 failed 人工介入
RETRY_BACKOFF_MINUTES = (5, 30, 120)
MAX_RETRIES = 5


def _add(item_id, user_id, slot, object_version, trigger_at):
    """写入一条提醒（uq 冲突跳过——同版本重复生成幂等）。"""
    existed = WorkReminder.query.filter_by(
        item_id=item_id, user_id=user_id, slot=slot,
        object_version=object_version, status='pending').first()
    if existed:
        return existed
    row = WorkReminder(item_id=item_id, user_id=user_id, slot=slot,
                       object_version=object_version, trigger_at=trigger_at)
    db.session.add(row)
    return row


def cancel_for_item(item_id):
    """取消事项全部 pending 提醒（改版/终态时调用）。"""
    WorkReminder.query.filter_by(item_id=item_id, status='pending') \
        .update({'status': 'cancelled'}, synchronize_session=False)


def regenerate_for_item(item):
    """按事项当前版本重建任务提醒（调用方持有事项行锁）。

    终态（done/cancelled）只取消不重建；无负责人/无截止不生成 due 提醒；
    follow_up 由 block 命令单独写入（带跟进时间）。"""
    cancel_for_item(item.id)
    if item.kind != 'task' or item.status in ('done', 'cancelled'):
        return
    task = WorkTask.query.filter_by(item_id=item.id).first()
    if not task or not task.assignee_user_id or not task.due_at:
        return
    now = datetime.now()
    due = task.due_at
    soon_at = due - timedelta(hours=DUE_SOON_HOURS)
    if soon_at > now:
        _add(item.id, task.assignee_user_id, 'due_soon', item.version, soon_at)
    # overdue：已过期立即触发（补发一次），未过期按点触发
    _add(item.id, task.assignee_user_id, 'overdue', item.version,
         due if due > now else now)
    # 受阻跟进时间仍有效的保留重建（block 时写入，版本一致则 uq 命中幂等）
    follow = WorkReminder.query.filter_by(
        item_id=item.id, slot='follow_up', object_version=item.version,
        status='pending').first()
    if follow and follow.trigger_at <= now:
        follow.status = 'cancelled'


def add_follow_up(item, follow_up_at):
    """受阻跟进提醒（block 命令）：到点提醒负责人继续跟进。"""
    cancel_follow_ups(item.id)
    _add(item.id, _assignee_of(item), 'follow_up', item.version, follow_up_at)


def cancel_follow_ups(item_id):
    WorkReminder.query.filter_by(item_id=item_id, slot='follow_up',
                                 status='pending') \
        .update({'status': 'cancelled'}, synchronize_session=False)


def for_response_request(request_row):
    """待回应请求的时限提醒（§7.2 回应时限→response_due，M3 补）。"""
    if not request_row.due_at:
        return None
    return _add(request_row.item_id, request_row.responder_user_id,
                'response_due', request_row.id, request_row.due_at)


def _assignee_of(item):
    task = WorkTask.query.filter_by(item_id=item.id).first()
    return task.assignee_user_id if task else None


# ── 扫描（work_scheduler 每分钟调用；可重入、可恢复） ────────

def claim_due(limit=20):
    """原子认领到期 pending 行：条件更新防多实例重复领取。"""
    now = datetime.now()
    rows = (WorkReminder.query
            .filter(WorkReminder.status == 'pending', WorkReminder.trigger_at <= now)
            .order_by(WorkReminder.trigger_at.asc())
            .limit(limit).all())
    claimed = []
    for r in rows:
        updated = (WorkReminder.query
                   .filter(WorkReminder.id == r.id, WorkReminder.status == 'pending')
                   .update({'status': 'claimed', 'claimed_at': now},
                           synchronize_session=False))
        if updated:
            claimed.append(r)
    return claimed


def send_one(reminder):
    """发送单条提醒通知（最小信息；与标记 sent 同事务由调用方提交）。"""
    from blueprints.notification import create_notification
    texts = {
        'due_soon': '你有一项工作即将到期',
        'overdue': '你有一项工作已逾期',
        'follow_up': '你有一项受阻工作到了跟进时间',
        'response_due': '有一条工作事项等待你回复',
    }
    create_notification(reminder.user_id, '内部工作台',
                        texts.get(reminder.slot, '内部工作台提醒'),
                        category='work', source_type='work_item',
                        source_id=reminder.item_id)
    reminder.status = 'sent'
    reminder.sent_at = datetime.now()


def mark_failed(reminder):
    """发送失败：退避重试；超次数转 failed（§10.3 可见状态+人工重试入口）。"""
    reminder.retry_count += 1
    if reminder.retry_count >= MAX_RETRIES:
        reminder.status = 'failed'
        return
    backoff = RETRY_BACKOFF_MINUTES[
        min(reminder.retry_count - 1, len(RETRY_BACKOFF_MINUTES) - 1)]
    reminder.next_retry_at = datetime.now() + timedelta(minutes=backoff)
    # pending + next_retry_at：扫描时只取 trigger_at<=now，退避期自然不重试
    reminder.trigger_at = reminder.next_retry_at
    reminder.status = 'pending'


def recover_stale_claims():
    """认领超时回收（C04）：进程崩溃遗留的 claimed 行回 pending。"""
    threshold = datetime.now() - timedelta(minutes=CLAIM_STALE_MINUTES)
    return (WorkReminder.query
            .filter(WorkReminder.status == 'claimed', WorkReminder.claimed_at < threshold)
            .update({'status': 'pending'}, synchronize_session=False))

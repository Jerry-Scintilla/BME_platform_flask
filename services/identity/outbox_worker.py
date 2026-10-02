"""身份 outbox 消费者（D5 收尾 P0-1，规格 7.1.11/S14/14 章）。

职责：把 identity_outbox 的 pending 行投递为站内通知（NotificationModel），
提交与发送分离——核心写（归并等）已提交，投递失败不回滚业务，退避重试；
attempts 达上限转 failed 并告警日志（超过 24 小时积压由巡检口径覆盖）。

调度：init_identity_outbox_scheduler(app)——APScheduler 每分钟扫一次；
fcntl 文件锁保证多 worker 只启动一个（同 attendance_report 制式）。
幂等：通知按 outbox 行只在置 sent 前创建一次（行级状态即去重键）。
"""
import os
from datetime import datetime

from exts import db

MAX_ATTEMPTS = 5
BATCH_LIMIT = 50


def _build_notification(outbox_row):
    """outbox payload → NotificationModel（不 add，由调用方决定）。

    当前载荷：{'case_id', 'action': 'link_applied', 'to_user'}——按 to_user 在
    案例中的角色（存续方/被归并方）给差异化文案；未知 action 记 failed 不炸循环。
    """
    from models import AccountLinkCaseModel, NotificationModel
    payload = outbox_row.payload or {}
    action = payload.get('action')
    to_user = payload.get('to_user')
    if action != 'link_applied' or not to_user:
        return None, f'未知载荷 action={action}'
    case = db.session.get(AccountLinkCaseModel, payload.get('case_id'))
    if case is None:
        return None, '案例不存在'
    if to_user == case.account_b:
        title, content = '账号关联完成（并入档案）', (
            '你的账号已并入同一人员档案的正式参与账号，本账号转为「已合并」状态：'
            '保留历史记录、不可再登录参与正式业务。如非本人操作，请立即联系负责人申诉。')
    else:
        title, content = '账号关联完成', (
            '账号认领已完成：另一账号的学校身份与档案已并入你当前的人员档案，'
            '刷新「身份与账号」页可查看最新核验状态。')
    n = NotificationModel(
        user_id=to_user, title=title, content=content,
        category='system', source_type='identity')
    return n, None


def deliver_pending(limit=BATCH_LIMIT):
    """扫一批 pending 并投递（不自行 commit——由调度入口统一提交）。

    返回 (sent, failed, skipped) 计数。单行失败不阻断批次：attempts+1 记
    last_error；达 MAX_ATTEMPTS 转 failed 终态并打告警日志。
    """
    from models import IdentityOutboxModel
    rows = IdentityOutboxModel.query.filter_by(
        delivery_state='pending').order_by(IdentityOutboxModel.id).limit(limit).all()
    sent = failed = 0
    for row in rows:
        notification, err = _build_notification(row)
        if err:
            row.attempts += 1
            row.last_error = err[:255]
            if row.attempts >= MAX_ATTEMPTS:
                row.delivery_state = 'failed'
                print(f"[identity-outbox] 行 {row.id} 终态失败：{err}")
            failed += 1
            continue
        try:
            db.session.add(notification)
            row.delivery_state = 'sent'
            row.sent_at = datetime.now()
            sent += 1
        except Exception as exc:  # noqa: BLE001 —— 单行失败不阻断批次
            db.session.rollback()
            row = db.session.merge(row)
            row.attempts += 1
            row.last_error = str(exc)[:255]
            if row.attempts >= MAX_ATTEMPTS:
                row.delivery_state = 'failed'
            failed += 1
    if sent or failed:
        db.session.commit()
    return sent, failed


# ── 调度（fcntl 单实例 + 每分钟扫描；同 attendance_report 制式）────────

_scheduler = None
_lock_fd = None
_LOCK_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '..', '..', 'log',
    '.identity_outbox_scheduler.lock')


def _try_acquire_scheduler_lock():
    """非阻塞排他锁。抢到返回 fd（持有者启动 scheduler），否则 None。"""
    import fcntl
    global _lock_fd
    os.makedirs(os.path.dirname(_LOCK_PATH), exist_ok=True)
    fd = os.open(_LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_fd = fd
        return fd
    except OSError:
        os.close(fd)
        return None


def _scheduled_job(app):
    """APScheduler 触发入口（无 Flask 上下文，手动 push）。"""
    with app.app_context():
        try:
            sent, failed = deliver_pending()
            if sent or failed:
                print(f"[identity-outbox] 投递 sent={sent} failed={failed}")
        except Exception as e:  # noqa: BLE001
            print(f"[identity-outbox] 调度任务失败: {e}")


def init_identity_outbox_scheduler(app):
    """启动 outbox 消费调度（多 worker 仅一个生效；启动时先清一次积压）。"""
    global _scheduler
    if _scheduler is not None:
        return
    if _try_acquire_scheduler_lock() is None:
        return
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.interval import IntervalTrigger
    _scheduler = BackgroundScheduler(daemon=True)
    _scheduler.add_job(
        lambda: _scheduled_job(app),
        trigger=IntervalTrigger(seconds=60),
        id='identity_outbox_deliver',
        max_instances=1,
        coalesce=True,
    )
    _scheduler.start()
    _scheduled_job(app)  # 启动即清一次积压（部署后尽快补投递）
    print("[identity-outbox] 消费调度已启动（60s 间隔，fcntl 单实例）")

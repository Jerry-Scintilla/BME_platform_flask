"""内部工作台调度器 — 持久提醒不依赖页面访问（设计方案 §10.3，M3）。

两个任务（全部幂等，可安全补跑）：
1. work_reminder_scan（每分钟）：原子认领到期 pending 提醒 → 同事务发站内通知
   + 标记 sent；失败退避重试（5/30/120 分钟，≥5 次转 failed）。claim 与 send
   同事务提交：进程崩溃即整体回滚回 pending（C04 崩溃恢复由此保证）；
   认领超时回收（claimed 超 10 分钟回 pending）为纵深防御，正常不可达。
2. work_transfer_expire_scan（每 5 分钟）：过期待确认转交 → expired + 通知发起人
   （B03：过期后当前负责人保持不变；逐行 FOR UPDATE 复查 status，与 accept 并发安全）。

基础设施沿用 attendance_report / camp_scheduler 模式：APScheduler + fcntl 文件锁
（多 worker 单实例），崩溃后内核回收锁由新 worker 接管。
"""
import atexit
import logging
import os

try:
    import fcntl  # Unix 专属；Windows 下为 None（开发环境单进程退化为无跨进程锁）
except ImportError:
    fcntl = None

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from exts import db

bp = None   # 本模块只有调度任务，无 HTTP 端点（占位避免被当蓝图注册）

_scheduler = None
_lock_fd = None
_LOCK_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "log", ".work_scheduler.lock")

logger = logging.getLogger(__name__)


def _try_acquire_lock():
    global _lock_fd
    os.makedirs(os.path.dirname(_LOCK_PATH), exist_ok=True)
    fd = os.open(_LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o644)
    if fcntl is None:
        _lock_fd = fd
        atexit.register(_release_lock)
        return fd
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    _lock_fd = fd
    atexit.register(_release_lock)
    return fd


def _release_lock():
    global _lock_fd
    if _lock_fd is not None:
        try:
            if fcntl is not None:
                fcntl.flock(_lock_fd, fcntl.LOCK_UN)
            os.close(_lock_fd)
        finally:
            _lock_fd = None


def _job_reminder_scan(app):
    """到期提醒：认领→发送→标记，单事务提交；任何一条失败不影响其余。"""
    with app.app_context():
        from services.work import reminders
        try:
            reminders.recover_stale_claims()
            rows = reminders.claim_due(limit=20)
            for r in rows:
                try:
                    reminders.send_one(r)
                    db.session.commit()
                except Exception:
                    db.session.rollback()
                    try:
                        fresh = db.session.get(type(r), r.id)
                        if fresh:
                            reminders.mark_failed(fresh)
                            db.session.commit()
                    except Exception:
                        db.session.rollback()
                        app.logger.exception("[work_scheduler] 提醒重试标记失败 id=%s", r.id)
            if rows:
                app.logger.info("[work_scheduler] 已发送 %d 条工作提醒", len(rows))
        except Exception:
            db.session.rollback()
            app.logger.exception("[work_scheduler] 提醒扫描失败")


def _job_expiration_scan(app):
    with app.app_context():
        from services.work import tasks, handoffs
        try:
            n = tasks.expire_transfers()
            db.session.commit()
            if n:
                app.logger.info("[work_scheduler] %d 条转交已过期", n)
        except Exception:
            db.session.rollback()
            app.logger.exception("[work_scheduler] 转交过期扫描失败")
        try:
            m = handoffs.expire_handoffs()
            db.session.commit()
            if m:
                app.logger.info("[work_scheduler] %d 条跨组交付已过期", m)
        except Exception:
            db.session.rollback()
            app.logger.exception("[work_scheduler] 交付过期扫描失败")


def init_work_scheduler(app):
    global _scheduler
    if _scheduler is not None:
        return
    if not app.config.get("WORK_SCHEDULER_ENABLED", True):
        app.logger.info("[work_scheduler] 已通过 WORK_SCHEDULER_ENABLED 关闭")
        return
    if _try_acquire_lock() is None:
        app.logger.info("[work_scheduler] 另一进程已持有锁，本进程不启动")
        return
    tz_name = app.config.get("ATTENDANCE_REPORT_TIMEZONE", "Asia/Shanghai")
    sched = BackgroundScheduler(timezone=tz_name)
    sched.add_job(
        _job_reminder_scan, trigger=IntervalTrigger(minutes=1, timezone=tz_name),
        args=[app], id="work_reminder_scan",
        coalesce=True, max_instances=1, misfire_grace_time=300, replace_existing=True)
    sched.add_job(
        _job_transfer_expire, trigger=IntervalTrigger(minutes=5, timezone=tz_name),
        args=[app], id="work_expiration_scan",
        coalesce=True, max_instances=1, misfire_grace_time=600, replace_existing=True)
    sched.start()
    _scheduler = sched
    app.logger.info("[work_scheduler] 已启动：到期提醒（1 分钟扫描）+ 转交过期（5 分钟扫描）")

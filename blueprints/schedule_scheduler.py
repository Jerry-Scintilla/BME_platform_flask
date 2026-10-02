"""个人日程提醒扫描调度器（2026-09-24 · AI 日程模块 Phase 1）。

单任务 schedule_reminder_scan：每 SCHEDULE_REMINDER_SCAN_INTERVAL_SECONDS
（默认 30 秒）扫描到期提醒并投递站内通知（category='schedule'）。与营期
调度的 30 分钟粒度不同，这里必须秒级——规划文档 §8 明确个人提醒不能沿用
30 分钟精度。

基础设施沿用 camp_scheduler 模式：APScheduler + fcntl 文件锁（多 worker 只
启动一个），崩溃后内核回收锁由新 worker 接管；但锁文件/开关/调度器实例
全部独立，互不牵连重启。扫描本身幂等且每条提醒独立小事务（恰好一次投递
见 services/schedule/reminders.py docstring），重复执行安全。
"""
import atexit
import os

try:
    import fcntl  # Unix 专属；Windows 下为 None（开发环境单进程退化为无跨进程锁）
except ImportError:
    fcntl = None

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

bp = None   # 本模块只有调度任务，无 HTTP 端点（占位避免被当蓝图注册）

_scheduler = None
_lock_fd = None
_LOCK_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "log", ".schedule_scheduler.lock")


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
    """扫描入口（09-24 批次 A 起包装心跳）：成功/失败/零条扫描都落
    schedule_service_runtime 心跳，管理端据此推导服务状态。"""
    from services.schedule.observability import run_reminder_scan_with_heartbeat
    run_reminder_scan_with_heartbeat(app)


def _job_capture_sweep(app):
    """超时录入兜底扫描（S01 整改新增）：把卡死在 pending/processing 超过
    STALE_AFTER 的 capture 判死，使恢复不再依赖用户读请求轮询触发。
    条件更新幂等，异常自吞不影响调度器。"""
    from services.schedule import capture as capture_svc
    try:
        with app.app_context():
            capture_svc.sweep_stale()
    except Exception:
        app.logger.exception("[schedule_scheduler] capture 超时兜底扫描失败")


def init_schedule_scheduler(app):
    global _scheduler
    if _scheduler is not None:
        return
    if not app.config.get("SCHEDULE_REMINDER_SCAN_ENABLED", True):
        app.logger.info("[schedule_scheduler] 已通过 SCHEDULE_REMINDER_SCAN_ENABLED 关闭")
        return
    if _try_acquire_lock() is None:
        app.logger.info("[schedule_scheduler] 另一进程已持有锁，本进程不启动")
        return
    tz_name = app.config.get("ATTENDANCE_REPORT_TIMEZONE", "Asia/Shanghai")
    interval = max(5, int(app.config.get("SCHEDULE_REMINDER_SCAN_INTERVAL_SECONDS", 30)))
    sched = BackgroundScheduler(timezone=tz_name)
    sched.add_job(
        _job_reminder_scan, trigger=IntervalTrigger(seconds=interval, timezone=tz_name),
        args=[app], id="schedule_reminder_scan",
        coalesce=True, max_instances=1, misfire_grace_time=120, replace_existing=True)
    sched.add_job(
        _job_capture_sweep, trigger=IntervalTrigger(seconds=interval, timezone=tz_name),
        args=[app], id="schedule_capture_sweep",
        coalesce=True, max_instances=1, misfire_grace_time=120, replace_existing=True)
    sched.start()
    _scheduler = sched
    app.logger.info(f"[schedule_scheduler] 已启动：提醒扫描 + capture 超时兜底（每 {interval} 秒，{tz_name}）")

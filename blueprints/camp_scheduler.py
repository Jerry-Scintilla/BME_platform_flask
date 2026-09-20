"""营期调度器 — 时间型通知不再依赖页面访问（通知方案 §5.7，2026-09-20）。

两个任务（全部幂等，可安全补跑）：
1. camp_deadline_reminder（每 30 分钟）：选导生 collecting 阶段、志愿截止在未来 24h 内
   且尚未提醒过的营——未提交志愿的学员收提醒，责任人收「N 人未提交」摘要。
   幂等：Redis SETNX 游标 camp:ms_deadline_remind:{sid}:{deadline_ts}（7 天过期），
   Redis 不可用降级跳过（绝不裸发全营重复通知，与 _maybe_notify_transition 同口径）。
2. camp_attendance_daily_digest（每天 08:30，汇总昨日）：running 且每日承诺考勤的营，
   昨日异常（缺勤/时长不足/迟到）按导生分组发团队摘要，责任人收全营汇总；
   全员无异常不发（降噪）。幂等：Redis 游标 camp:att_digest:{sid}:{date}。

调度基础设施沿用 attendance_report 的模式：APScheduler + fcntl 文件锁（多 worker
只启动一个），崩溃后内核回收锁由新 worker 接管。评估口径直接复用 camp._eval_day
（不在通知代码里重算考勤，方案 §6.8）。
"""
import atexit
import os
from datetime import date, datetime, timedelta

try:
    import fcntl  # Unix 专属；Windows 下为 None（开发环境单进程退化为无跨进程锁）
except ImportError:
    fcntl = None

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from exts import db, redis_client

bp = None   # 本模块只有调度任务，无 HTTP 端点（占位避免被当蓝图注册）

_scheduler = None
_lock_fd = None
_LOCK_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "log", ".camp_scheduler.lock")


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


def _redis_guard(key, ttl_seconds=None):
    """幂等游标：首次见到 key 返回 True（并占位），重复见返回 False；Redis 故障返回
    False（跳过本次——宁可不发，不发重复）。"""
    try:
        return bool(redis_client.set(key, "1", nx=True, ex=ttl_seconds or 86400))
    except Exception:
        return False


# ─────────────────────────────────────────────
# 1. 志愿截止提醒（T-24h）
# ─────────────────────────────────────────────

def _job_deadline_reminder(app):
    with app.app_context():
        try:
            _deadline_reminder_scan()
        except Exception as e:
            app.logger.exception(f"[camp_scheduler] 志愿截止提醒失败: {e}")


def _deadline_reminder_scan(now=None):
    """collecting 阶段、24h 内截止且未提醒的营 → 未交志愿学员 + 责任人。
    直接调用（app_context 内）便于手动补跑与测试。"""
    from models import CampSession, CampMember, CampMentorPreference
    from .camp_ms import MS_COLLECTING, _ms_phase
    from .camp_staff import camp_responsible_ids
    from .notification import create_notification

    now = now or datetime.now()
    camps = CampSession.query.filter(
        CampSession.status == 'selecting',
        CampSession.mentor_selection_enabled.is_(True)).all()
    sent = 0
    for camp in camps:
        if _ms_phase(camp, now) != MS_COLLECTING:
            continue
        dl = camp.ms_preference_deadline
        if not dl or not (now < dl <= now + timedelta(hours=24)):
            continue
        # 幂等：同一截止时刻只提醒一轮（改期后重发——deadline_ts 变化即新 key）
        if not _redis_guard(f"camp:ms_deadline_remind:{camp.id}:{dl.strftime('%Y%m%d%H%M')}",
                            ttl_seconds=7 * 86400):
            continue
        submitted = {p.student_user_id for p in CampMentorPreference.query
                     .filter_by(camp_session_id=camp.id).all()}
        pending = [m for m in CampMember.query.filter_by(
            camp_session_id=camp.id, role='student').all()
            if m.user_id not in submitted]
        dl_text = dl.strftime("%m-%d %H:%M")
        for m in pending:
            create_notification(
                m.user_id, "志愿即将截止",
                f"「{camp.name}」选导生志愿将在 {dl_text} 截止，你尚未提交——"
                f"截止后由老师统一协调指派，请尽快进入营期提交。",
                category='camp', source_type='mentor_selection',
                source_id=camp.id, camp_session_id=camp.id, is_important=True)
        if pending:
            for uid in camp_responsible_ids(camp.id):
                create_notification(
                    uid, "选导生志愿即将截止",
                    f"「{camp.name}」志愿 {dl_text} 截止，仍有 {len(pending)} 名学员未提交。",
                    category='camp', source_type='camp_admin',
                    source_id=camp.id, camp_session_id=camp.id)
        sent += 1
        db.session.commit()
    return sent


# ─────────────────────────────────────────────
# 2. 考勤日摘要（次日上午汇总昨日）
# ─────────────────────────────────────────────

ABNORMAL_STATES = ('absent', 'short_hours', 'late', 'late_and_short')
ABNORMAL_LABELS = {'absent': '缺勤', 'short_hours': '时长不足',
                   'late': '迟到', 'late_and_short': '迟到且时长不足'}


def _job_attendance_digest(app):
    with app.app_context():
        try:
            _attendance_digest_scan(date.today() - timedelta(days=1))
        except Exception as e:
            app.logger.exception(f"[camp_scheduler] 考勤日摘要失败: {e}")


def _attendance_digest_scan(day, *, force=False):
    """某日考勤异常摘要：导生收本队明细，责任人收全营汇总；无异常不发。
    force=True 跳过幂等游标（手动补跑指定日期）。直接调用便于测试。"""
    from models import CampSession, CampMember, CampAttendancePlan, CheckRecord
    from .camp import (_eval_day, _approved_leave_dates, _attendance_mode)
    from .camp_staff import camp_responsible_ids
    from .notification import create_notification

    camps = CampSession.query.filter(CampSession.status == 'running').all()
    digested = 0
    for camp in camps:
        if _attendance_mode(camp) != 'daily':
            continue
        if not force and not _redis_guard(f"camp:att_digest:{camp.id}:{day.isoformat()}",
                                          ttl_seconds=8 * 86400):
            continue
        students = CampMember.query.filter_by(
            camp_session_id=camp.id, role='student').all()
        if not students:
            continue
        plans = CampAttendancePlan.query.filter_by(camp_session_id=camp.id, date=day).all()
        plan_by_user = {p.user_id: p for p in plans}
        records = CheckRecord.query.filter(CheckRecord.date == day).all()
        recs_by_user = {}
        for r in records:
            recs_by_user.setdefault(r.user_id, []).append(r)
        leave_set = _approved_leave_dates(camp.id, day, day)
        # 异常明细：user_id -> (状态, 是否本日承诺)
        abnormal = {}
        for s in students:
            plan = plan_by_user.get(s.user_id)
            if not plan:
                continue                     # 非承诺日不判
            ev = _eval_day(recs_by_user.get(s.user_id, []), plan,
                          (s.user_id, day) in leave_set, is_today=False)
            if ev["status"] in ABNORMAL_STATES:
                abnormal[s.user_id] = ev["status"]
        if not abnormal:
            db.session.commit()
            continue
        # 按导生分组（未分配学员单独一组，归责任人）
        from models import UserModel
        names = {u.id: u.username for u in UserModel.query.filter(
            UserModel.id.in_(list(abnormal) + [s.team_mentor_id for s in students
                                              if s.team_mentor_id]))}
        team = {}
        unassigned = []
        for s in students:
            if s.user_id in abnormal:
                if s.team_mentor_id:
                    team.setdefault(s.team_mentor_id, []).append(s.user_id)
                else:
                    unassigned.append(s.user_id)
        day_text = day.isoformat()
        for mentor_id, uids in team.items():
            lines = "；".join(f"{names.get(uid, uid)}（{ABNORMAL_LABELS[abnormal[uid]]}）"
                             for uid in uids)
            create_notification(
                mentor_id, "团队考勤日摘要",
                f"「{camp.name}」{day_text} 本团队考勤异常 {len(uids)} 人：{lines}。",
                category='camp', source_type='camp_session',
                source_id=camp.id, camp_session_id=camp.id)
        total = len(abnormal)
        camp_line = (f"共 {total} 人异常（未分组 {len(unassigned)} 人）；"
                     f"{'、'.join(f'{ABNORMAL_LABELS[s]}x{sum(1 for v in abnormal.values() if v == s)}'
                                 for s in ABNORMAL_STATES if any(v == s for v in abnormal.values()))}")
        for uid in camp_responsible_ids(camp.id):
            create_notification(
                uid, "全营考勤日摘要",
                f"「{camp.name}」{day_text} {camp_line}。明细见各导生团队摘要与考勤看板。",
                category='camp', source_type='camp_admin',
                source_id=camp.id, camp_session_id=camp.id)
        digested += 1
        db.session.commit()
    return digested


# ─────────────────────────────────────────────
# 初始化（fcntl 多 worker 单例，同 attendance_report 模式）
# ─────────────────────────────────────────────

def init_camp_scheduler(app):
    global _scheduler
    if _scheduler is not None:
        return
    if not app.config.get("CAMP_SCHEDULER_ENABLED", True):
        app.logger.info("[camp_scheduler] 已通过 CAMP_SCHEDULER_ENABLED 关闭")
        return
    if _try_acquire_lock() is None:
        app.logger.info("[camp_scheduler] 另一进程已持有锁，本进程不启动")
        return
    tz_name = app.config.get("ATTENDANCE_REPORT_TIMEZONE", "Asia/Shanghai")
    sched = BackgroundScheduler(timezone=tz_name)
    sched.add_job(
        _job_deadline_reminder, trigger=IntervalTrigger(minutes=30, timezone=tz_name),
        args=[app], id="camp_deadline_reminder",
        coalesce=True, max_instances=1, misfire_grace_time=600, replace_existing=True)
    sched.add_job(
        _job_attendance_digest,
        trigger=CronTrigger(hour=8, minute=30, timezone=tz_name),
        args=[app], id="camp_attendance_daily_digest",
        coalesce=True, max_instances=1, misfire_grace_time=7200, replace_existing=True)
    sched.start()
    _scheduler = sched
    app.logger.info(f"[camp_scheduler] 已启动：志愿截止提醒（30 分钟扫描）+ "
                    f"考勤日摘要（每天 08:30，{tz_name}）")

"""
每日出勤报告蓝图 — 每天 00:00 自动汇总昨日全平台出勤明细并发邮件

数据来源：CheckRecord（学习中心座位打卡记录）
触发方式：
  1. APScheduler 后台定时（每天 00:00 Asia/Shanghai），见 init_scheduler
  2. POST /attendance-report/send_now 手动触发/补发（管理员）

报告形态：纯出勤明细。昨日所有打卡记录按 user 聚合（一人一天可能多条段），
输出 HTML 表格正文 + CSV 附件，发送给 .env 中配置的 ATTENDANCE_REPORT_RECIPIENTS。
"""
import atexit
import csv
try:
    import fcntl  # Unix 专属；Windows 下为 None（开发环境单进程退化为无跨进程锁）
except ImportError:
    fcntl = None
import io
import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from flask import Blueprint, request, jsonify, current_app, render_template_string
from flask_jwt_extended import jwt_required, get_jwt_identity
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from exts import db
from models import UserModel, CheckRecord, PermissionModel, UserPermissionModel
from .notification import send_report_emails

bp = Blueprint("attendance_report", __name__, url_prefix="/attendance-report")

SHANGHAI = ZoneInfo("Asia/Shanghai")
WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

# 收件人资格通过 RBAC 权限表达：拥有此权限的用户即为出勤报告收件人。
# 增删收件人复用 /permission/assign 与 /permission/revoke，无需新建表或接口。
RECIPIENT_PERMISSION = "attendance_report_recipient"


# ────────────────────────────────────────
# 日期口径（时区防御：禁用裸 date.today() / datetime.now()）
# ────────────────────────────────────────

def _yesterday_in_shanghai(target=None):
    """返回 Asia/Shanghai 时区的昨日 date。传入 target 则直接用（手动补发）。"""
    if target is not None:
        return target
    return (datetime.now(SHANGHAI) - timedelta(days=1)).date()


# ────────────────────────────────────────
# 数据汇总
# ────────────────────────────────────────

def collect_attendance(target_date):
    """
    汇总 target_date 当日全平台出勤明细，按 user 聚合。

    一人一天可能存在多条 CheckRecord（如上午、下午各打卡一段），按 user 聚合：
      duration = SUM(duration)   check_in = MIN(check_in)
      check_out = MAX(check_out) segments = 记录条数
    """
    records = CheckRecord.query.filter_by(date=target_date).all()

    agg = {}  # user_id -> {duration, check_in, check_out, segments}
    for r in records:
        a = agg.setdefault(r.user_id, {
            "duration": 0.0, "check_in": None, "check_out": None, "segments": 0,
        })
        a["segments"] += 1
        a["duration"] += (r.duration or 0)
        if r.check_in is not None:
            a["check_in"] = r.check_in if a["check_in"] is None else min(a["check_in"], r.check_in)
        if r.check_out is not None:
            a["check_out"] = r.check_out if a["check_out"] is None else max(a["check_out"], r.check_out)

    user_map = {}
    if agg:
        users = UserModel.query.filter(UserModel.id.in_(agg.keys())).all()
        user_map = {u.id: u for u in users}

    rows = []
    total_hours = 0.0
    for uid, a in agg.items():
        u = user_map.get(uid)
        rows.append({
            "user_id": uid,
            "name": u.username if u else f"用户{uid}",
            "student_id": u.student_id if u else None,
            "check_in": a["check_in"],
            "check_out": a["check_out"],
            "duration": round(a["duration"], 2),
            "segments": a["segments"],
        })
        total_hours += a["duration"]

    rows.sort(key=lambda x: x["duration"], reverse=True)

    return {
        "date": target_date.isoformat(),
        "weekday": WEEKDAYS[target_date.weekday()],
        "summary": {
            "person_count": len(rows),
            "segment_count": sum(r["segments"] for r in rows),
            "total_hours": round(total_hours, 2),
        },
        "records": rows,
    }


# ────────────────────────────────────────
# 渲染
# ────────────────────────────────────────

HTML_TEMPLATE = """
<div style="font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;max-width:880px;color:#333;">
  <h2 style="margin-bottom:4px;">BME 平台每日出勤汇总</h2>
  <p style="color:#666;margin-top:0;">{{ date }} {{ weekday }}　共 {{ summary.person_count }} 人出勤</p>

  <table style="border-collapse:collapse;width:100%;max-width:520px;margin:12px 0;font-size:14px;">
    <tr style="background:#f5f7fa;">
      <td style="padding:8px 12px;border:1px solid #e3e8ee;">出勤人次</td>
      <td style="padding:8px 12px;border:1px solid #e3e8ee;">{{ summary.person_count }} 人</td>
      <td style="padding:8px 12px;border:1px solid #e3e8ee;">打卡段数</td>
      <td style="padding:8px 12px;border:1px solid #e3e8ee;">{{ summary.segment_count }} 次</td>
    </tr>
    <tr style="background:#f5f7fa;">
      <td style="padding:8px 12px;border:1px solid #e3e8ee;">总学习时长</td>
      <td style="padding:8px 12px;border:1px solid #e3e8ee;" colspan="3">{{ summary.total_hours }} 小时</td>
    </tr>
  </table>

  <h3 style="margin-top:24px;">出勤明细（按时长降序）</h3>
  <table style="border-collapse:collapse;width:100%;font-size:13px;">
    <thead>
      <tr style="background:#eef2f7;">
        <th style="padding:8px 10px;border:1px solid #dce3ec;text-align:left;">姓名</th>
        <th style="padding:8px 10px;border:1px solid #dce3ec;text-align:left;">学号</th>
        <th style="padding:8px 10px;border:1px solid #dce3ec;text-align:left;">最早签到</th>
        <th style="padding:8px 10px;border:1px solid #dce3ec;text-align:left;">最晚签退</th>
        <th style="padding:8px 10px;border:1px solid #dce3ec;text-align:right;">时长(小时)</th>
        <th style="padding:8px 10px;border:1px solid #dce3ec;text-align:right;">段数</th>
      </tr>
    </thead>
    <tbody>
    {% for m in records %}
      <tr>
        <td style="padding:6px 10px;border:1px solid #dce3ec;">{{ m.name }}</td>
        <td style="padding:6px 10px;border:1px solid #dce3ec;">{{ m.student_id if m.student_id else '-' }}</td>
        <td style="padding:6px 10px;border:1px solid #dce3ec;">{{ m.check_in.strftime('%H:%M') if m.check_in else '-' }}</td>
        <td style="padding:6px 10px;border:1px solid #dce3ec;">{{ m.check_out.strftime('%H:%M') if m.check_out else '-' }}</td>
        <td style="padding:6px 10px;border:1px solid #dce3ec;text-align:right;">{{ '%.2f'|format(m.duration) }}</td>
        <td style="padding:6px 10px;border:1px solid #dce3ec;text-align:right;">{{ m.segments }}</td>
      </tr>
    {% else %}
      <tr><td style="padding:12px;border:1px solid #dce3ec;color:#999;" colspan="6">当日无出勤记录</td></tr>
    {% endfor %}
    </tbody>
  </table>

  <p style="color:#999;margin-top:24px;font-size:12px;">— BME 卓越工程师在线教育平台 · 系统自动发送 · 完整明细见附件 CSV</p>
</div>
"""


def build_csv(data):
    """生成带 UTF-8 BOM 的 CSV 字节串（Excel 可正确识别中文）。"""
    buf = io.StringIO()
    buf.write("﻿")  # BOM
    w = csv.writer(buf)
    w.writerow(["日期", "学号", "姓名", "最早签到", "最晚签退", "时长(小时)", "打卡段数"])
    for m in data["records"]:
        w.writerow([
            data["date"],
            m["student_id"] if m["student_id"] else "",
            m["name"],
            m["check_in"].strftime("%Y-%m-%d %H:%M:%S") if m["check_in"] else "",
            m["check_out"].strftime("%Y-%m-%d %H:%M:%S") if m["check_out"] else "",
            f"{m['duration']:.2f}",
            m["segments"],
        ])
    return buf.getvalue().encode("utf-8")


# ────────────────────────────────────────
# 收件人（RBAC 权限）
# ────────────────────────────────────────

def _get_recipient_user_ids():
    """返回拥有 attendance_report.recipient 权限的 user_id 列表。"""
    perm = PermissionModel.query.filter_by(name=RECIPIENT_PERMISSION).first()
    if not perm:
        return []
    rows = UserPermissionModel.query.filter_by(permission_id=perm.id).all()
    return [r.user_id for r in rows]


def ensure_recipient_permission(app):
    """幂等确保收件人权限存在（app 启动时调用）。

    seed.py 也会创建该权限，但已运行的环境未必重跑 seed，这里兜底。
    多 worker 并发创建因 name unique 约束，失败方回滚忽略即可。
    """
    with app.app_context():
        try:
            if not PermissionModel.query.filter_by(name=RECIPIENT_PERMISSION).first():
                db.session.add(PermissionModel(
                    name=RECIPIENT_PERMISSION,
                    description="接收每日出勤汇总邮件",
                ))
                db.session.commit()
                app.logger.info(f"[attendance_report] 已创建权限 {RECIPIENT_PERMISSION}")
        except Exception as e:
            db.session.rollback()
            app.logger.warning(f"[attendance_report] ensure 收件人权限跳过: {e}")


# ────────────────────────────────────────
# 总入口：汇总 → 渲染 → 发送
# ────────────────────────────────────────

def _build_and_send(target_date):
    """汇总 target_date 出勤，渲染 HTML + CSV，发给配置的收件人。

    scheduler 定时任务与手动接口共用此函数。需在 app context 内调用。
    返回汇总数据（供手动接口回显）。
    """
    data = collect_attendance(target_date)
    html = render_template_string(HTML_TEMPLATE, **data)
    csv_bytes = build_csv(data)

    user_ids = _get_recipient_user_ids()
    title = f"每日出勤汇总 {target_date.isoformat()}"
    content = (
        f"{data['date']} 出勤汇总：{data['summary']['person_count']} 人，"
        f"共 {data['summary']['segment_count']} 段打卡，"
        f"总时长 {data['summary']['total_hours']} 小时。完整明细见附件。"
    )
    send_report_emails(
        user_ids=user_ids,
        title=title,
        content=content,
        html=html,
        attachments=[{
            "filename": f"attendance_{target_date.isoformat()}.csv",
            "content_type": "text/csv",
            "data": csv_bytes,
        }],
    )
    return data


def _scheduled_job(app):
    """APScheduler 触发入口（无 Flask 上下文，需手动 push app context）。"""
    with app.app_context():
        try:
            target = _yesterday_in_shanghai()
            app.logger.info(f"[attendance_scheduler] 开始生成 {target} 出勤报告")
            _build_and_send(target)
        except Exception as e:
            app.logger.exception(f"[attendance_scheduler] 任务失败: {e}")


# ────────────────────────────────────────
# scheduler 单例（fcntl 文件锁保证多 worker 只启动一个）
# ────────────────────────────────────────

_scheduler = None
_lock_fd = None
_LOCK_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "log", ".attendance_scheduler.lock"
)


def _try_acquire_scheduler_lock():
    """非阻塞排他锁。抢到返回 fd（持有者可启动 scheduler），否则返回 None。

    Windows 无 fcntl，开发环境单进程下直接持有 fd（不做跨进程锁）。
    """
    global _lock_fd
    os.makedirs(os.path.dirname(_LOCK_PATH), exist_ok=True)
    fd = os.open(_LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o644)
    if fcntl is None:
        _lock_fd = fd
        atexit.register(_release_scheduler_lock)
        return fd
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    _lock_fd = fd
    atexit.register(_release_scheduler_lock)
    return fd


def _release_scheduler_lock():
    global _lock_fd
    if _lock_fd is not None:
        try:
            if fcntl is not None:
                fcntl.flock(_lock_fd, fcntl.LOCK_UN)
            os.close(_lock_fd)
        finally:
            _lock_fd = None


def init_scheduler(app):
    """初始化每日出勤报告定时任务。

    生产 gunicorn workers>1 时，每个 worker 都会执行到此函数；fcntl 文件锁
    保证只有一个 worker 真正启动 scheduler，其余直接返回。worker 崩溃后内核
    自动回收锁，新 worker 可接管。
    """
    global _scheduler
    if _scheduler is not None:
        return
    if not app.config.get("ATTENDANCE_REPORT_ENABLED", True):
        app.logger.info("[attendance_scheduler] 已通过 ATTENDANCE_REPORT_ENABLED 关闭")
        return
    if _try_acquire_scheduler_lock() is None:
        app.logger.info("[attendance_scheduler] 另一进程已持有锁，本进程不启动")
        return

    tz_name = app.config.get("ATTENDANCE_REPORT_TIMEZONE", "Asia/Shanghai")
    sched = BackgroundScheduler(timezone=tz_name)
    sched.add_job(
        _scheduled_job,
        trigger=CronTrigger(hour=0, minute=0, timezone=tz_name),
        args=[app],
        id="attendance_daily_report",
        coalesce=True,
        max_instances=1,
        misfire_grace_time=3600,
        replace_existing=True,
    )
    sched.start()
    _scheduler = sched
    app.logger.info(f"[attendance_scheduler] 已启动，每天 00:00 ({tz_name}) 汇总昨日出勤")


# ────────────────────────────────────────
# 接口
# ────────────────────────────────────────

@bp.route("/recipients", methods=["GET"])
@jwt_required()
def list_recipients():
    """查看当前出勤报告收件人（拥有 attendance_report.recipient 权限的用户）。仅管理员。

    增删收件人请用 /permission/assign 与 /permission/revoke
    （传 permission_name='attendance_report.recipient' 或对应 permission_id）。
    """
    user = UserModel.query.filter_by(email=get_jwt_identity()).first()
    if not user:
        return jsonify({"code": 401, "message": "用户未认证"}), 401
    if user.user_mode != "admin":
        return jsonify({"code": 403, "message": "权限不足，仅管理员可查看"}), 403

    perm = PermissionModel.query.filter_by(name=RECIPIENT_PERMISSION).first()
    if not perm:
        return jsonify({"code": 200, "data": {
            "permission": RECIPIENT_PERMISSION, "recipients": [], "count": 0,
        }}), 200

    ups = UserPermissionModel.query.filter_by(permission_id=perm.id).all()
    uids = [up.user_id for up in ups]
    users = {u.id: u for u in UserModel.query.filter(UserModel.id.in_(uids)).all()} if uids else {}
    recipients = [
        {"user_id": uid, "username": users[uid].username, "email": users[uid].email}
        for uid in uids if uid in users
    ]
    return jsonify({"code": 200, "data": {
        "permission": RECIPIENT_PERMISSION,
        "recipients": recipients,
        "count": len(recipients),
    }}), 200


@bp.route("/send_now", methods=["POST"])
@jwt_required()
def send_now():
    """
    手动触发出勤报告邮件（仅管理员），用于测试或补发。

    Body（可选）:
      date — 'YYYY-MM-DD'，补发指定日期；不传则默认昨日
    """
    email_identity = get_jwt_identity()
    user = UserModel.query.filter_by(email=email_identity).first()
    if not user:
        return jsonify({"code": 401, "message": "用户未认证"}), 401
    if user.user_mode != "admin":
        return jsonify({"code": 403, "message": "权限不足，仅管理员可触发"}), 403

    data = request.get_json(silent=True) or {}
    target = None
    raw = data.get("date")
    if raw:
        try:
            target = date.fromisoformat(raw)
        except ValueError:
            return jsonify({"code": 400, "message": "date 格式应为 YYYY-MM-DD"}), 400
    target = _yesterday_in_shanghai(target)

    try:
        result = _build_and_send(target)
    except Exception as e:
        current_app.logger.exception(f"[attendance_report] 手动发送失败: {e}")
        return jsonify({"code": 500, "message": f"发送失败: {e}"}), 500

    return jsonify({
        "code": 200,
        "message": f"已触发 {target.isoformat()} 出勤报告邮件",
        "data": {
            "date": target.isoformat(),
            "person_count": result["summary"]["person_count"],
            "total_hours": result["summary"]["total_hours"],
        },
    }), 200

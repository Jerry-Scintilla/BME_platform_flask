"""日程服务心跳（管理面板批次 A，migrate_56）。

首批仅 service_key='reminder_scan'（唯一周期服务）。每个锁持有进程一行
（UQ(service_key, instance_id)，instance_id 懒生成 uuid4.hex）；fcntl 锁保证
任一时刻单写者，「最新行（updated_at DESC, id DESC 双键）」即当前活实例，
旧行是进程死亡/接管的残留，不算失联。

已知 quirk（文档化不修，camp_scheduler 同款）：dev reloader 下父进程先完成
模块导入并持有 flock，扫描器与心跳持续来自父进程的启动时代码——文件变更只
重启子进程，不刷新父进程调度器。生产 gunicorn 无此问题（worker 滚动重启即
换锁持有者）。

纪律：心跳写是独立小事务，自身 try/except 包裹——失败只记日志，【绝不】
影响业务投递；异常路径写失败心跳前必须先 rollback（防搭上已 abort 的事务）。
"""
import hashlib
import json
import os
import uuid
from datetime import datetime

from exts import db
from models import ScheduleServiceRuntime

SERVICE_KEY_REMINDER_SCAN = 'reminder_scan'

_INSTANCE_ID = None


def instance_id():
    """进程内懒生成（非锁持有进程也会调用本模块——只有锁持有者真正写行）。"""
    global _INSTANCE_ID
    if _INSTANCE_ID is None:
        _INSTANCE_ID = uuid.uuid4().hex
    return _INSTANCE_ID


def config_fingerprint(enabled, interval):
    """非敏感白名单值指纹（配置不一致比对用，不含任何密钥/路径）。"""
    return hashlib.sha256(
        f"enabled={int(bool(enabled))};interval={int(interval)}".encode()).hexdigest()


def _upsert_row(enabled, interval):
    row = ScheduleServiceRuntime.query.filter_by(
        service_key=SERVICE_KEY_REMINDER_SCAN, instance_id=instance_id()).first()
    if row is None:
        row = ScheduleServiceRuntime(
            service_key=SERVICE_KEY_REMINDER_SCAN, instance_id=instance_id(),
            environment=os.getenv('BME_ENV', 'default')[:30],
            deployment_version=(os.getenv('BME_DEPLOYMENT_VERSION') or None),
            enabled_snapshot=bool(enabled), interval_seconds=int(interval),
            config_fingerprint=config_fingerprint(enabled, interval))
        db.session.add(row)
    row.enabled_snapshot = bool(enabled)
    row.interval_seconds = int(interval)
    row.config_fingerprint = config_fingerprint(enabled, interval)
    return row


def record_scan_start(app, *, enabled, interval):
    """扫描开始心跳：last_started_at + outcome='running'。"""
    try:
        row = _upsert_row(enabled, interval)
        now = datetime.now()
        row.last_started_at = now
        row.last_outcome = 'running'
        row.updated_at = now
        db.session.commit()
    except Exception:
        db.session.rollback()
        app.logger.warning("[schedule_observability] 开始心跳写入失败", exc_info=True)


def record_scan_result(app, *, outcome, enabled, interval, stats=None, error_code=None):
    """扫描结束心跳：success（含零条扫描）/ failed。"""
    try:
        row = _upsert_row(enabled, interval)
        now = datetime.now()
        row.last_finished_at = now
        row.last_outcome = outcome
        row.safe_error_code = error_code
        if outcome == 'success':
            row.last_success_at = now
            row.last_batch_counts = json.dumps(stats or {}, ensure_ascii=False)
        row.updated_at = now
        db.session.commit()
    except Exception:
        db.session.rollback()
        app.logger.warning("[schedule_observability] 结果心跳写入失败", exc_info=True)


def _classify_exception(exc):
    """异常类名 → 安全错误码白名单（不存原文）。"""
    name = type(exc).__name__.lower()
    if 'timeout' in name:
        return 'timeout'
    if any(k in name for k in ('operationalerror', 'programmingerror',
                               'integrityerror', 'dataerror', 'disconnection')):
        return 'db_error'
    return 'internal'


def run_reminder_scan_with_heartbeat(app):
    """schedule_scheduler job 入口：开始心跳 → 扫描 → 结果心跳。
    心跳失败不影响扫描；扫描异常照旧由本函数记录（job 层不再包一层）。"""
    with app.app_context():
        from exts import db as _db
        enabled = bool(app.config.get('SCHEDULE_REMINDER_SCAN_ENABLED', True))
        interval = max(5, int(app.config.get('SCHEDULE_REMINDER_SCAN_INTERVAL_SECONDS', 30)))
        record_scan_start(app, enabled=enabled, interval=interval)
        try:
            from services.schedule.reminders import scan_due_reminders
            stats = scan_due_reminders()
            record_scan_result(app, outcome='success', enabled=enabled,
                               interval=interval, stats=stats)
            if any(stats.values()):
                app.logger.info(f"[schedule_scheduler] 扫描投递：{stats}")
        except Exception as e:
            app.logger.exception(f"[schedule_scheduler] 提醒扫描失败: {e}")
            _db.session.rollback()          # 先回滚已 abort 的事务再写失败心跳
            record_scan_result(app, outcome='failed', enabled=enabled,
                               interval=interval, error_code=_classify_exception(e))

"""迁移 56：日程服务管理面板批次 A——运行心跳表 + 提醒原因码 + 录入观测列（2026-09-24，幂等）。

背景：docs/计划/管理端日程服务面板-开发计划-2026-09-24.md 批次 A（只读观测）。
1) schedule_service_runtime：提醒扫描器心跳（每锁持有进程一行；fcntl 锁保证
   单写者，「最新行」即活实例，旧行为接管残留不算失联）；
2) schedule_reminder 加 status_reason_code（expired 原因安全枚举）与
   last_error_code（管理端只出码不出 last_error 原文）；
3) schedule_capture 加 started_at/finished_at/elapsed_ms 与三计数
   （旧记录 NULL=未采集，不回填伪数据）；
4) ix_schedule_capture_status_created 支撑管理列表按状态+时间的扫描。
   reminder 侧 (status, trigger_at) 已有 ix_schedule_reminder_scan，不重复建。

空库守卫：schedule_reminder/schedule_capture 表不存在时跳过对应 ALTER
（全新库由 db.create_all() 按新模型建全列）。

用法：python scripts/migrate/migrate_56_schedule_admin.py
回滚（批次 A 回退=关闭管理入口，表列暂保留；确需回滚）：
  DROP TABLE schedule_service_runtime;
  ALTER TABLE schedule_reminder DROP COLUMN status_reason_code, DROP COLUMN last_error_code;
  ALTER TABLE schedule_capture DROP COLUMN started_at, DROP COLUMN finished_at,
    DROP COLUMN elapsed_ms, DROP COLUMN created_count, DROP COLUMN clarify_count, DROP COLUMN failed_count;
  DROP INDEX ix_schedule_capture_status_created ON schedule_capture;
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

import config  # noqa: E402
from sqlalchemy import create_engine, text, inspect  # noqa: E402

TABLES = {
    'schedule_service_runtime': """
        CREATE TABLE schedule_service_runtime (
            id INT AUTO_INCREMENT PRIMARY KEY,
            environment VARCHAR(30) NOT NULL DEFAULT 'default' COMMENT 'BME_ENV 或 default，仅展示',
            service_key VARCHAR(30) NOT NULL COMMENT '首批仅 reminder_scan',
            instance_id VARCHAR(36) NOT NULL COMMENT '锁持有进程 uuid4.hex',
            deployment_version VARCHAR(64) NULL COMMENT 'BME_DEPLOYMENT_VERSION，可空不参与推导',
            enabled_snapshot TINYINT(1) NOT NULL DEFAULT 1 COMMENT '心跳时开关快照',
            interval_seconds INT NOT NULL DEFAULT 30,
            config_fingerprint VARCHAR(64) NULL COMMENT '非敏感白名单值 sha256',
            last_started_at DATETIME NULL,
            last_finished_at DATETIME NULL,
            last_success_at DATETIME NULL COMMENT '成功完成即刷新（含零条扫描）',
            last_outcome VARCHAR(20) NULL COMMENT 'running/success/failed',
            safe_error_code VARCHAR(30) NULL COMMENT 'db_error/timeout/internal',
            last_batch_counts TEXT NULL COMMENT '扫描计数 JSON',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY uq_schedule_service_instance (service_key, instance_id),
            INDEX ix_schedule_service_latest (service_key, updated_at))
        CHARSET=utf8mb4
    """,
}

COLUMNS = {
    'schedule_reminder': [
        ("status_reason_code",
         "ALTER TABLE schedule_reminder ADD COLUMN status_reason_code VARCHAR(30) NULL "
         "COMMENT 'target_gone/stale_version/target_inactive/no_deadline/missed' AFTER status"),
        ("last_error_code",
         "ALTER TABLE schedule_reminder ADD COLUMN last_error_code VARCHAR(30) NULL "
         "COMMENT 'notification_write_failed，重试成功后清空' AFTER last_error"),
    ],
    'schedule_capture': [
        ("started_at", "ALTER TABLE schedule_capture ADD COLUMN started_at DATETIME NULL COMMENT 'worker claim 时刻'"),
        ("finished_at", "ALTER TABLE schedule_capture ADD COLUMN finished_at DATETIME NULL COMMENT '首个终态时刻（不含补答）'"),
        ("elapsed_ms", "ALTER TABLE schedule_capture ADD COLUMN elapsed_ms INT NULL COMMENT '异步处理墙钟毫秒，旧行未采集'"),
        ("created_count", "ALTER TABLE schedule_capture ADD COLUMN created_count INT NULL COMMENT '已落库事项数'"),
        ("clarify_count", "ALTER TABLE schedule_capture ADD COLUMN clarify_count INT NULL COMMENT '待补充事项数'"),
        ("failed_count", "ALTER TABLE schedule_capture ADD COLUMN failed_count INT NULL COMMENT '失败事项数'"),
    ],
}

INDEXES = [
    ('schedule_capture', 'ix_schedule_capture_status_created',
     "CREATE INDEX ix_schedule_capture_status_created ON schedule_capture (status, created_at)"),
]

engine = create_engine(config.SQLALCHEMY_DATABASE_URI)
insp = inspect(engine)

with engine.connect() as conn:
    for table, ddl in TABLES.items():
        if insp.has_table(table):
            print(f"[=] 表 {table} 已存在")
            continue
        conn.execute(text(ddl))
        conn.commit()
        print(f"[+] 表 {table} 已创建")

    for table, cols in COLUMNS.items():
        if not insp.has_table(table):
            print(f"[skip] 表 {table} 不存在（空库由 create_all 建全列）")
            continue
        existing = {c['name'] for c in insp.get_columns(table)}
        for name, ddl in cols:
            if name in existing:
                print(f"[=] {table}.{name} 已存在")
                continue
            conn.execute(text(ddl))
            conn.commit()
            print(f"[+] {table}.{name} 已添加")

    for table, index_name, ddl in INDEXES:
        if not insp.has_table(table):
            print(f"[skip] 索引 {index_name}：表 {table} 不存在")
            continue
        existing_idx = {ix['name'] for ix in insp.get_indexes(table)}
        if index_name in existing_idx:
            print(f"[=] 索引 {index_name} 已存在")
            continue
        conn.execute(text(ddl))
        conn.commit()
        print(f"[+] 索引 {index_name} 已创建")
    print("[done] migrate_56 完成")

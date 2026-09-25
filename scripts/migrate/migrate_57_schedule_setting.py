"""迁移 57：日程服务在线配置表 schedule_setting（2026-09-25，幂等）。

背景：管理端面板批次 B3（有限在线配置，开发计划 §9.2）。仅三个白名单低
耦合参数可在管理端发布覆盖值（DB > env/默认）；模型、扫描间隔等不进本表。

用法：python scripts/migrate/migrate_57_schedule_setting.py
回滚：DROP TABLE schedule_setting;（覆盖值丢失即回落默认，无业务数据）
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

import config  # noqa: E402
from sqlalchemy import create_engine, text, inspect  # noqa: E402

TABLES = {
    'schedule_setting': """
        CREATE TABLE schedule_setting (
            id INT AUTO_INCREMENT PRIMARY KEY,
            `key` VARCHAR(60) NOT NULL COMMENT '白名单配置键',
            value VARCHAR(200) NULL COMMENT '平台覆盖值；NULL=跟随默认',
            version INT NOT NULL DEFAULT 1 COMMENT '乐观锁',
            previous_value VARCHAR(200) NULL COMMENT '上一版值（审计）',
            reason VARCHAR(200) NULL COMMENT '变更原因',
            updated_by INT NULL COMMENT '发布人 user.id',
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uq_schedule_setting_key (`key`),
            CONSTRAINT fk_schedule_setting_user FOREIGN KEY (updated_by) REFERENCES user(id))
        CHARSET=utf8mb4
    """,
}

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
    print("[done] migrate_57 完成")

"""迁移 46：承诺出勤日变更账本（2026-09-20，幂等）。

《通知方案》§3.2/§6.8：学员手选承诺日被本人之外的人修改时须立即通知并保留记录，
避免争议。管理员/负责人对某学员承诺日的显式调整（全量替换）落一行只追加账本：
before/after 记日期集合快照，operator+reason 留痕。plan 行本身的 source 已由
migrate_41 提供（self_selected/admin/legacy）。

用法：python scripts/migrate/migrate_46_attendance_change.py
回滚：DROP TABLE camp_attendance_change;
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

import config  # noqa: E402
from sqlalchemy import create_engine, text, inspect  # noqa: E402

TABLES = {
    'camp_attendance_change': """
        CREATE TABLE camp_attendance_change (
            id INT AUTO_INCREMENT PRIMARY KEY,
            camp_session_id INT NOT NULL COMMENT '营期',
            user_id INT NOT NULL COMMENT '学员',
            before_dates JSON NULL COMMENT '变更前日期集合 ["2026-10-01",...]',
            after_dates JSON NULL COMMENT '变更后日期集合',
            operator_id INT NOT NULL COMMENT '操作人（学员本人调整不落此表）',
            reason VARCHAR(500) NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            INDEX ix_cac_session_user (camp_session_id, user_id),
            CONSTRAINT fk_cac_session FOREIGN KEY (camp_session_id) REFERENCES camp_session(id),
            CONSTRAINT fk_cac_user FOREIGN KEY (user_id) REFERENCES user(id),
            CONSTRAINT fk_cac_operator FOREIGN KEY (operator_id) REFERENCES user(id)
        ) CHARSET=utf8mb4
    """,
}

engine = create_engine(config.SQLALCHEMY_DATABASE_URI)
insp = inspect(engine)

with engine.connect() as conn:
    for table, ddl in TABLES.items():
        if insp.has_table(table):
            print(f"[=] {table} 已存在")
            continue
        conn.execute(text(ddl))
        conn.commit()
        print(f"[+] {table} 已创建")
    print("[done] migrate_46 完成")

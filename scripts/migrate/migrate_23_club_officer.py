"""迁移 23：社团干事任职表（功能扩展轮 §四，幂等）。

club_officer 单表。轻量身份档案，不挂任何权限；卸任=状态化 ended 不删行。
约束在应用层（blueprints/officers.py R1-R4），表级仅常规索引。

用法：python scripts/migrate/migrate_23_club_officer.py
回滚：DROP TABLE club_officer
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

import config  # noqa: E402
from sqlalchemy import create_engine, text, inspect  # noqa: E402

TABLES = {
    'club_officer': """
        CREATE TABLE club_officer (
            id INT AUTO_INCREMENT PRIMARY KEY,
            user_id INT NOT NULL COMMENT '任职社员',
            title VARCHAR(20) NOT NULL COMMENT '职位白名单：社长/副社长/团支书/副团支书/组长',
            department VARCHAR(50) NULL COMMENT '归属组名（组织树叶子，组名全树唯一）；社长为空',
            term_start DATE NOT NULL COMMENT '任期起',
            term_end DATE NULL COMMENT '任期止（空=在任）',
            status VARCHAR(20) NOT NULL DEFAULT 'active' COMMENT 'active/ended（卸任状态化不删行）',
            appointed_by INT NULL COMMENT '任命操作人（审计留痕）',
            ended_by INT NULL COMMENT '卸任操作人',
            end_reason VARCHAR(200) NULL COMMENT '卸任原因（选填）',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            INDEX ix_club_officer_user (user_id),
            INDEX ix_club_officer_status (status),
            CONSTRAINT fk_club_officer_user FOREIGN KEY (user_id) REFERENCES user(id)
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
    print("[done] migrate_23 完成")

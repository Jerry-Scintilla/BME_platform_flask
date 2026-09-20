"""迁移 45：营期成员软删除 + 成员变更账本（2026-09-20，幂等）。

《通知方案》§3.3：移除成员不再物理删除——「谁曾经参加、何时退出、谁操作」可追溯。
- camp_member + status('active'/'removed'/'exited') / ended_at / ended_by / end_reason，
  存量行回填 active；+ INDEX(camp_session_id, status)
- camp_member_event 只追加账本：assign/approve_join/reactivate/reassign/remove/exit，
  before/after 记 JSON 快照，operator_id + reason 留痕

查询侧由 models.py 的 do_orm_execute 全局默认过滤兜底（默认只见 active，
member_history_scope 逃生口供复职判定与历史视图），无需逐点改 77 处查询。

用法：python scripts/migrate/migrate_45_member_soft_delete.py
回滚：ALTER TABLE camp_member DROP COLUMN status, DROP COLUMN ended_at,
      DROP COLUMN ended_by, DROP COLUMN end_reason; DROP TABLE camp_member_event;
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

import config  # noqa: E402
from sqlalchemy import create_engine, text, inspect  # noqa: E402

TABLES = {
    'camp_member_event': """
        CREATE TABLE camp_member_event (
            id INT AUTO_INCREMENT PRIMARY KEY,
            camp_session_id INT NOT NULL COMMENT '营期',
            user_id INT NOT NULL COMMENT '成员',
            action VARCHAR(20) NOT NULL COMMENT 'assign/approve_join/reactivate/reassign/remove/exit',
            before JSON NULL COMMENT '变更前 {role,status,team_mentor_id}',
            after JSON NULL COMMENT '变更后',
            operator_id INT NOT NULL COMMENT '操作人',
            reason VARCHAR(500) NULL,
            occurred_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            INDEX ix_cme_session (camp_session_id),
            INDEX ix_cme_user (user_id),
            CONSTRAINT fk_cme_session FOREIGN KEY (camp_session_id) REFERENCES camp_session(id),
            CONSTRAINT fk_cme_user FOREIGN KEY (user_id) REFERENCES user(id),
            CONSTRAINT fk_cme_operator FOREIGN KEY (operator_id) REFERENCES user(id)
        ) CHARSET=utf8mb4
    """,
}

COLUMNS = {
    'camp_member': [
        ("status", "ALTER TABLE camp_member ADD COLUMN status VARCHAR(20) NOT NULL "
         "DEFAULT 'active' COMMENT 'active / removed / exited（软删除，2026-09-20）'"),
        ("ended_at", "ALTER TABLE camp_member ADD COLUMN ended_at DATETIME NULL"),
        ("ended_by", "ALTER TABLE camp_member ADD COLUMN ended_by INT NULL"),
        ("end_reason", "ALTER TABLE camp_member ADD COLUMN end_reason VARCHAR(500) NULL"),
    ],
}

INDEXES = [
    ("ix_camp_member_session_status",
     "ALTER TABLE camp_member ADD INDEX ix_camp_member_session_status (camp_session_id, status)"),
]

engine = create_engine(config.SQLALCHEMY_DATABASE_URI)
insp = inspect(engine)

with engine.connect() as conn:
    for table, cols in COLUMNS.items():
        if not insp.has_table(table):
            print(f"[!] 表 {table} 不存在，跳过（新库由 create_all 建全量列）")
            continue
        existing = {c['name'] for c in insp.get_columns(table)}
        for name, ddl in cols:
            if name in existing:
                print(f"[=] {table}.{name} 已存在")
                continue
            conn.execute(text(ddl))
            conn.commit()
            print(f"[+] {table}.{name} 已添加")
    existing_idx = {ix['name'] for ix in insp.get_indexes('camp_member')}
    for name, ddl in INDEXES:
        if name in existing_idx:
            print(f"[=] 索引 {name} 已存在")
            continue
        conn.execute(text(ddl))
        conn.commit()
        print(f"[+] 索引 {name} 已创建")
    for table, ddl in TABLES.items():
        if insp.has_table(table):
            print(f"[=] {table} 已存在")
            continue
        conn.execute(text(ddl))
        conn.commit()
        print(f"[+] {table} 已创建")
    print("[done] migrate_45 完成")

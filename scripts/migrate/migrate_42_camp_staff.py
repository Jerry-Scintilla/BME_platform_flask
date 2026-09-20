"""迁移 42：营期工作人员（CampStaff）+ 职责变更事件（2026-09-20，幂等）。

《培训营老师身份与营期负责人架构设计方案》阶段 1 身份地基：
- camp_staff：owner=主负责人 / teacher=协同老师；status=active|ended（不物理删除，
  保留审计链）；UNIQUE(camp_session_id, user_id)。
- camp_staff_event：只追加的职责变更事件（assign/promote/demote/transfer/end）。
不回填：不把所有 super_admin 自动填成每个营的老师（方案 §11.1——会把「超管兜底」
永久固化）；由管理端逐营委任真实负责人，委任前该营通知仍走 super_admin 兜底。

用法：python scripts/migrate/migrate_42_camp_staff.py
回滚：DROP TABLE camp_staff_event; DROP TABLE camp_staff;
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

import config  # noqa: E402
from sqlalchemy import create_engine, text, inspect  # noqa: E402

TABLES = {
    'camp_staff': """
        CREATE TABLE camp_staff (
            id INT AUTO_INCREMENT PRIMARY KEY,
            camp_session_id INT NOT NULL COMMENT '营期',
            user_id INT NOT NULL COMMENT '工作人员',
            role VARCHAR(20) NOT NULL DEFAULT 'teacher' COMMENT 'owner=主负责人 / teacher=协同老师',
            status VARCHAR(20) NOT NULL DEFAULT 'active' COMMENT 'active / ended（不物理删除）',
            assigned_by INT NOT NULL COMMENT '委任人',
            assigned_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            ended_by INT NULL COMMENT '解除人',
            ended_at DATETIME NULL,
            end_reason VARCHAR(500) NULL COMMENT '解除/转交原因',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            INDEX ix_camp_staff_session (camp_session_id),
            INDEX ix_camp_staff_user (user_id),
            INDEX ix_camp_staff_session_status_role (camp_session_id, status, role),
            UNIQUE KEY uq_camp_staff_camp_user (camp_session_id, user_id),
            CONSTRAINT fk_camp_staff_session FOREIGN KEY (camp_session_id) REFERENCES camp_session(id),
            CONSTRAINT fk_camp_staff_user FOREIGN KEY (user_id) REFERENCES user(id),
            CONSTRAINT fk_camp_staff_assigned_by FOREIGN KEY (assigned_by) REFERENCES user(id),
            CONSTRAINT fk_camp_staff_ended_by FOREIGN KEY (ended_by) REFERENCES user(id)
        ) CHARSET=utf8mb4
    """,
    'camp_staff_event': """
        CREATE TABLE camp_staff_event (
            id INT AUTO_INCREMENT PRIMARY KEY,
            camp_session_id INT NOT NULL COMMENT '营期',
            user_id INT NOT NULL COMMENT '被操作的工作人员',
            action VARCHAR(20) NOT NULL COMMENT 'assign / promote / demote / transfer / end',
            before_role VARCHAR(20) NULL,
            after_role VARCHAR(20) NULL,
            operator_id INT NOT NULL COMMENT '操作人',
            reason VARCHAR(500) NULL,
            occurred_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            INDEX ix_camp_staff_event_session (camp_session_id),
            INDEX ix_camp_staff_event_user (user_id),
            CONSTRAINT fk_cse_session FOREIGN KEY (camp_session_id) REFERENCES camp_session(id),
            CONSTRAINT fk_cse_user FOREIGN KEY (user_id) REFERENCES user(id),
            CONSTRAINT fk_cse_operator FOREIGN KEY (operator_id) REFERENCES user(id)
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
    print("[done] migrate_42 完成")

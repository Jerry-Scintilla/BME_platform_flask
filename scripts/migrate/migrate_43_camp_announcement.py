"""迁移 43：营期公告表（2026-09-20，幂等）。

《培训营通知系统与业务闭环重构设计方案》§7.3：公告是营期内需持续展示的公共内容
（可置顶/过期/按受众发布），与一次性送达的通知不共表；发布时可选择同时向受众
扇出一条通知。audience 为应用层枚举（all/mentors/students，staff 恒可见）。

用法：python scripts/migrate/migrate_43_camp_announcement.py
回滚：DROP TABLE camp_announcement;
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

import config  # noqa: E402
from sqlalchemy import create_engine, text, inspect  # noqa: E402

TABLES = {
    'camp_announcement': """
        CREATE TABLE camp_announcement (
            id INT AUTO_INCREMENT PRIMARY KEY,
            camp_session_id INT NOT NULL COMMENT '营期',
            title VARCHAR(200) NOT NULL COMMENT '标题',
            content TEXT NOT NULL COMMENT '正文',
            audience VARCHAR(20) NOT NULL DEFAULT 'all' COMMENT '受众：all/mentors/students（staff 恒可见）',
            is_pinned TINYINT(1) NOT NULL DEFAULT 0 COMMENT '置顶',
            status VARCHAR(20) NOT NULL DEFAULT 'active' COMMENT 'active / ended（撤下不物理删除）',
            published_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            expires_at DATETIME NULL COMMENT '过期不再展示',
            created_by INT NOT NULL COMMENT '发布人',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            INDEX ix_camp_ann_session (camp_session_id),
            CONSTRAINT fk_camp_ann_session FOREIGN KEY (camp_session_id) REFERENCES camp_session(id),
            CONSTRAINT fk_camp_ann_creator FOREIGN KEY (created_by) REFERENCES user(id)
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
    print("[done] migrate_43 完成")

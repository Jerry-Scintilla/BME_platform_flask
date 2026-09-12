"""迁移 22：项目广场表组（功能扩展轮 §五 MVP，幂等）。

showcase_project / showcase_favorite。评论复用 discussion 基建（scope_type 加 'project'，
代码侧白名单扩展，无表变更）。

用法：python scripts/migrate/migrate_22_showcase.py
回滚：DROP 两表
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

import config  # noqa: E402
from sqlalchemy import create_engine, text, inspect  # noqa: E402

TABLES = {
    'showcase_project': """
        CREATE TABLE showcase_project (
            id INT AUTO_INCREMENT PRIMARY KEY,
            source VARCHAR(20) NOT NULL COMMENT 'camp=营期项目发布投影 / community=自由分享',
            source_ref INT NULL COMMENT 'camp→camp_unit.id；community 空',
            owner_user_id INT NOT NULL COMMENT '发布人（camp=负责人）/ 创建人（community）',
            title VARCHAR(120) NOT NULL,
            summary VARCHAR(300) NULL COMMENT '列表页简介',
            description TEXT NULL COMMENT '详情正文',
            cover VARCHAR(255) NULL,
            tags TEXT NULL COMMENT 'JSON 字符串数组',
            project_status VARCHAR(20) NOT NULL DEFAULT 'ongoing' COMMENT 'idea/ongoing/done（camp 随结营自动 done）',
            status VARCHAR(20) NOT NULL DEFAULT 'visible' COMMENT 'visible/hidden（治理）',
            members_json TEXT NULL COMMENT 'JSON 展示成员（community 可选公开）',
            links_json TEXT NULL COMMENT 'JSON 资料区链接 [{label,url}]',
            archive_ref INT NULL COMMENT 'camp：结营档案 id（引用不复制）',
            view_count INT NOT NULL DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            INDEX ix_showcase_source_ref (source_ref),
            INDEX ix_showcase_owner (owner_user_id),
            UNIQUE KEY uq_showcase_source_ref (source, source_ref),
            CONSTRAINT fk_showcase_owner FOREIGN KEY (owner_user_id) REFERENCES user(id)
        ) CHARSET=utf8mb4
    """,
    'showcase_favorite': """
        CREATE TABLE showcase_favorite (
            id INT AUTO_INCREMENT PRIMARY KEY,
            user_id INT NOT NULL,
            project_id INT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            INDEX ix_showcase_fav_user (user_id),
            INDEX ix_showcase_fav_project (project_id),
            UNIQUE KEY uq_showcase_fav (user_id, project_id),
            CONSTRAINT fk_showcase_fav_user FOREIGN KEY (user_id) REFERENCES user(id),
            CONSTRAINT fk_showcase_fav_project FOREIGN KEY (project_id) REFERENCES showcase_project(id)
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
    print("[done] migrate_22 完成")

"""迁移 34：营期·组会留档两表（2026-09-17，幂等）。

camp_meeting + camp_meeting_attachment：培训组（导生组）与项目组通用组会纪要，
组长/负责人提交（文字+文件附件+视频），组员可查看下载；结营整体只读。
双作用域：scope='team'（培训组，锚点 mentor_id）/ scope='unit'（项目组，挂 unit_id）。
附件本体走 storage 层（STORAGE_BACKEND=minio|local），object_key 规则
camp/{sid}/meeting/{meeting_id}/{uuid}{ext}；下载走鉴权代理端点并支持 HTTP Range。

用法：python scripts/migrate/migrate_34_camp_meeting.py
回滚：DROP TABLE camp_meeting_attachment; DROP TABLE camp_meeting;
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

import config  # noqa: E402
from sqlalchemy import create_engine, text, inspect  # noqa: E402

TABLES = {
    'camp_meeting': """
        CREATE TABLE camp_meeting (
            id INT AUTO_INCREMENT PRIMARY KEY,
            camp_session_id INT NOT NULL COMMENT '营期',
            scope VARCHAR(10) NOT NULL COMMENT '组作用域：team=培训组（导生组）/ unit=项目组',
            unit_id INT NULL COMMENT 'scope=unit 时的项目单元',
            mentor_id INT NULL COMMENT 'scope=team 时的组长（导生）',
            title VARCHAR(200) NOT NULL COMMENT '会议主题',
            meeting_date DATE NOT NULL COMMENT '会议日期',
            content TEXT COMMENT '文字纪要（可空=纯附件/视频）',
            created_by INT NOT NULL COMMENT '创建人',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            INDEX ix_cmtg_camp_scope (camp_session_id, scope),
            INDEX ix_cmtg_unit (unit_id),
            INDEX ix_cmtg_mentor (camp_session_id, mentor_id),
            CONSTRAINT fk_cmtg_camp FOREIGN KEY (camp_session_id) REFERENCES camp_session(id),
            CONSTRAINT fk_cmtg_unit FOREIGN KEY (unit_id) REFERENCES camp_unit(id),
            CONSTRAINT fk_cmtg_mentor FOREIGN KEY (mentor_id) REFERENCES user(id),
            CONSTRAINT fk_cmtg_creator FOREIGN KEY (created_by) REFERENCES user(id)
        ) CHARSET=utf8mb4
    """,
    'camp_meeting_attachment': """
        CREATE TABLE camp_meeting_attachment (
            id INT AUTO_INCREMENT PRIMARY KEY,
            meeting_id INT NOT NULL COMMENT '所属组会',
            object_key VARCHAR(255) NOT NULL,
            filename VARCHAR(200) NOT NULL,
            size INT,
            content_type VARCHAR(100),
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            INDEX ix_cmtga_meeting (meeting_id),
            CONSTRAINT fk_cmtga_meeting FOREIGN KEY (meeting_id)
                REFERENCES camp_meeting(id) ON DELETE CASCADE
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
    print("[done] migrate_34 完成")

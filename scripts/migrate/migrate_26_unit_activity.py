"""迁移 26：项目营组长活动考勤两表（2026-09-13，幂等）。

camp_unit_activity（负责人发起 会议/外出调研/其他）
camp_unit_activity_check（按活动勾选出席，UQ activity+user，删除活动级联清勾选）

用法：python scripts/migrate/migrate_26_unit_activity.py
回滚：DROP TABLE camp_unit_activity_check, camp_unit_activity
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

import config  # noqa: E402
from sqlalchemy import create_engine, text, inspect  # noqa: E402

DDL = """
    CREATE TABLE camp_unit_activity (
        id INT NOT NULL AUTO_INCREMENT,
        unit_id INT NOT NULL,
        type VARCHAR(20) NOT NULL DEFAULT 'meeting',
        title VARCHAR(100) NOT NULL,
        happens_on DATE NOT NULL,
        note VARCHAR(500),
        created_by INT,
        created_at DATETIME,
        PRIMARY KEY (id),
        UNIQUE KEY uq_unit_activity_title_date (unit_id, title, happens_on),
        KEY ix_camp_unit_activity_unit (unit_id),
        FOREIGN KEY (unit_id) REFERENCES camp_unit (id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
      COMMENT='项目营组长活动（09-13：会议/调研等，出席另表勾选）'
"""

DDL_CHECK = """
    CREATE TABLE camp_unit_activity_check (
        id INT NOT NULL AUTO_INCREMENT,
        activity_id INT NOT NULL,
        user_id INT NOT NULL,
        present TINYINT(1) NOT NULL DEFAULT 0,
        marked_by INT,
        marked_at DATETIME,
        PRIMARY KEY (id),
        UNIQUE KEY uq_unit_activity_check_pair (activity_id, user_id),
        KEY ix_camp_unit_activity_check_activity (activity_id),
        FOREIGN KEY (activity_id) REFERENCES camp_unit_activity (id) ON DELETE CASCADE,
        FOREIGN KEY (user_id) REFERENCES user (id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
      COMMENT='活动出席勾选（负责人记录，PUT 全量替换）'
"""

engine = create_engine(config.SQLALCHEMY_DATABASE_URI)
insp = inspect(engine)

with engine.connect() as conn:
    tables = insp.get_table_names()
    if 'camp_unit_activity' in tables:
        print("[=] camp_unit_activity 已存在")
    else:
        conn.execute(text(DDL))
        conn.commit()
        print("[+] camp_unit_activity 已创建")
    tables = insp.get_table_names()
    if 'camp_unit_activity_check' in tables:
        print("[=] camp_unit_activity_check 已存在")
    else:
        conn.execute(text(DDL_CHECK))
        conn.commit()
        print("[+] camp_unit_activity_check 已创建")
    print("[done] migrate_26 完成")

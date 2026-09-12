"""迁移 25：考勤模式列（2026-09-12 三模式拍板，幂等）。

camp_policy 加 attendance_mode（daily/weekly，默认 daily）。
模式 B（学期远程·不考勤）不占此列——由 capabilities.attendance=false 承载。

用法：python scripts/migrate/migrate_25_attendance_mode.py
回滚：ALTER TABLE camp_policy DROP COLUMN attendance_mode
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

import config  # noqa: E402
from sqlalchemy import create_engine, text, inspect  # noqa: E402

DDL = """
    ALTER TABLE camp_policy
        ADD COLUMN attendance_mode VARCHAR(20) NOT NULL DEFAULT 'daily'
        COMMENT '考勤模式：daily=假期营每日承诺出勤；weekly=学期校区按周累计（B 模式不考勤走 capabilities.attendance=false）'
"""

engine = create_engine(config.SQLALCHEMY_DATABASE_URI)
insp = inspect(engine)

with engine.connect() as conn:
    cols = [c['name'] for c in insp.get_columns('camp_policy')]
    if 'attendance_mode' in cols:
        print("[=] camp_policy.attendance_mode 已存在")
    else:
        conn.execute(text(DDL))
        conn.commit()
        print("[+] camp_policy.attendance_mode 已添加（默认 daily）")
    print("[done] migrate_25 完成")

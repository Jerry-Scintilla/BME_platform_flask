"""迁移 02：建营期 schema（幂等）。

- db.create_all() 建 6 张新营期表（camp_session/camp_member/camp_course/
  camp_attendance_plan/camp_seat/camp_leave）
- 既有表加列（create_all 不改既有表，需手动 ALTER）：
  user_course.camp_session_id / medal_user.(camp_session_id, issued_by) /
  check_record.(camp_session_id, seat_id) / notification.camp_session_id

用法（在项目根目录）：python scripts/migrate/migrate_02_camp_schema.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from sqlalchemy import text, inspect  # noqa: E402
from app import app          # noqa: E402
from exts import db          # noqa: E402

NEW_COLS = {
    'user_course':  ['camp_session_id'],
    'medal_user':   ['camp_session_id', 'issued_by'],
    'check_record': ['camp_session_id', 'seat_id'],
    'notification': ['camp_session_id'],
}

with app.app_context():
    db.create_all()   # 建 6 张营期表（已存在则跳过）
    print("[+] create_all done（营期表已确保存在）")
    engine = db.engine
    insp = inspect(engine)
    with engine.connect() as conn:
        for tbl, cols in NEW_COLS.items():
            existing = {c['name'] for c in insp.get_columns(tbl)}
            for col in cols:
                if col not in existing:
                    conn.execute(text(f'ALTER TABLE `{tbl}` ADD COLUMN `{col}` INT NULL'))
                    print(f"  [+] {tbl}.{col}")
                else:
                    print(f"  [=] {tbl}.{col} 已存在")
        conn.commit()
    names = insp.get_table_names()
    camp_tables = [t for t in names if t.startswith('camp_')]
    print("[i] camp_ 表:", camp_tables)
    print("[done] migrate_02 完成")

"""迁移 04：CampJoinRequest +selected_days 列（存学员手选承诺出勤日 JSON 数组字符串）。幂等。

依赖 migrate_03 先建 camp_join_request 表。用法（项目根）：python scripts/migrate/migrate_04_join_selected_days.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from app import app          # noqa: E402
from exts import db          # noqa: E402
from sqlalchemy import text  # noqa: E402

with app.app_context():
    cols = [r[0] for r in db.session.execute(text("SHOW COLUMNS FROM camp_join_request")).fetchall()]
    if "selected_days" not in cols:
        db.session.execute(text("ALTER TABLE camp_join_request ADD COLUMN selected_days TEXT"))
        print("[+] added selected_days to camp_join_request")
    else:
        print("[=] selected_days already exists")
    db.session.commit()
    print("[done] migrate_04 完成")

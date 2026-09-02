"""迁移 14：导生报名审核制（幂等）。camp_join_request + apply_role(student/mentor)。
用法：python scripts/migrate/migrate_14_mentor_review.py"""
import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))
import config  # noqa: E402
from sqlalchemy import create_engine, text, inspect  # noqa: E402
engine = create_engine(config.SQLALCHEMY_DATABASE_URI)
insp = inspect(engine)
with engine.connect() as conn:
    cols = {c['name'] for c in insp.get_columns('camp_join_request')}
    if 'apply_role' not in cols:
        conn.execute(text("ALTER TABLE camp_join_request ADD COLUMN apply_role VARCHAR(20) DEFAULT 'student'"))
        conn.commit()
        print("[+] apply_role 已添加")
    else:
        print("[=] apply_role 已存在")
    print("[done] migrate_14 完成")

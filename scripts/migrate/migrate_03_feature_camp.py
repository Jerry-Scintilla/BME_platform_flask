"""迁移 03：营期主页指定 + 加入申请 schema（幂等）。

- camp_session 加 is_featured 列（ALTER，create_all 不管加列）
- 建新表 camp_join_request（create_all，已存在则跳过）

依赖 migrate_02 先建 camp_session。用法（项目根）：python scripts/migrate/migrate_03_feature_camp.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from app import app          # noqa: E402
from exts import db          # noqa: E402
from sqlalchemy import text  # noqa: E402

with app.app_context():
    cols = [r[0] for r in db.session.execute(text("SHOW COLUMNS FROM camp_session")).fetchall()]
    if "is_featured" not in cols:
        db.session.execute(text("ALTER TABLE camp_session ADD COLUMN is_featured TINYINT(1) DEFAULT 0"))
        print("[+] added is_featured to camp_session")
    else:
        print("[=] is_featured already exists")

    db.create_all()   # 建 camp_join_request（已存在则跳过）
    print("[+] create_all done (camp_join_request created if absent)")
    db.session.commit()
    print("[done] migrate_03 完成")

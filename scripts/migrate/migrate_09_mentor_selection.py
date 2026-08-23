"""迁移 09：营期·选导生（Mentor Selection）schema。

- camp_session 加 6 列（mentor_selection_enabled + 4 个时间点 + ms_tags）：
  create_all 不给已存在表补列（schema-drift 坑，报 1054 Unknown column），须手动 ALTER。
- 3 张新表（camp_mentor_profile / camp_mentor_preference / camp_mentor_match）由
  import app 时的 db.create_all() 自动建（幂等，checkfirst），脚本不重复建。
- 顺带建名片照片目录 ./data/mentor_photos/。

幂等：inspector 检查列存在再 ADD；makedirs exist_ok。可重复运行。

用法（项目根）：
python scripts/migrate/migrate_09_mentor_selection.py

回滚：
ALTER TABLE camp_session DROP COLUMN mentor_selection_enabled, DROP COLUMN ms_preference_start,
 DROP COLUMN ms_preference_deadline, DROP COLUMN ms_round1_deadline, DROP COLUMN ms_round2_deadline,
 DROP COLUMN ms_tags;
DROP TABLE camp_mentor_match; DROP TABLE camp_mentor_preference; DROP TABLE camp_mentor_profile;
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from app import app          # noqa: E402  (import 副作用：create_all 建 3 张新表)
from exts import db          # noqa: E402
from sqlalchemy import inspect, text          # noqa: E402

CAMP_SESSION_COLUMNS = [
    # (列名, DDL 片段)
    ("mentor_selection_enabled", "TINYINT(1) NOT NULL DEFAULT 0"),
    ("ms_preference_start", "DATETIME NULL"),
    ("ms_preference_deadline", "DATETIME NULL"),
    ("ms_round1_deadline", "DATETIME NULL"),
    ("ms_round2_deadline", "DATETIME NULL"),
    ("ms_tags", "TEXT NULL"),
]

with app.app_context():
    inspector = inspect(db.engine)
    existing = {c['name'] for c in inspector.get_columns('camp_session')}

    stmts = []
    for col, ddl in CAMP_SESSION_COLUMNS:
        if col not in existing:
            stmts.append(f"ALTER TABLE camp_session ADD COLUMN {col} {ddl}")

    with db.engine.begin() as conn:
        for s in stmts:
            print(f"[+] {s}")
            conn.execute(text(s))

    # 新表在 import app 时已由 create_all 建好；此处确认存在（缺则显式补建）
    tables = inspect(db.engine).get_table_names()
    for t in ('camp_mentor_profile', 'camp_mentor_preference', 'camp_mentor_match'):
        if t not in tables:
            db.create_all()   # checkfirst：只补缺的
            print(f"[+] create_all 补建表 {t}")
            break

    # 名片照片目录
    os.makedirs(os.path.join('.', 'data', 'mentor_photos'), exist_ok=True)

    print("[done] migrate_09 完成"
          + (f"（本次加列 {len(stmts)} 个）" if stmts else "（列均已存在，无操作）"))

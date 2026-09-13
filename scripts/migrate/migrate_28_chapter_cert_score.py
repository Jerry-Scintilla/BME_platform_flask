"""迁移 28：章节认证评分列 + 方向多课程（2026-09-13，幂等）。

1) camp_chapter_certification 加 score INT NULL（按章打分制 0-100；课程均分读时聚合不落库）。
2) 方向多课程（ms_tags JSON 由 {name, course_id} 升级为 {name, course_ids: []}）零迁移——
   JSON 列读时归一，存量单课形状由 _ms_directions 兼容，无需触碰数据。

用法：python scripts/migrate/migrate_28_chapter_cert_score.py
回滚：ALTER TABLE camp_chapter_certification DROP COLUMN score
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

import config  # noqa: E402
from sqlalchemy import create_engine, text, inspect  # noqa: E402

TABLE = 'camp_chapter_certification'
COLUMN = 'score'

engine = create_engine(config.SQLALCHEMY_DATABASE_URI)
insp = inspect(engine)

with engine.connect() as conn:
    cols = {c['name'] for c in insp.get_columns(TABLE)}
    if COLUMN in cols:
        print(f"[=] {TABLE}.{COLUMN} 已存在")
    else:
        conn.execute(text(
            f"ALTER TABLE {TABLE} ADD COLUMN {COLUMN} INT NULL "
            f"COMMENT '章节评分 0-100（按章打分制；可空=认证未打分）'"))
        conn.commit()
        print(f"[+] {TABLE}.{COLUMN} 已创建")
    print("[done] migrate_28 完成")

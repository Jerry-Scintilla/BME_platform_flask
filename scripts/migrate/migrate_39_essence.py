"""迁移 39：精华标记（2026-09-20，幂等，社区 Phase 3 质量分层）。

步骤：
1. discussion_thread 加 is_essence（精华帖，热度 ×2）
2. article_v2 加 is_essence（精华文章，热度 ×2）

用法（项目根）：python scripts/migrate/migrate_39_essence.py
回滚：ALTER TABLE discussion_thread DROP COLUMN is_essence;
      ALTER TABLE article_v2 DROP COLUMN is_essence;（升级前先 mysqldump）
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from app import app          # noqa: E402,F401  (import 即装配 config)
from sqlalchemy import create_engine, text, inspect  # noqa: E402

import config                # noqa: E402

engine = create_engine(config.SQLALCHEMY_DATABASE_URI)
insp = inspect(engine)


def _add_col(table, col, ddl):
    with engine.connect() as conn:
        cols = [c['name'] for c in insp.get_columns(table)]
        if col in cols:
            print(f"[=] {table}.{col} 已存在")
        else:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {ddl}"))
            conn.commit()
            print(f"[+] {table}.{col} 已加列")


if __name__ == '__main__':
    with app.app_context():
        _add_col('discussion_thread', 'is_essence', "is_essence TINYINT(1) NOT NULL DEFAULT 0")
        _add_col('article_v2', 'is_essence', "is_essence TINYINT(1) NOT NULL DEFAULT 0")
    print("migrate_39 完成")

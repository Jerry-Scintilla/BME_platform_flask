"""迁移 48：官方富文本推文（2026-09-20 官方富文本推文-调整方案.md，幂等）。

步骤：
1. article_v2 加 content_type（markdown/html，存量回填 'markdown'）
2. article_v2 加 content_html（MEDIUMTEXT，清洗后 HTML 正文，初始为空）
3. article_v2 加 content_version（HTML 清洗规范版本，初始 1）

用法（项目根）：python scripts/migrate/migrate_48_article_html.py
回滚：ALTER TABLE article_v2 DROP COLUMN content_type, DROP COLUMN content_html,
      DROP COLUMN content_version;（升级前先 mysqldump）
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
        _add_col('article_v2', 'content_type',
                 "content_type VARCHAR(16) NOT NULL DEFAULT 'markdown'")
        _add_col('article_v2', 'content_html', "content_html MEDIUMTEXT NULL")
        _add_col('article_v2', 'content_version',
                 "content_version INT NOT NULL DEFAULT 1")
        # 存量回填：迁移前建的行已由 DEFAULT 覆盖；双保险显式刷一遍（幂等）
        with engine.connect() as conn:
            n = conn.execute(text(
                "UPDATE article_v2 SET content_type = 'markdown' "
                "WHERE content_type IS NULL OR content_type = ''"
            )).rowcount
            conn.commit()
            if n:
                print(f"[~] 回填 content_type='markdown' {n} 行")
    print("migrate_48 完成")

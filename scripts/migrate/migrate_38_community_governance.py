"""迁移 38：社区治理 Phase 2（2026-09-20，幂等）。

步骤（每步独立幂等，可重复运行）：
1. discussion_thread 加 category（话题标签）/ project_id（关联 XLAB 项目）/ pinned_until（置顶过期）

用法（项目根）：python scripts/migrate/migrate_38_community_governance.py
回滚：ALTER TABLE discussion_thread DROP COLUMN category, DROP COLUMN project_id,
      DROP COLUMN pinned_until;（升级前先 mysqldump）
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from app import app          # noqa: E402,F401  (import 即装配 config)
from sqlalchemy import create_engine, text, inspect  # noqa: E402

import config                # noqa: E402

engine = create_engine(config.SQLALCHEMY_DATABASE_URI)
insp = inspect(engine)


def step_1_columns():
    with engine.connect() as conn:
        cols = [c['name'] for c in insp.get_columns('discussion_thread')]
        if 'category' not in cols:
            conn.execute(text("ALTER TABLE discussion_thread ADD COLUMN category VARCHAR(20) NULL"))
            conn.commit()
            print("[+] discussion_thread.category 已加列")
        else:
            print("[=] discussion_thread.category 已存在")
        if 'project_id' not in cols:
            conn.execute(text("ALTER TABLE discussion_thread ADD COLUMN project_id INT NULL"))
            conn.execute(text("CREATE INDEX ix_discussion_project ON discussion_thread (project_id)"))
            conn.commit()
            print("[+] discussion_thread.project_id 已加列（含索引）")
        else:
            print("[=] discussion_thread.project_id 已存在")
        if 'pinned_until' not in cols:
            conn.execute(text("ALTER TABLE discussion_thread ADD COLUMN pinned_until DATETIME NULL"))
            conn.commit()
            print("[+] discussion_thread.pinned_until 已加列")
        else:
            print("[=] discussion_thread.pinned_until 已存在")


if __name__ == '__main__':
    with app.app_context():
        step_1_columns()
    print("migrate_38 完成")

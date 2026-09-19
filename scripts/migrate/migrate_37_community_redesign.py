"""迁移 37：社区广场重设计（2026-09-19，幂等）。

步骤（每步独立幂等，可重复运行）：
1. article_v2 加 cover_image_key（封面 '/media/articles/...'）与 is_official（官方推文标记）
2. discussion_thread 加 images_json（帖子图集 '/media/discussions/...' ≤4）

用法（项目根）：python scripts/migrate/migrate_37_community_redesign.py
回滚：ALTER TABLE article_v2 DROP COLUMN cover_image_key, DROP COLUMN is_official;
      ALTER TABLE discussion_thread DROP COLUMN images_json;（升级前先 mysqldump）
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from app import app          # noqa: E402,F401  (import 即装配 config)
from sqlalchemy import create_engine, text, inspect  # noqa: E402

import config                # noqa: E402

engine = create_engine(config.SQLALCHEMY_DATABASE_URI)
insp = inspect(engine)


def step_1_article_v2():
    with engine.connect() as conn:
        cols = [c['name'] for c in insp.get_columns('article_v2')]
        if 'cover_image_key' not in cols:
            conn.execute(text("ALTER TABLE article_v2 ADD COLUMN cover_image_key VARCHAR(255) NULL"))
            conn.commit()
            print("[+] article_v2.cover_image_key 已加列")
        else:
            print("[=] article_v2.cover_image_key 已存在")
        if 'is_official' not in cols:
            conn.execute(text(
                "ALTER TABLE article_v2 ADD COLUMN is_official TINYINT(1) NOT NULL DEFAULT 0"))
            conn.commit()
            print("[+] article_v2.is_official 已加列")
        else:
            print("[=] article_v2.is_official 已存在")


def step_2_discussion_images():
    with engine.connect() as conn:
        cols = [c['name'] for c in insp.get_columns('discussion_thread')]
        if 'images_json' in cols:
            print("[=] discussion_thread.images_json 已存在")
        else:
            conn.execute(text("ALTER TABLE discussion_thread ADD COLUMN images_json TEXT NULL"))
            conn.commit()
            print("[+] discussion_thread.images_json 已加列")


if __name__ == '__main__':
    with app.app_context():
        step_1_article_v2()
        step_2_discussion_images()
    print("migrate_37 完成")

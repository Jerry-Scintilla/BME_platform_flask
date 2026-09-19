"""迁移 36：XLAB 项目广场媒体列（2026-09-19，幂等）。

步骤（每步独立幂等，可重复运行）：
1. showcase_project 表加 images_json 列（图集相对 URL JSON 数组，<=9 张）
   —— cover 列 migrate_22 建表时已有，无需处理。

用法（项目根）：python scripts/migrate/migrate_36_showcase_media.py
回滚：ALTER TABLE showcase_project DROP COLUMN images_json;（升级前先 mysqldump）
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
        cols = [c['name'] for c in insp.get_columns('showcase_project')]
        if 'images_json' in cols:
            print("[=] showcase_project.images_json 已存在")
        else:
            conn.execute(text("ALTER TABLE showcase_project ADD COLUMN images_json TEXT NULL"))
            conn.commit()
            print("[+] showcase_project.images_json 已加列")


if __name__ == '__main__':
    with app.app_context():
        step_1_columns()
    print("migrate_36 完成")

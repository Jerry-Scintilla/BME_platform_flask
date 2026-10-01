"""迁移 61：club_group + description 列（小组介绍，组织页组态展示位，2026-10-01）。

管理端 PUT /admin/club/groups/<id> 可写（≤500 字，空=清空）；
GET /organization 树节点带 description，组织页组态「小组介绍」区自动生效。

用法（项目根）：.venv/bin/python scripts/migrate/migrate_61_club_group_description.py
回滚：ALTER TABLE club_group DROP COLUMN description;（升级前先 mysqldump）
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from app import app          # noqa: E402,F401  (import 即装配 config)
from sqlalchemy import create_engine, text, inspect  # noqa: E402

import config                # noqa: E402

engine = create_engine(config.SQLALCHEMY_DATABASE_URI)
insp = inspect(engine)

if __name__ == '__main__':
    with app.app_context():
        with engine.connect() as conn:
            cols = [c['name'] for c in insp.get_columns('club_group')]
            if 'description' not in cols:
                conn.execute(text(
                    "ALTER TABLE club_group ADD COLUMN description TEXT NULL "
                    "COMMENT '小组介绍(组织页组态展示位,<=500字)'"))
                conn.commit()
                print("[+] club_group: 已加列 description")
            else:
                print("[=] club_group.description 已存在")
        from models import ClubGroup
        n = ClubGroup.query.count()
        print(f"[i] club_group 现有 {n} 个组，介绍为空时组织页显示占位文案")

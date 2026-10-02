"""迁移 71：club_officer + scope_note 列（任职范围描述，2026-10-02）。

社长挂组配套（用户拍板）：管理端任命/编辑可填任职范围描述（<=200 字），
个人主页「社团身份」卡随职位展示。

用法（项目根）：.venv/bin/python scripts/migrate/migrate_71_officer_scope_note.py
回滚：ALTER TABLE club_officer DROP COLUMN scope_note;（升级前先 mysqldump）
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
            cols = [c['name'] for c in insp.get_columns('club_officer')]
            if 'scope_note' not in cols:
                conn.execute(text(
                    "ALTER TABLE club_officer ADD COLUMN scope_note VARCHAR(200) NULL "
                    "COMMENT '任职范围描述(选填,个人主页社团身份卡展示)'"))
                conn.commit()
                print('[+] club_officer: 已加列 scope_note')
            else:
                print('[=] club_officer.scope_note 已存在')

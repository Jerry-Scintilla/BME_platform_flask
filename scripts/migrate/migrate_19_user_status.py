"""迁移 19：user 表加账号状态列 status（幂等）。

背景（2026-09-11 用户管理）：UserManage 恢复操作列。删除被否决——user.id 被 25+ 张表
引用（文章/课程/勋章/小组/营期成员/考勤/通知/感谢信/LLM…），删除要么炸外键要么毁历史；
用户拍板改做**封禁**：banned = 禁登录 + 存量 token 在 app.py before_request 入口拦截，
内容与归属全保留、可逆。

- ALTER TABLE user ADD COLUMN status VARCHAR(20) NOT NULL DEFAULT 'active'

用法（项目根目录）：python scripts/migrate/migrate_19_user_status.py
回滚：ALTER TABLE user DROP COLUMN status
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

import config  # noqa: E402
from sqlalchemy import create_engine, text, inspect  # noqa: E402

engine = create_engine(config.SQLALCHEMY_DATABASE_URI)
insp = inspect(engine)

with engine.connect() as conn:
    cols = {c['name'] for c in insp.get_columns('user')}
    if 'status' not in cols:
        conn.execute(text(
            "ALTER TABLE user ADD COLUMN status VARCHAR(20) NOT NULL DEFAULT 'active'"))
        conn.commit()
        print("[+] user.status 已添加（默认 active）")
    else:
        print("[=] user.status 已存在")
    print("[done] migrate_19 完成")

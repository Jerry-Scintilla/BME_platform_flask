"""迁移 17：用户邮件通知开关（user.email_notify_enabled，幂等）。

背景（2026-09-06 用户反馈）：部分用户被设置为邮件通知对象（如每日出勤汇总）
后每天都会收到邮件，希望可自行关闭。个人中心-账户设置提供开关
（GET/POST /notification/email_pref），发送侧在 notification._resolve_email_recipients
统一过滤（通知邮件 + 报告邮件；登录验证码不受影响）。

- user + email_notify_enabled TINYINT(1) NOT NULL DEFAULT 1（存量用户默认开启，行为不变）

用法：python scripts/migrate/migrate_17_email_notify.py
回滚：ALTER TABLE `user` DROP COLUMN email_notify_enabled
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
    if 'email_notify_enabled' not in cols:
        conn.execute(text(
            "ALTER TABLE `user` ADD COLUMN email_notify_enabled TINYINT(1) NOT NULL DEFAULT 1 "
            "COMMENT '邮件通知接收开关：0=关闭（不收通知/报告类邮件）'"))
        conn.commit()
        print("[+] user.email_notify_enabled 已添加")
    else:
        print("[=] user.email_notify_enabled 已存在")
    print("[done] migrate_17 完成")

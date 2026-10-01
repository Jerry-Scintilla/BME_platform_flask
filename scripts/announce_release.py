"""发版广播：为全站在册用户创建一条系统通知，点击直达 /changelog 更新日志页。

配套《发布与部署 SOP》（docs/记录/发布与部署SOP.md）：A2 发版动作产出公告后，
在**部署完成**时于服务器执行本脚本（通知深链指向新版的 /changelog，先发后部会跳 404）。

前端深链：source_type='platform_release' → /changelog（composables/notificationTarget.js）。

用法（项目根）：
  .venv/bin/python scripts/announce_release.py --version v3.3            # 干跑（只报数字）
  .venv/bin/python scripts/announce_release.py --version v3.3 --apply    # 落库
幂等：按标题去重（已收到同标题通知的用户跳过），重复执行无害。
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import app          # noqa: E402,F401  (import 即装配 config)


def main():
    parser = argparse.ArgumentParser(description='发版广播（系统通知，深链 /changelog）')
    parser.add_argument('--version', required=True, help='版本号，如 v3.3（标题=「平台更新 v3.3」）')
    parser.add_argument('--apply', action='store_true', help='落库（默认干跑）')
    args = parser.parse_args()
    version = args.version.strip()
    if not version.startswith('v'):
        version = f'v{version}'

    title = f'平台更新 {version}'
    content = (f'BME 平台 {version} 已发布。'
               '点击查看本版本的完整更新日志。')

    with app.app_context():
        from exts import db
        from models import UserModel, NotificationModel
        from blueprints.notification import create_notification
        users = UserModel.query.filter_by(status='active').all()
        existing = {n.user_id for n in
                    NotificationModel.query.filter_by(title=title).all()}
        todo = [u for u in users if u.id not in existing]
        print(f'[i] 在册用户 {len(users)}，已收到「{title}」 {len(existing)}，本次将发送 {len(todo)}')
        if not args.apply:
            print('[i] 干跑未落库；确认后加 --apply')
            return
        for u in todo:
            create_notification(u.id, title, content,
                                category='system', source_type='platform_release')
        db.session.commit()
        print(f'[+] 已创建 {len(todo)} 条系统通知（category=system, source_type=platform_release）')
        print('[i] 用户点击后经 notificationTarget 深链直达 /changelog')


if __name__ == '__main__':
    main()

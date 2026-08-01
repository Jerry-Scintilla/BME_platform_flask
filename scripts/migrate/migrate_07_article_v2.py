"""迁移 07：文章 V2 建表（幂等）。

- 建新表 article_v2（create_all，已存在则跳过）

正文存 content_md 字段（Markdown），不写文件。用法（项目根）：
python scripts/migrate/migrate_07_article_v2.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from app import app          # noqa: E402
from exts import db          # noqa: E402

with app.app_context():
    db.create_all()   # 建 article_v2（已存在则跳过）
    print("[+] create_all done (article_v2 created if absent)")
    db.session.commit()
    print("[done] migrate_07 完成")

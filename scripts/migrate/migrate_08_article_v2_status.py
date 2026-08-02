"""迁移 08：article_v2 加草稿状态字段（status / created_at / updated_at），放宽内容字段可空。

- create_all 不给已存在表补列（见 schema-drift 坑：报 1054 Unknown column），故用 ALTER TABLE 手动加列
- 幂等：用 inspector 检查列存在再 ADD；MODIFY / UPDATE 可重复执行
- 回填：现有行（均由旧 /public 发表）视为已发布，created_at/updated_at 从 publish_time 继承

用法（项目根）：
python scripts/migrate/migrate_08_article_v2_status.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from app import app          # noqa: E402
from exts import db          # noqa: E402
from sqlalchemy import inspect, text          # noqa: E402

with app.app_context():
    inspector = inspect(db.engine)
    existing = {c['name'] for c in inspector.get_columns('article_v2')}

    stmts = []
    if 'status' not in existing:
        stmts.append("ALTER TABLE article_v2 ADD COLUMN status VARCHAR(20) DEFAULT 'published'")
    if 'created_at' not in existing:
        stmts.append("ALTER TABLE article_v2 ADD COLUMN created_at DATETIME")
    if 'updated_at' not in existing:
        stmts.append("ALTER TABLE article_v2 ADD COLUMN updated_at DATETIME")

    # 放宽 nullable（MODIFY 可重复执行，幂等无害）
    stmts.append("ALTER TABLE article_v2 MODIFY title VARCHAR(100) NULL")
    stmts.append("ALTER TABLE article_v2 MODIFY introduction TEXT NULL")
    stmts.append("ALTER TABLE article_v2 MODIFY content_md TEXT NULL")
    stmts.append("ALTER TABLE article_v2 MODIFY publish_time DATETIME NULL")

    with db.engine.begin() as conn:
        for s in stmts:
            print(f"[+] {s}")
            conn.execute(text(s))

        # 回填：现有行（旧 /public 发表）视为已发布；时间从 publish_time 继承
        conn.execute(text(
            "UPDATE article_v2 SET status='published' "
            "WHERE status IS NULL OR status=''"
        ))
        conn.execute(text(
            "UPDATE article_v2 SET created_at=publish_time "
            "WHERE created_at IS NULL AND publish_time IS NOT NULL"
        ))
        conn.execute(text(
            "UPDATE article_v2 SET updated_at=publish_time "
            "WHERE updated_at IS NULL AND publish_time IS NOT NULL"
        ))
        # 无 publish_time 的兜底（理论上不存在）
        conn.execute(text("UPDATE article_v2 SET created_at=NOW() WHERE created_at IS NULL"))
        conn.execute(text("UPDATE article_v2 SET updated_at=NOW() WHERE updated_at IS NULL"))

    print("[done] migrate_08 完成")

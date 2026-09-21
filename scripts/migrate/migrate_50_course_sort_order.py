"""迁移 50：course 表加 sort_order 手动排序列（2026-09-20，幂等）。

背景：课程列表（学生端年份分组 + 管理端表格）此前无任何排序，按插入序展示，
同年上传的课程新版被压在旧版下面。本次加单值 sort_order：
- 列表统一 order_by(sort_order ASC, publish_time DESC, id DESC)；
- 存量全部为默认 0 → 自动回退为「发布时间倒序（新课程在前）」，无需回填；
- 管理端「上移/下移」按 resource_sort 同款全量重排语义改写该列。
配合同批次的 status='off_shelf'（下架仅隐藏列表，不动排序槽位，重新上架即回原位）。

用法：python scripts/migrate/migrate_50_course_sort_order.py
回滚：ALTER TABLE course DROP COLUMN sort_order;
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

import config  # noqa: E402
from sqlalchemy import create_engine, text, inspect  # noqa: E402

COLUMNS = {
    'course': [
        ("sort_order", "ALTER TABLE course ADD COLUMN sort_order INT NOT NULL DEFAULT 0 "
         "COMMENT '手动排序，小者在前，同序按发布时间倒序（0=未手动排序）'"),
    ],
}

engine = create_engine(config.SQLALCHEMY_DATABASE_URI)
insp = inspect(engine)

with engine.connect() as conn:
    for table, cols in COLUMNS.items():
        if not insp.has_table(table):
            print(f"[!] 表 {table} 不存在，跳过（新库由 create_all 建全量列）")
            continue
        existing = {c['name'] for c in insp.get_columns(table)}
        for name, ddl in cols:
            if name in existing:
                print(f"[=] {table}.{name} 已存在")
                continue
            conn.execute(text(ddl))
            conn.commit()
            print(f"[+] {table}.{name} 已添加")
    print("[done] migrate_50 完成")

"""迁移 49：组会课内布置认证截止（2026-09-20，幂等）。

课内进度布置（camp_meeting_chapter_plan）此前只有章节多选，无任何时间字段——
标签写「到下次组会前完成认证」但没有落地。本次在 camp_meeting 上加单值
chapter_due_at（统一截止）：不放 plan 每行（plan 是纯投影联接表、assignments
整组替换写放大、产品语义就是统一截止，与 CampMeetingTask.due_at 同为实体级单值）。
调度器据此发 T-24h 提醒与逾期通知（仅 team 域、仅有布置章的会生效）。
存量无截止 = 不提醒，语义正确，无需回填。

用法：python scripts/migrate/migrate_49_chapter_due_at.py
回滚：ALTER TABLE camp_meeting DROP COLUMN chapter_due_at;
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

import config  # noqa: E402
from sqlalchemy import create_engine, text, inspect  # noqa: E402

COLUMNS = {
    'camp_meeting': [
        ("chapter_due_at", "ALTER TABLE camp_meeting ADD COLUMN chapter_due_at DATETIME NULL "
         "COMMENT '课内布置认证截止（team 域；调度器据此发 T-24h/逾期提醒）'"),
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
    print("[done] migrate_49 完成")

"""迁移 44：组会任务生命周期（2026-09-20，幂等）。

《通知方案》§3.7：任务此前只有标题/说明/提交类型与学员提交，缺截止、必交、审阅。
- camp_meeting_task + due_at / required(默认 true) / allow_late(默认 true)
- camp_meeting_task_submission + status(默认 submitted，存量行回填) / submitted_at
  （取 created_at 保持时序） / reviewed_by / reviewed_at / review_comment

「已交」口径不变（有效提交即可，兼容 zip 打包与既有看板），审阅态是叠加层：
returned 视为未完成（重交后回 submitted），accepted 只是认可标记。

用法：python scripts/migrate/migrate_44_meeting_task_lifecycle.py
回滚：ALTER TABLE camp_meeting_task DROP COLUMN due_at, DROP COLUMN required, DROP COLUMN allow_late;
      ALTER TABLE camp_meeting_task_submission DROP COLUMN status, DROP COLUMN submitted_at,
      DROP COLUMN reviewed_by, DROP COLUMN reviewed_at, DROP COLUMN review_comment;
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

import config  # noqa: E402
from sqlalchemy import create_engine, text, inspect  # noqa: E402

COLUMNS = {
    'camp_meeting_task': [
        ("due_at", "ALTER TABLE camp_meeting_task ADD COLUMN due_at DATETIME NULL "
         "COMMENT '截止时间（调度器据此发 T-24h/逾期提醒）'"),
        ("required", "ALTER TABLE camp_meeting_task ADD COLUMN required TINYINT(1) NOT NULL DEFAULT 1 "
         "COMMENT '必交（逾期判定与提醒只看必交任务）'"),
        ("allow_late", "ALTER TABLE camp_meeting_task ADD COLUMN allow_late TINYINT(1) NOT NULL DEFAULT 1 "
         "COMMENT '允许迟交（关=逾期后拒收）'"),
    ],
    'camp_meeting_task_submission': [
        ("status", "ALTER TABLE camp_meeting_task_submission ADD COLUMN status VARCHAR(20) NOT NULL "
         "DEFAULT 'submitted' COMMENT 'submitted/returned/accepted'"),
        ("submitted_at", "ALTER TABLE camp_meeting_task_submission ADD COLUMN submitted_at DATETIME NULL "
         "COMMENT '最近一次有效提交时间'"),
        ("reviewed_by", "ALTER TABLE camp_meeting_task_submission ADD COLUMN reviewed_by INT NULL "
         "COMMENT '审阅人'"),
        ("reviewed_at", "ALTER TABLE camp_meeting_task_submission ADD COLUMN reviewed_at DATETIME NULL "
         "COMMENT '审阅时间'"),
        ("review_comment", "ALTER TABLE camp_meeting_task_submission ADD COLUMN review_comment VARCHAR(500) NULL "
         "COMMENT '审阅意见/退回原因'"),
    ],
}

BACKFILL = [
    # 存量提交行回填 submitted 态与提交时间（不伪造审阅记录）
    "UPDATE camp_meeting_task_submission SET status='submitted', submitted_at=created_at "
    "WHERE status IS NULL OR status='' OR submitted_at IS NULL",
]

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
    for sql in BACKFILL:
        r = conn.execute(text(sql))
        conn.commit()
        print(f"[~] 回填 {r.rowcount} 行")
    print("[done] migrate_44 完成")

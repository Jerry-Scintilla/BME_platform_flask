"""迁移 41：培训营闭环·业务真相补列（2026-09-20，幂等）。

配合《培训营通知系统与业务闭环重构设计方案》阶段 0（P0 止血）：
- camp_attendance_plan.source：承诺日出勤日来源（self_selected/admin/legacy）。
  存量行回填 legacy（不猜测来源）；此后「重生成/日期同步」不再给 self_selected
  学员自动补全工作日（方案 §3.2：承诺语义保护）。
- camp_join_request.review_note：报名评审意见（拒绝原因入业务真相，不再只拼进通知，
  方案 §3.8：通知从业务记录渲染文案，而不是反过来）。
- camp_leave.decision_note：请假审批意见/拒绝原因（同上）。

用法：python scripts/migrate/migrate_41_camp_closure_truth.py
回滚：ALTER TABLE ... DROP COLUMN source / review_note / decision_note;
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

import config  # noqa: E402
from sqlalchemy import create_engine, text, inspect  # noqa: E402

COLUMNS = {
    'camp_attendance_plan': [(
        "source",
        "ALTER TABLE camp_attendance_plan ADD COLUMN source VARCHAR(20) "
        "NOT NULL DEFAULT 'legacy' COMMENT "
        "'来源：self_selected=学员报名手选 / admin=管理员兜底展开 / legacy=迁移存量'",
    )],
    'camp_join_request': [(
        "review_note",
        "ALTER TABLE camp_join_request ADD COLUMN review_note TEXT NULL "
        "COMMENT '评审意见（拒绝原因等，业务真相）'",
    )],
    'camp_leave': [(
        "decision_note",
        "ALTER TABLE camp_leave ADD COLUMN decision_note TEXT NULL "
        "COMMENT '审批意见/拒绝原因（业务真相）'",
    )],
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
    print("[done] migrate_41 完成")

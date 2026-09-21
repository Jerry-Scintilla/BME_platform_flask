"""迁移 52：course 表加 learning_mode 学习方式列（2026-09-21，幂等）。

背景：此前「能否学习」只取决于有无 user_course 选课行，课程本身无访问策略，
且 /learningProgress/lesson/update 对任何登录用户敞开。本次加两档学习方式：
- open（自主学）：登录即可直接学习，首次打点自动建立选课关系（全局口径）；
  全部课时自评完成 → user_course.status 自动置 completed（课成判定标准）。
- camp（营期学）：仅经营期选课可学；未选课打点返回 403。完成判定=导生按章认证。
存量默认 'camp'（保持 A14「入课唯一途径=营期选课」语义），无需回填；
自主学课由管理端逐门切换。

用法：python scripts/migrate/migrate_52_course_learning_mode.py
回滚：ALTER TABLE course DROP COLUMN learning_mode;
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

import config  # noqa: E402
from sqlalchemy import create_engine, text, inspect  # noqa: E402

COLUMNS = {
    'course': [
        ("learning_mode", "ALTER TABLE course ADD COLUMN learning_mode VARCHAR(20) NOT NULL "
         "DEFAULT 'camp' COMMENT '学习方式：open=自主学（登录即学，完成自评）/ camp=营期学（仅营期选课）'"),
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
    print("[done] migrate_52 完成")

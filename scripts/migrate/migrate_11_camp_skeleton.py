"""迁移 11：营期共用骨架——CampCycle 表 + camp_session.category/cycle_id（幂等）。

阶段 1（2026-09，方案 v1.2 §3.2/§3.3）：
- 新增 camp_cycle（教学周期：code/name/sort_order，无起止日期无状态）；
- camp_session + category（learning/project，存量回填 learning）+ cycle_id；
- 存量营期补一条 '2026-summer' 周期并挂接；
- 旧 camp_type 列保留不删（停读写，下个大版本清理）。

用法（项目根目录）：python scripts/migrate/migrate_11_camp_skeleton.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

import config  # noqa: E402
from sqlalchemy import create_engine, text, inspect  # noqa: E402

engine = create_engine(config.SQLALCHEMY_DATABASE_URI)
insp = inspect(engine)

with engine.connect() as conn:
    # ── 1. camp_cycle 建表 ──
    if not insp.has_table('camp_cycle'):
        conn.execute(text("""
            CREATE TABLE camp_cycle (
                id INT AUTO_INCREMENT PRIMARY KEY,
                code VARCHAR(20) NOT NULL UNIQUE,
                name VARCHAR(50) NOT NULL,
                sort_order INT DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            ) CHARSET=utf8mb4
        """))
        conn.commit()
        print("[+] camp_cycle 已创建")
    else:
        print("[=] camp_cycle 已存在")

    # ── 2. camp_session 加列 ──
    cols = {c['name'] for c in insp.get_columns('camp_session')}
    if 'category' not in cols:
        conn.execute(text(
            "ALTER TABLE camp_session ADD COLUMN category VARCHAR(20) NOT NULL DEFAULT 'learning'"
        ))
        conn.commit()
        print("[+] camp_session.category 已添加（回填 learning）")
    else:
        print("[=] camp_session.category 已存在")
    if 'cycle_id' not in cols:
        conn.execute(text(
            "ALTER TABLE camp_session ADD COLUMN cycle_id INT NULL, "
            "ADD CONSTRAINT fk_camp_session_cycle FOREIGN KEY (cycle_id) REFERENCES camp_cycle(id)"
        ))
        conn.commit()
        print("[+] camp_session.cycle_id 已添加")
    else:
        print("[=] camp_session.cycle_id 已存在")

    # ── 3. 回填：存量营期挂 2026-summer 周期 ──
    r0 = conn.execute(text(
        "INSERT IGNORE INTO camp_cycle (code, name, sort_order) VALUES ('2026-summer', '2026 暑期', 0)"
    ))
    conn.commit()
    if r0.rowcount:
        print("[+] 已创建周期 2026-summer")
    n = conn.execute(text(
        "UPDATE camp_session SET cycle_id = (SELECT id FROM camp_cycle WHERE code='2026-summer') "
        "WHERE cycle_id IS NULL"
    )).rowcount
    conn.commit()
    print(f"[~] {n} 个存量营期已挂接 2026-summer；category 已全量 learning")

    # ── 4. post-check ──
    rows = conn.execute(text(
        "SELECT category, COUNT(*) FROM camp_session GROUP BY category"
    )).fetchall()
    orphan = conn.execute(text(
        "SELECT COUNT(*) FROM camp_session WHERE cycle_id IS NULL"
    )).scalar()
    print(f"[ok] category 分布 {dict(rows)}；未挂周期营期 {orphan} 个")
    print("[done] migrate_11 完成")

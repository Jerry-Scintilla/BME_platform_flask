"""迁移 12：状态机五态化数据迁移（幂等）。

H-004 冻结（2026-09-02）：draft/upcoming/selecting/running/archived 取代
draft/active/archived。列本身是 VARCHAR 无需 DDL，只做存量映射：
draft→draft、active→running、archived→archived（upcoming/selecting 为新值无存量）。
用法：python scripts/migrate/migrate_12_state_machine.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

import config  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

engine = create_engine(config.SQLALCHEMY_DATABASE_URI)
with engine.connect() as conn:
    n = conn.execute(text(
        "UPDATE camp_session SET status='running' WHERE status='active'"
    )).rowcount
    conn.commit()
    rows = conn.execute(text(
        "SELECT status, COUNT(*) FROM camp_session GROUP BY status"
    )).fetchall()
    print(f"[~] active→running {n} 行；分布 {dict(rows)}")
    print("[done] migrate_12 完成")

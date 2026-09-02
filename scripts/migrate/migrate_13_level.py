"""迁移 13：用户等级地基 + 候选人池 source（幂等）。

设计（2026-09-02 用户定稿）：营期外链「导生候选人池」，进池策略可插拔——
A 手工导入 / B 按等级生成（user.level >= N 物化进池，source='level'）。
报名门禁只查池子，与等级系统解耦。等级现阶段手动调整，评价引擎属阶段 3。

- user + level（LV1-4 默认 1）；存量 seed：当过导生者（camp_member.role='mentor'）→ LV2
- camp_mentor_eligibility + source（manual/level），存量回填 manual
用法：python scripts/migrate/migrate_13_level.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

import config  # noqa: E402
from sqlalchemy import create_engine, text, inspect  # noqa: E402

engine = create_engine(config.SQLALCHEMY_DATABASE_URI)
insp = inspect(engine)

with engine.connect() as conn:
    ucols = {c['name'] for c in insp.get_columns('user')}
    if 'level' not in ucols:
        conn.execute(text(
            "ALTER TABLE `user` ADD COLUMN `level` INT NOT NULL DEFAULT 1 "
            "COMMENT '用户等级 LV1-4，现阶段手动，评价引擎阶段3'"
        ))
        conn.commit()
        print("[+] user.level 已添加（默认 1）")
    else:
        print("[=] user.level 已存在")

    n = conn.execute(text(
        "UPDATE `user` SET level=2 WHERE level<2 AND id IN "
        "(SELECT DISTINCT user_id FROM camp_member WHERE role='mentor')"
    )).rowcount
    conn.commit()
    print(f"[~] 存量导生 → LV2：{n} 人")

    ecols = {c['name'] for c in insp.get_columns('camp_mentor_eligibility')}
    if 'source' not in ecols:
        conn.execute(text(
            "ALTER TABLE camp_mentor_eligibility ADD COLUMN source VARCHAR(20) DEFAULT 'manual'"
        ))
        conn.commit()
        print("[+] camp_mentor_eligibility.source 已添加")
    else:
        print("[=] camp_mentor_eligibility.source 已存在")

    rows = conn.execute(text("SELECT level, COUNT(*) FROM `user` GROUP BY level")).fetchall()
    print(f"[ok] 等级分布 {dict(rows)}")
    print("[done] migrate_13 完成")

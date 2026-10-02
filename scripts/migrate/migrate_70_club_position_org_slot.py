"""迁移 70：club_position + org_slot 列（社团组织管理改版·职位类别显式化，2026-10-02）。

背景（设计方案 §9-9 预留兑现）：「全社治理职务」（社长/副社长/团支书）与
「组内组长类职位」（组长）此前只靠 sort_rank<=9 / >=10 数值区间区分，判定散在
organization.py / officers.py / provisioning.py 三处魔法数字。本列显式化两类 title：
  club  = 社团职务（原 rank<=9 管理层/分管口径）
  group = 组内职位（原 rank>=10 组长类口径）
可空设计：NULL = 按 sort_rank 兜底派生（services/club_rules.py 单源），保证 sqlite
单测（ORM 直建职位只设 rank）与不带类别的 API 建职位行为与历史逐位一致。

ORM 侧同批：models.py ClubPosition.org_slot；三处阈值判定收敛到 club_rules。

用法（项目根）：.venv/bin/python scripts/migrate/migrate_70_club_position_org_slot.py
回滚：ALTER TABLE club_position DROP COLUMN org_slot;（升级前先 mysqldump）
幂等：按列存在探测；回填仅在本次刚建列分支执行——重跑不覆盖人工改过的类别。
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

import config                # noqa: E402
from sqlalchemy import create_engine, text, inspect  # noqa: E402

engine = create_engine(config.SQLALCHEMY_DATABASE_URI)
insp = inspect(engine)

if __name__ == '__main__':
    with engine.connect() as conn:
        cols = [c['name'] for c in insp.get_columns('club_position')]
        if 'org_slot' not in cols:
            conn.execute(text(
                "ALTER TABLE club_position ADD COLUMN org_slot VARCHAR(10) NULL "
                "COMMENT '类别 club=社团职务 / group=组内职位(组长类); NULL=按sort_rank兜底'"))
            # 回填只在本分支跑：与历史判定逐位等价（<=9→club，>=10→group），重跑不覆盖人工修正
            conn.execute(text(
                "UPDATE club_position SET org_slot = "
                "CASE WHEN sort_rank <= 9 THEN 'club' ELSE 'group' END"))
            conn.commit()
            print("[+] club_position: 已加列 org_slot 并按 sort_rank 回填类别")
        else:
            print("[=] club_position.org_slot 已存在，跳过（不覆盖既有类别）")
        rows = conn.execute(text(
            "SELECT org_slot, COUNT(*) FROM club_position GROUP BY org_slot")).fetchall()
        stats = ", ".join(f"{(r[0] or 'NULL')}={r[1]}" for r in rows)
        print(f"[i] 职位类别分布：{stats}")

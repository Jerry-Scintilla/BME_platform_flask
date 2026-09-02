"""迁移 16：报名选意向大组（camp_join_request + preferred_tag，幂等）。

背景（2026-09-02 用户要求）：报名要进营期工作台内进行，且报名时需选择意向大组
（软件组/硬件组等，取本营 ms_tags 类别标签，A13）；项目营未来或有别的报名机制，
本字段按学习营先行落地，项目营报名形态阶段 3 再定。

- camp_join_request + preferred_tag VARCHAR(50) NULL（须在本营 ms_tags 内，端点校验）
- 存量行为空（历史申请未选组）

用法：python scripts/migrate/migrate_16_join_tag.py
回滚：ALTER TABLE camp_join_request DROP COLUMN preferred_tag
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

import config  # noqa: E402
from sqlalchemy import create_engine, text, inspect  # noqa: E402

engine = create_engine(config.SQLALCHEMY_DATABASE_URI)
insp = inspect(engine)

with engine.connect() as conn:
    cols = {c['name'] for c in insp.get_columns('camp_join_request')}
    if 'preferred_tag' not in cols:
        conn.execute(text(
            "ALTER TABLE camp_join_request ADD COLUMN preferred_tag VARCHAR(50) NULL "
            "COMMENT '报名意向大组（须在本营 ms_tags 内）'"))
        conn.commit()
        print("[+] camp_join_request.preferred_tag 已添加")
    else:
        print("[=] camp_join_request.preferred_tag 已存在")
    print("[done] migrate_16 完成")

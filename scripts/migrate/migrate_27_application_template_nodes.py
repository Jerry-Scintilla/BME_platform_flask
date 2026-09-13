"""迁移 27：申报版本表加模板节点快照列（2026-09-13，幂等）。

project_application_version.template_nodes（TEXT，JSON 数组）
——09-13 拍板「申报即设计模板」：申报时设计节点序列替代「计划」栏，
过审时据此建 ProjectTemplate 并实例化里程碑。存量行 NULL=负责人过审后自建。

用法：python scripts/migrate/migrate_27_application_template_nodes.py
回滚：ALTER TABLE project_application_version DROP COLUMN template_nodes
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

import config  # noqa: E402
from sqlalchemy import create_engine, text, inspect  # noqa: E402

DDL = """
    ALTER TABLE project_application_version
    ADD COLUMN template_nodes TEXT COMMENT '申报时设计的模板节点序列（JSON，09-13 申报即模板）'
"""

engine = create_engine(config.SQLALCHEMY_DATABASE_URI)
insp = inspect(engine)

with engine.connect() as conn:
    cols = [c['name'] for c in insp.get_columns('project_application_version')]
    if 'template_nodes' in cols:
        print("[=] template_nodes 已存在")
    else:
        conn.execute(text(DDL))
        conn.commit()
        print("[+] template_nodes 已添加")
    print("[done] migrate_27 完成")

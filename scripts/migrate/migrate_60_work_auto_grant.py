"""迁移 60：内部工作台·授权自动化（2026-10-01 设计方案，D1=乙全量回填，幂等）。

步骤：
1. work_workspace + auto_grant 列（DEFAULT 1：新建区与存量区一律默认开启入职自动授）
2. work_access_grant + origin 列（auto/manual，存量全 manual）
3. 回填不在本迁移内：由 scripts/backfill_work_auto_grants.py 按现存组织行生成自动授权
   （--dry-run 看报告，--apply 落库）

用法（项目根）：.venv/bin/python scripts/migrate/migrate_60_work_auto_grant.py
回滚：ALTER TABLE work_workspace DROP COLUMN auto_grant;
      ALTER TABLE work_access_grant DROP COLUMN origin;（升级前先 mysqldump）
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from app import app          # noqa: E402,F401  (import 即装配 config)
from sqlalchemy import create_engine, text, inspect  # noqa: E402

import config                # noqa: E402

engine = create_engine(config.SQLALCHEMY_DATABASE_URI)
insp = inspect(engine)


def _add_column(conn, table, ddl):
    conn.execute(text(ddl))
    conn.commit()
    print(f"[+] {table}: 已加列 {ddl.split()[-3] if 'COMMENT' in ddl else ''}".rstrip())


if __name__ == '__main__':
    with engine.connect() as conn:
        ws_cols = [c['name'] for c in insp.get_columns('work_workspace')]
        if 'auto_grant' not in ws_cols:
            _add_column(conn, 'work_workspace',
                        "ALTER TABLE work_workspace ADD COLUMN auto_grant "
                        "TINYINT(1) NOT NULL DEFAULT 1 COMMENT '入职自动授(派生器)'")
        else:
            print("[=] work_workspace.auto_grant 已存在")

        grant_cols = [c['name'] for c in insp.get_columns('work_access_grant')]
        if 'origin' not in grant_cols:
            _add_column(conn, 'work_access_grant',
                        "ALTER TABLE work_access_grant ADD COLUMN origin "
                        "VARCHAR(10) NOT NULL DEFAULT 'manual' COMMENT 'auto=派生/manual=手动'")
        else:
            print("[=] work_access_grant.origin 已存在")

    with app.app_context():
        from exts import db
        from models import WorkAccessGrant, WorkWorkspace
        n_ws = WorkWorkspace.query.filter_by(auto_grant=True).count()
        n_grant = WorkAccessGrant.query.count()
        print(f"[i] 自动授已开启的工作区 {n_ws} 个；授权行共 {n_grant} 条（存量视为 manual）")
        print("[i] 回填：.venv/bin/python scripts/backfill_work_auto_grants.py --dry-run")

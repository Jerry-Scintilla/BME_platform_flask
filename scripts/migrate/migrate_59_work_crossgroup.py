"""迁移 59：内部工作台·跨组协作（2026-09-30 设计方案，feature/work-collab X1-X3，幂等）。

步骤：
1. 建新表（create_all 限定清单，checkfirst 幂等）：
   work_handoff（跨组交付，方案 §4.2）
   work_object_claim（对象认领通用化，X2 方案 §4.3：source_type+source_id+workspace_id）
2. 幂等修补：work_access_grant + subtree 列（X1 摘要层授权标记）
3. 幂等修补：work_workspace + scope 列（X3 社团工作区，'group'|'club'）；
   club_group_id 改 NULLable（MySQL：MODIFY 列定义——本迁移仅在新列存在时执行一次，
   重复执行由 inspect 判定跳过）
4. 无存量数据迁移：subtree 授权由治理端勾选开通；work_handoff 为空表起步

用法（项目根）：.venv/bin/python scripts/migrate/migrate_59_work_crossgroup.py
回滚：ALTER TABLE work_workspace DROP COLUMN scope;
      ALTER TABLE work_access_grant DROP COLUMN subtree;
      DROP TABLE work_object_claim, work_handoff;（升级前先 mysqldump）
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from app import app          # noqa: E402,F401  (import 即装配 config)
from sqlalchemy import create_engine, text, inspect  # noqa: E402

import config                # noqa: E402

engine = create_engine(config.SQLALCHEMY_DATABASE_URI)
insp = inspect(engine)

NEW_TABLES = ('work_handoff', 'work_object_claim')


def _add_column(conn, table, ddl):
    conn.execute(text(ddl))
    conn.commit()
    print(f"[+] {table}: {ddl.split()[-1] if 'ADD COLUMN' in ddl else ddl}")


if __name__ == '__main__':
    with app.app_context():
        from exts import db
        missing = [t for t in NEW_TABLES if not insp.has_table(t)]
        if missing:
            db.create_all(tables=[db.metadata.tables[t] for t in missing])
            print(f"[+] 已建表：{', '.join(missing)}")
        else:
            print("[=] 跨组协作表均已存在")

    # 幂等清理：通用化前的旧空表（未提交过的中间形态）
    with engine.connect() as conn:
        if insp.has_table('work_course_claim'):
            conn.execute(text("DROP TABLE work_course_claim"))
            conn.commit()
            print("[+] 已清理旧空表 work_course_claim（通用化为 work_object_claim）")

        grant_cols = [c['name'] for c in insp.get_columns('work_access_grant')]
        if 'subtree' not in grant_cols:
            _add_column(conn, 'work_access_grant',
                        "ALTER TABLE work_access_grant ADD COLUMN subtree "
                        "TINYINT(1) NOT NULL DEFAULT 0 COMMENT '子树汇总(摘要级)'")
        else:
            print("[=] work_access_grant.subtree 已存在")

        ws_cols = [c['name'] for c in insp.get_columns('work_workspace')]
        if 'scope' not in ws_cols:
            _add_column(conn, 'work_workspace',
                        "ALTER TABLE work_workspace ADD COLUMN scope "
                        "VARCHAR(10) NOT NULL DEFAULT 'group' COMMENT 'group|club(社团工作区)'")
            # club 工作区 club_group_id 为空：放开 NOT NULL（幂等：仅 scope 首次添加时执行）
            if engine.dialect.name == 'mysql':
                conn.execute(text(
                    "ALTER TABLE work_workspace MODIFY COLUMN club_group_id INT NULL"))
                conn.commit()
                print("[+] work_workspace.club_group_id 已放开 NULL")
        else:
            print("[=] work_workspace.scope 已存在")

        n = conn.execute(text("SELECT COUNT(*) FROM work_handoff")).scalar()
        print(f"[i] work_handoff 现有 {n} 行")

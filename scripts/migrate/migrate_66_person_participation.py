"""迁移 66：D5 参与锚点与 enforcement 地基（成员身份确认与重复账号安全迁移，2026-10-02）。

新建 4 表（规格 3.2/9.2/9.4）：
  camp_person_participation   营期参与锚点：PK(camp_session_id, person_id)+UNIQUE(camp_member_id)，
                              复合外键核对 user 归属与 camp_member 三元组一致——
                              「同一 Person 同营唯一」的数据库裁决点
  unit_person_participation   单元参与锚点：同构（PK(unit_id, person_id)+UNIQUE(unit_member_id)）
  person_workspace_restriction 人员级工作区否决：换主账号不得绕过账号级 veto（规格 9.4）
  identity_exception_grant    宽限/续办例外：按具体业务授权，不能授予核验成功或跳过双端证明

前置 DDL：camp_member 加 UNIQUE(id, camp_session_id, user_id)、camp_unit_member 加
UNIQUE(id, unit_id, user_id)——锚点复合外键的引用目标（id 已 PK，此唯一键恒成立，
仅为外键可引用而建，user 表 UNIQUE(id,person_id) 同款手法）。

用法（项目根）：.venv/bin/python scripts/migrate/migrate_66_person_participation.py
回滚：DROP TABLE identity_exception_grant, person_workspace_restriction,
     unit_person_participation, camp_person_participation;
     ALTER TABLE camp_member DROP INDEX uq_cm_triple; ALTER TABLE camp_unit_member DROP INDEX uq_cum_triple;
依赖：只加表不回填——锚点影子登记由 scripts/backfill_person_participation.py 执行；
enforcement 开关 IDENTITY_ENFORCEMENT_MODE 默认 shadow（规格 13.1），上线路径见 .env_example。
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

import config                # noqa: E402
from sqlalchemy import create_engine, text, inspect  # noqa: E402

engine = create_engine(config.SQLALCHEMY_DATABASE_URI)
insp = inspect(engine)

PRE_UNIQUES = [
    ("camp_member", "uq_cm_triple",
     "ALTER TABLE camp_member ADD UNIQUE KEY uq_cm_triple (id, camp_session_id, user_id)"),
    ("camp_unit_member", "uq_cum_triple",
     "ALTER TABLE camp_unit_member ADD UNIQUE KEY uq_cum_triple (id, unit_id, user_id)"),
]

NEW_TABLES = {
    "camp_person_participation": """
        CREATE TABLE camp_person_participation (
          camp_session_id INT NOT NULL,
          person_id INT NOT NULL,
          user_id INT NOT NULL COMMENT '当前参与号；续办例外可指向原成员账号',
          camp_member_id INT NOT NULL,
          state VARCHAR(20) NOT NULL DEFAULT 'active' COMMENT 'active/ended；退出历史另记事件',
          version INT NOT NULL DEFAULT 1,
          created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
          PRIMARY KEY (camp_session_id, person_id),
          UNIQUE KEY uq_cpp_member (camp_member_id),
          KEY idx_cpp_user (user_id),
          CONSTRAINT fk_cpp_user_person FOREIGN KEY (user_id, person_id)
            REFERENCES `user` (id, person_id),
          CONSTRAINT fk_cpp_member FOREIGN KEY (camp_member_id, camp_session_id, user_id)
            REFERENCES camp_member (id, camp_session_id, user_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='营期人员参与锚点（同Person同营唯一）'
    """,
    "unit_person_participation": """
        CREATE TABLE unit_person_participation (
          unit_id INT NOT NULL,
          person_id INT NOT NULL,
          user_id INT NOT NULL,
          unit_member_id INT NOT NULL,
          state VARCHAR(20) NOT NULL DEFAULT 'active',
          version INT NOT NULL DEFAULT 1,
          created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
          PRIMARY KEY (unit_id, person_id),
          UNIQUE KEY uq_upp_member (unit_member_id),
          KEY idx_upp_user (user_id),
          CONSTRAINT fk_upp_user_person FOREIGN KEY (user_id, person_id)
            REFERENCES `user` (id, person_id),
          CONSTRAINT fk_upp_member FOREIGN KEY (unit_member_id, unit_id, user_id)
            REFERENCES camp_unit_member (id, unit_id, user_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='单元人员参与锚点'
    """,
    "person_workspace_restriction": """
        CREATE TABLE person_workspace_restriction (
          person_id INT NOT NULL,
          workspace_id INT NOT NULL COMMENT '具体工作区（第一轮不做事无巨细的全局否决）',
          state VARCHAR(20) NOT NULL DEFAULT 'vetoed' COMMENT 'vetoed/lifted',
          reason VARCHAR(255) NOT NULL,
          source_event VARCHAR(64) NULL COMMENT '来源（账号级 veto 迁移/人工）',
          version INT NOT NULL DEFAULT 1,
          created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
          PRIMARY KEY (person_id, workspace_id),
          KEY idx_pwr_person (person_id, state)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='人员级工作区否决（换主号不绕过）'
    """,
    "identity_exception_grant": """
        CREATE TABLE identity_exception_grant (
          id INT AUTO_INCREMENT PRIMARY KEY,
          person_id INT NOT NULL,
          user_id INT NOT NULL,
          operation_scope VARCHAR(50) NOT NULL COMMENT 'camp_join/unit_join/work_access等具体业务',
          scope_id INT NULL,
          valid_until DATETIME NOT NULL,
          reason VARCHAR(255) NOT NULL,
          approved_by INT NOT NULL,
          state VARCHAR(20) NOT NULL DEFAULT 'active' COMMENT 'active/expired/revoked',
          created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
          KEY idx_ieg_lookup (operation_scope, scope_id, state, valid_until),
          KEY idx_ieg_person (person_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='身份宽限/续办例外（按具体业务授权）'
    """,
}


def migrate_pre_uniques(conn):
    for table, name, ddl in PRE_UNIQUES:
        if name in {i['name'] for i in insp.get_indexes(table)}:
            print(f"[=] {table}.{name} 已存在")
            continue
        conn.execute(text(ddl))
        conn.commit()
        print(f"[+] {table}: 已加唯一键 {name}（外键引用目标）")


def migrate_tables(conn):
    existing = set(insp.get_table_names())
    for table, ddl in NEW_TABLES.items():
        if table in existing:
            print(f"[=] {table} 已存在")
            continue
        conn.execute(text(ddl))
        conn.commit()
        print(f"[+] 已建表 {table}")


if __name__ == '__main__':
    with engine.connect() as conn:
        migrate_pre_uniques(conn)
        migrate_tables(conn)

    from app import app  # noqa: E402,F401
    with app.app_context():
        print("[i] 参与锚点/否决/例外四表就位；影子登记走 "
              "scripts/backfill_person_participation.py（--dry-run 先看冲突报告）")
        print("[i] enforcement 开关 IDENTITY_ENFORCEMENT_MODE 默认 shadow（只记不拦）")

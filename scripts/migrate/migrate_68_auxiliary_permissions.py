"""迁移 68：D5 收尾 P2——辅助账号授权表 + 身份岗位能力权限位（规格 3.2/9.3/11 章）。

  auxiliary_account_grant  管理辅助账号授权：绑定归属人员、用途、范围、期限
                           （默认 90 天复核）、批准人、状态——同人关系不替代
                           角色授权；到期实时检查（不依赖清理任务）
  permission 种子           identity_review / identity_admin / identity_migrate /
                           identity_recover 四个岗位能力位（规格 11：审核、配置、
                           迁移、恢复）——super_admin 直通不变，普通账号可经
                           UserPermission 授予（如营期老师按范围审核）

用法（项目根）：.venv/bin/python scripts/migrate/migrate_68_auxiliary_permissions.py
回滚：DROP TABLE auxiliary_account_grant;
      DELETE FROM permission WHERE name LIKE 'identity\\_%';
依赖：只加表与权限行，零行为变化；权限门槛在管理端点按位启用。
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

import config                # noqa: E402
from sqlalchemy import create_engine, text, inspect  # noqa: E402

engine = create_engine(config.SQLALCHEMY_DATABASE_URI)
insp = inspect(engine)

NEW_TABLES = {
    "auxiliary_account_grant": """
        CREATE TABLE auxiliary_account_grant (
          id INT AUTO_INCREMENT PRIMARY KEY,
          user_id INT NOT NULL COMMENT '辅助账号',
          owner_person_id INT NULL COMMENT '归属人员（绑定核验人员）',
          purpose VARCHAR(100) NOT NULL COMMENT '用途说明',
          scope VARCHAR(100) NULL COMMENT '范围（组/营期等）',
          valid_until DATETIME NOT NULL COMMENT '期限；默认批准+90 天，到期实时失效',
          approved_by INT NOT NULL,
          state VARCHAR(20) NOT NULL DEFAULT 'active' COMMENT 'active/expired/revoked',
          created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
          KEY idx_aag_user (user_id, state, valid_until),
          KEY idx_aag_owner (owner_person_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
          COMMENT='管理辅助账号授权（同人关系不替代角色授权）'
    """,
}

PERMISSIONS = [
    ('identity_review', '身份审核：核验申请/关联案例审批队列（super_admin 直通）'),
    ('identity_admin', '身份管理：学校核验配置/审核人名单'),
    ('identity_migrate', '身份迁移：复杂历史迁移任务（第二轮启用）'),
    ('identity_recover', '身份恢复：恢复申诉决策与补偿'),
]


def migrate_tables(conn):
    existing = set(insp.get_table_names())
    for table, ddl in NEW_TABLES.items():
        if table in existing:
            print(f"[=] {table} 已存在")
            continue
        conn.execute(text(ddl))
        conn.commit()
        print(f"[+] 已建表 {table}")


def seed_permissions(conn):
    for name, desc in PERMISSIONS:
        row = conn.execute(text(
            "SELECT id FROM permission WHERE name=:n"), {"n": name}).first()
        if row is not None:
            print(f"[=] 权限 {name} 已存在")
            continue
        conn.execute(text(
            "INSERT INTO permission (name, description) VALUES (:n, :d)"),
            {"n": name, "d": desc})
        conn.commit()
        print(f"[+] 已种子权限 {name}")


if __name__ == '__main__':
    with engine.connect() as conn:
        migrate_tables(conn)
        seed_permissions(conn)
    print("[i] 辅助授权经管理端批准（user.account_kind 同步 management_aux）；"
          "权限位在管理端点按位启用（super_admin 直通不变）")

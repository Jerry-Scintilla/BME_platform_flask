"""迁移 69：D1 会话四表 user 外键改 ON DELETE CASCADE（2026-10-02 审计转办根因修）。

背景（下一阶段计划·审计发现 #1）：删除用户时 auth_session 行触发 nullify 更新撞
NOT NULL——任何删用户路径都会炸。平台用户策略是软删除（banned），物理删除仅
冒烟/运维场景，但根因仍须修：会话/MFA 因子/恢复码/旧兑换四表语义上随账号终结，
改为级联删除；业务表（营期/文章等）保持 NO ACTION——有业务痕迹的账号本就
不该物理删除，由外键天然拦截。

ORM 侧同批：UserModel 上的 auth_sessions/auth_factors/auth_recovery_codes
backref 加 cascade='all, delete-orphan'（session.delete(user) 时同步清）。

用法（项目根）：.venv/bin/python scripts/migrate/migrate_69_auth_fk_cascade.py
回滚：ALTER TABLE ... DROP FOREIGN KEY, ADD FOREIGN KEY ...（还原 NO ACTION）；
幂等：按 DELETE_RULE 探测，已是 CASCADE 跳过。
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

import config                # noqa: E402
from sqlalchemy import create_engine, text, inspect  # noqa: E402

engine = create_engine(config.SQLALCHEMY_DATABASE_URI)

# (表, 约束名, 还原用列定义)
TARGETS = [
    ('auth_session', 'fk_auth_session_user',
     'CONSTRAINT fk_auth_session_user FOREIGN KEY (user_id) REFERENCES `user` (id) ON DELETE CASCADE'),
    ('auth_factor', 'fk_auth_factor_user',
     'CONSTRAINT fk_auth_factor_user FOREIGN KEY (user_id) REFERENCES `user` (id) ON DELETE CASCADE'),
    ('auth_recovery_code', 'fk_auth_recovery_user',
     'CONSTRAINT fk_auth_recovery_user FOREIGN KEY (user_id) REFERENCES `user` (id) ON DELETE CASCADE'),
    ('auth_legacy_refresh_consumption', 'fk_legacy_user',
     'CONSTRAINT fk_legacy_user FOREIGN KEY (user_id) REFERENCES `user` (id) ON DELETE CASCADE'),
]


def current_rule(conn, table):
    row = conn.execute(text(
        "SELECT DELETE_RULE FROM information_schema.REFERENTIAL_CONSTRAINTS "
        "WHERE CONSTRAINT_SCHEMA=DATABASE() AND TABLE_NAME=:t AND "
        "REFERENCED_TABLE_NAME='user' LIMIT 1"), {"t": table}).first()
    return row[0] if row else None


def migrate():
    with engine.connect() as conn:
        for table, fk_name, ddl in TARGETS:
            rule = current_rule(conn, table)
            if rule == 'CASCADE':
                print(f"[=] {table}.{fk_name} 已是 CASCADE")
                continue
            # 约束名探测（历史库可能自动命名）：按引用目标找本表指向 user 的约束名
            name = fk_name
            row = conn.execute(text(
                "SELECT CONSTRAINT_NAME FROM information_schema.REFERENTIAL_CONSTRAINTS "
                "WHERE CONSTRAINT_SCHEMA=DATABASE() AND TABLE_NAME=:t AND "
                "REFERENCED_TABLE_NAME='user' LIMIT 1"), {"t": table}).first()
            if row:
                name = row[0]
            conn.execute(text(f"ALTER TABLE {table} DROP FOREIGN KEY {name}"))
            conn.execute(text(f"ALTER TABLE {table} ADD {ddl}"))
            conn.commit()
            print(f"[+] {table}: user 外键 {name} → ON DELETE CASCADE")


if __name__ == '__main__':
    migrate()
    from app import app  # noqa: E402,F401
    with app.app_context():
        print("[i] ORM 侧 backref 级联已同批（models.py）；物理删用户仍会被业务表外键"
              "拦住（营期/文章等 NO ACTION）——这是设计")

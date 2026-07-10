"""迁移 01：给 user 表加 role 列 + 回填管理员角色（幂等）。

背景：RBAC 四级角色(super_admin/teacher/mentor/student)依赖 user.role 列。
本次变更新增该列，2025-06-12 之前的老库没有。加列默认 'student'，
必须把现有 user_mode='admin' 的用户回填为 'super_admin'，否则管理员
登录后失去后台门禁（check_permission/can 全部失效）。

顺序：必须在 migrate_05_perms 之前执行（权限授权按 role='super_admin' 查用户）。
用法（在项目根目录）：python scripts/migrate/migrate_01_user_role.py
"""
import os
import sys

# 把项目根加入 sys.path，使 from app import app 在子目录下也可用
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from app import app          # noqa: E402
from exts import db          # noqa: E402
from sqlalchemy import text, inspect  # noqa: E402

with app.app_context():
    engine = db.engine
    insp = inspect(engine)
    existing = {c['name'] for c in insp.get_columns('user')}

    if 'role' not in existing:
        with engine.connect() as conn:
            conn.execute(text(
                "ALTER TABLE `user` ADD COLUMN `role` VARCHAR(20) NOT NULL DEFAULT 'student'"
            ))
            conn.commit()
        print("[+] user.role 已添加（默认 'student'）")
    else:
        print("[=] user.role 已存在，跳过加列")

    # 回填：现有 user_mode='admin' → role='super_admin'（双写 user_mode 保留，裸 admin 门禁不改）
    result = db.session.execute(text(
        "UPDATE `user` SET role='super_admin' WHERE user_mode='admin' AND role<>'super_admin'"
    ))
    db.session.commit()
    print(f"[~] 回填 {result.rowcount} 个 admin 用户 → super_admin")

    # 校验：role 分布
    rows = db.session.execute(text("SELECT role, COUNT(*) FROM `user` GROUP BY role")).fetchall()
    print("[i] role 分布:", {r[0]: r[1] for r in rows})
    print("[done] migrate_01 完成")

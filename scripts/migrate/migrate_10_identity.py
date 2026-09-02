"""迁移 10：身份解耦——全局角色收敛两级 + 删除 user_mode 列（幂等）。

背景（营期升级 Phase 1a，2026-09）：
- 全局角色从四级(super_admin/teacher/mentor/student)收敛为两级 super_admin/user；
  「导生/学员」等身份改为营期内任职(CampMember)，不再看 user.role。
- teacher 并入 super_admin（admin_tag='teacher' 区分展示，无权限语义）；
  mentor/student 一律刷为 user。
- 代码侧已把全站约 48 处裸 user_mode=='admin' 门禁统一替换为 user.is_admin()，
  并删除登录/用户接口的 User_Mode/user_mode 返回键——因此本列可以安全删除。

注意：本脚本直接用 config 的 DB_URI 建裸 engine，不 import app——
app 导入即触发启动钩子查库，而 admin_tag 列尚不存在会 1054（先有鸡后有蛋）。

前置校验（等价性）：收敛前 user_mode=='admin' 必须与 role ∈ {super_admin,teacher}
完全一致（历史双写不变量）。不一致则列出差异行并中止，人工裁定后加 --force 重跑。

部署顺序：先停后端 → 执行本迁移 → 以新代码启动后端（旧代码仍读 user_mode，不可混跑）。

用法（项目根目录）：
    python scripts/migrate/migrate_10_identity.py          # 校验+执行
    python scripts/migrate/migrate_10_identity.py --force  # 跳过等价性中止
回滚：按脚本末尾输出的 SQL 执行（恢复 role 用 role_v1_backup）。
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

import config  # noqa: E402
from sqlalchemy import create_engine, text, inspect  # noqa: E402

FORCE = '--force' in sys.argv

engine = create_engine(config.SQLALCHEMY_DATABASE_URI)
insp = inspect(engine)

with engine.connect() as conn:
    cols = {c['name'] for c in insp.get_columns('user')}

    # ── 0. 快照 ──
    total_before = conn.execute(text("SELECT COUNT(*) FROM `user`")).scalar()
    print(f"[i] 迁移前 user 总数: {total_before}")

    # ── 1. 等价性校验（把「历史巧合正确」变成「已验证正确」）──
    if 'user_mode' in cols:
        mismatch = conn.execute(text(
            "SELECT id, email, role, user_mode FROM `user` "
            "WHERE (user_mode='admin') <> (role IN ('super_admin','teacher'))"
        )).fetchall()
        if mismatch:
            print(f"[!] 等价性校验失败：{len(mismatch)} 行 role 与 user_mode 不一致：")
            for r in mismatch[:20]:
                print(f"    id={r[0]} email={r[1]} role={r[2]} user_mode={r[3]}")
            if not FORCE:
                print("[x] 中止。人工裁定这些行后，加 --force 重跑。")
                sys.exit(1)
            print("[!] --force：跳过中止，继续执行")
        else:
            print("[ok] 等价性校验通过：user_mode=='admin' ⇔ role∈{super_admin,teacher}")

    # ── 2. DDL：回滚保险列 + admin_tag ──
    if 'role_v1_backup' not in cols:
        conn.execute(text(
            "ALTER TABLE `user` ADD COLUMN `role_v1_backup` VARCHAR(20) NULL"
        ))
        conn.commit()
        print("[+] user.role_v1_backup 已添加（回滚保险，下个大版本删除）")
    else:
        print("[=] user.role_v1_backup 已存在")

    if 'admin_tag' not in cols:
        conn.execute(text(
            "ALTER TABLE `user` ADD COLUMN `admin_tag` VARCHAR(20) NULL "
            "COMMENT 'super_admin 内部标签 teacher/developer，仅审计展示'"
        ))
        conn.commit()
        print("[+] user.admin_tag 已添加")
    else:
        print("[=] user.admin_tag 已存在")

    # ── 3. DML：备份 → 角色刷新 → admin_tag 默认 ──
    conn.execute(text(
        "UPDATE `user` SET role_v1_backup = role WHERE role_v1_backup IS NULL"
    ))
    r1 = conn.execute(text(
        "UPDATE `user` SET role='super_admin' WHERE role IN ('super_admin','teacher')"
    ))
    r2 = conn.execute(text(
        "UPDATE `user` SET role='user' WHERE role NOT IN ('super_admin','user')"
    ))
    # 老库管理员默认视为老师；开发人员用 PUT /admin/users/<id>/role 调整标签
    r3 = conn.execute(text(
        "UPDATE `user` SET admin_tag='teacher' WHERE role='super_admin' AND admin_tag IS NULL"
    ))
    conn.commit()
    print(f"[~] 备份 role → role_v1_backup；teacher→super_admin {r1.rowcount} 行，"
          f"mentor/student→user {r2.rowcount} 行，admin_tag 默认 teacher {r3.rowcount} 行")

    # ── 4. 删除 user_mode 列 ──
    if 'user_mode' in cols:
        conn.execute(text("ALTER TABLE `user` DROP COLUMN `user_mode`"))
        conn.commit()
        print("[-] user.user_mode 已删除")
    else:
        print("[=] user.user_mode 不存在（已删），跳过")

    # ── 5. post-check ──
    total_after = conn.execute(text("SELECT COUNT(*) FROM `user`")).scalar()
    rows = conn.execute(text(
        "SELECT role, COUNT(*) FROM `user` GROUP BY role"
    )).fetchall()
    dist = {r[0]: r[1] for r in rows}
    bad = set(dist.keys()) - {'super_admin', 'user'}
    assert total_after == total_before, f"行数变化！{total_before} → {total_after}"
    assert not bad, f"存在非法 role 取值: {bad}"
    print(f"[ok] post-check：总数一致（{total_after}），role 分布 {dist}")

    print()
    print("[rollback] 如需回滚（先停后端，恢复旧代码后执行）：")
    print("  ALTER TABLE `user` ADD COLUMN `user_mode` VARCHAR(20) DEFAULT 'user';")
    print("  UPDATE `user` SET user_mode='admin' WHERE role_v1_backup IN ('super_admin','teacher');")
    print("  UPDATE `user` SET user_mode='user'   WHERE role_v1_backup IN ('mentor','student');")
    print("  UPDATE `user` SET role=role_v1_backup;")
    print("[done] migrate_10_identity 完成")

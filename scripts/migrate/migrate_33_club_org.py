"""迁移 33：社团身份体系·职位/组别解耦三表 + 存量转换（2026-09-15，幂等）。

设计方案：BME_platform_frontend/docs/社团身份体系-设计方案.md v1.2
- 建 club_group（组树，种子=定稿 16 组）/ club_position（职位，种子 5 条，规则转字段）/
  club_membership（全员归属 primary/secondary 两槽）
- club_officer 加 title_id/group_id（FK 真相源），title/department 冗余双写过渡（Phase C 退役）
- 存量回填：title/department 名 → id（含 赛事组→竞赛组、游学组→科普游学组 规范化）
- 组员头衔退役：active 组员行 → club_membership（首行 primary、次行 secondary），任职行置 ended 留痕

用法：./.venv/bin/python scripts/migrate/migrate_33_club_org.py
回滚：DROP TABLE club_membership; DROP TABLE club_position; DROP TABLE club_group;
      ALTER TABLE club_officer DROP COLUMN title_id, DROP COLUMN group_id;
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

import config  # noqa: E402
from sqlalchemy import create_engine, text, inspect  # noqa: E402

TABLES = {
    'club_group': """
        CREATE TABLE club_group (
            id INT AUTO_INCREMENT PRIMARY KEY,
            name VARCHAR(50) NOT NULL,
            parent_id INT NULL,
            sort_order INT NOT NULL DEFAULT 0,
            status VARCHAR(20) NOT NULL DEFAULT 'active',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY uq_club_group_name (name),
            INDEX ix_club_group_parent (parent_id),
            INDEX ix_club_group_status (status),
            CONSTRAINT fk_club_group_parent FOREIGN KEY (parent_id) REFERENCES club_group(id)
        ) CHARSET=utf8mb4
    """,
    'club_position': """
        CREATE TABLE club_position (
            id INT AUTO_INCREMENT PRIMARY KEY,
            name VARCHAR(30) NOT NULL,
            sort_rank INT NOT NULL DEFAULT 99,
            badge_tier INT NOT NULL DEFAULT 3,
            badge_with_group TINYINT(1) NOT NULL DEFAULT 0,
            group_rule VARCHAR(20) NOT NULL DEFAULT 'optional',
            per_group_limit INT NOT NULL DEFAULT 1,
            global_limit INT NOT NULL DEFAULT 0,
            status VARCHAR(20) NOT NULL DEFAULT 'active',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY uq_club_position_name (name),
            INDEX ix_club_position_status (status)
        ) CHARSET=utf8mb4
    """,
    'club_membership': """
        CREATE TABLE club_membership (
            id INT AUTO_INCREMENT PRIMARY KEY,
            user_id INT NOT NULL,
            group_id INT NOT NULL,
            slot VARCHAR(20) NOT NULL,
            joined_at DATE NOT NULL,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY uq_club_membership_user_slot (user_id, slot),
            INDEX ix_club_membership_group (group_id),
            CONSTRAINT fk_club_membership_user FOREIGN KEY (user_id) REFERENCES user(id),
            CONSTRAINT fk_club_membership_group FOREIGN KEY (group_id) REFERENCES club_group(id)
        ) CHARSET=utf8mb4
    """,
}

# 种子组树（定稿 §1.1）：name, parent_name, sort_order
GROUPS = [
    ('成员发展组', None, 1),
    ('运行保障组', None, 2),
    ('项目运营组', None, 3),
    ('行业交流组', None, 4),
    ('品牌建设组', None, 5),
    ('临床调研组', None, 6),
    ('培训组', '项目运营组', 1),
    ('项目组', '项目运营组', 2),
    ('竞赛组', '项目运营组', 3),
    ('科普游学组', '项目运营组', 4),
    ('文宣组', '品牌建设组', 1),
    ('活动组', '品牌建设组', 2),
    ('硬件组', '培训组', 1),
    ('软件组', '培训组', 2),
    ('先进制造组', '培训组', 3),
    ('柔性电子组', '培训组', 4),
]

# 种子职位（定稿 §1.2）：name, rank, badge_tier, badge_with_group, group_rule, per_group_limit, global_limit
POSITIONS = [
    ('社长', 1, 1, 0, 'forbidden', 1, 1),
    ('副社长', 2, 1, 1, 'required', 1, 3),
    ('团支书', 3, 1, 1, 'required', 1, 1),
    ('副团支书', 4, 1, 1, 'required', 1, 1),
    ('组长', 10, 2, 1, 'required', 1, 0),
]

# 存量 department 规范化（前端硬编码漂移订正，§0.1）
DEPT_NORMALIZE = {'赛事组': '竞赛组', '游学组': '科普游学组'}

engine = create_engine(config.SQLALCHEMY_DATABASE_URI)
insp = inspect(engine)

with engine.connect() as conn:
    # 1. 建表
    for table, ddl in TABLES.items():
        if insp.has_table(table):
            print(f"[=] {table} 已存在")
        else:
            conn.execute(text(ddl))
            conn.commit()
            print(f"[+] {table} 已创建")

    # 2. club_officer 加引用列（真相源）
    officer_cols = {c['name'] for c in insp.get_columns('club_officer')}
    for col, ddl in [
        ('title_id', "ALTER TABLE club_officer ADD COLUMN title_id INT NULL"),
        ('group_id', "ALTER TABLE club_officer ADD COLUMN group_id INT NULL"),
    ]:
        if col not in officer_cols:
            conn.execute(text(ddl))
            conn.commit()
            print(f"[+] club_officer.{col} 已添加")
        else:
            print(f"[=] club_officer.{col} 已存在")

    # 3. 种子组树（按父名解析，两级循环保证父先插）
    group_ids = {}
    for name, parent, sort in GROUPS:
        if parent and parent not in group_ids:
            row = conn.execute(text("SELECT id FROM club_group WHERE name=:n"), {'n': parent}).fetchone()
            if row:
                group_ids[parent] = row[0]
        exists = conn.execute(text("SELECT id FROM club_group WHERE name=:n"), {'n': name}).fetchone()
        if exists:
            group_ids[name] = exists[0]
            continue
        conn.execute(text(
            "INSERT INTO club_group (name, parent_id, sort_order) VALUES (:n, :p, :s)"),
            {'n': name, 'p': group_ids.get(parent), 's': sort})
        conn.commit()
        group_ids[name] = conn.execute(
            text("SELECT id FROM club_group WHERE name=:n"), {'n': name}).fetchone()[0]
        print(f"[+] 组 {name} (id={group_ids[name]})")

    # 4. 种子职位
    for name, rank, tier, with_group, rule, per_group, global_limit in POSITIONS:
        exists = conn.execute(text("SELECT id FROM club_position WHERE name=:n"), {'n': name}).fetchone()
        if exists:
            print(f"[=] 职位 {name} 已存在")
            continue
        conn.execute(text(
            "INSERT INTO club_position (name, sort_rank, badge_tier, badge_with_group, group_rule, "
            "per_group_limit, global_limit) VALUES (:n, :r, :t, :w, :g, :pg, :gl)"),
            {'n': name, 'r': rank, 't': tier, 'w': with_group, 'g': rule,
             'pg': per_group, 'gl': global_limit})
        conn.commit()
        print(f"[+] 职位 {name}")

    # 5. 存量任职回填 title_id / group_id
    rows = conn.execute(text(
        "SELECT id, title, department FROM club_officer WHERE title_id IS NULL")).fetchall()
    pos_ids = {r[1]: r[0] for r in conn.execute(text("SELECT id, name FROM club_position")).fetchall()}
    misses = []
    for oid, title, dept in rows:
        norm_dept = DEPT_NORMALIZE.get(dept, dept)
        tid = pos_ids.get(title)
        gid = group_ids.get(norm_dept) if norm_dept else None
        if title != '组员' and not tid:
            misses.append(f"#{oid} 职位「{title}」命中不到职位表")
        if norm_dept and not gid:
            misses.append(f"#{oid} 组「{norm_dept}」命中不到组表")
        conn.execute(text(
            "UPDATE club_officer SET title_id=:t, group_id=:g WHERE id=:i"),
            {'t': tid, 'g': gid, 'i': oid})
    conn.commit()
    print(f"[+] 任职回填 {len(rows)} 行" + (f"；未命中：{misses}" if misses else ""))
    for m in misses:
        print(f"    [!] {m}")

    # 6. 组员头衔退役：active 组员行 → membership（同人首行 primary、次行 secondary），行置 ended
    members = conn.execute(text(
        "SELECT id, user_id, group_id FROM club_officer "
        "WHERE status='active' AND title='组员' ORDER BY user_id, id")).fetchall()
    converted = 0
    for oid, uid, gid in members:
        if not gid:
            conn.execute(text(
                "UPDATE club_officer SET status='ended', term_end=CURDATE(), "
                "end_reason='组员制退役迁移（组未命中不转归属）' WHERE id=:i"), {'i': oid})
            converted += 1
            continue
        slot_row = conn.execute(text(
            "SELECT slot FROM club_membership WHERE user_id=:u"), {'u': uid}).fetchone()
        slot = None
        if not slot_row:
            slot = 'primary'
        elif len(conn.execute(text(
                "SELECT slot FROM club_membership WHERE user_id=:u")).fetchall()) == 1:
            slot = 'secondary'
        if slot:
            try:
                conn.execute(text(
                    "INSERT INTO club_membership (user_id, group_id, slot, joined_at) "
                    "VALUES (:u, :g, :s, CURDATE())"), {'u': uid, 'g': gid, 's': slot})
            except Exception:
                pass  # 唯一键兜底（重跑/超两行场景）
        conn.execute(text(
            "UPDATE club_officer SET status='ended', term_end=CURDATE(), "
            "end_reason='组员制退役迁移' WHERE id=:i"), {'i': oid})
        converted += 1
    conn.commit()
    print(f"[+] 组员退役转换 {converted} 行（membership 承接）")

    print("[done] migrate_33 完成")

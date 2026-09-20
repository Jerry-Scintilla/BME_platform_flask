"""迁移 47：营期课程分配表 CampCourseAssignment（2026-09-20 B2，幂等）。

《通知方案》§3.4：营期维度入课事实与 UserCourse（用户×课程全局唯一）解耦——
同课跨营不再覆盖 UserCourse.camp_session_id（legacy 戳停写、读端 assignment 优先）。

回填（方案 §13.3，双源尽力回填，不伪造精确历史）：
  A. 方向源（高置信）：营成员（含 removed/exited）有 team_mentor_id → 导生名片
     tags[0] → 营 ms_tags 方向的 course_ids → source='direction'，ref=导生 user_id；
  B. 营戳源：user_course.camp_session_id 有戳且 (营,人) 有成员行、A 未覆盖 →
     source='migrated'、ref=NULL（unknown——历史来源已不可考）。
  成员 status!=active → 分配行 ended（ended_at 取成员 ended_at）；课程学习行不动。
幂等：UQ(camp,student,course) 冲突即跳过（INSERT IGNORE）。

用法：python scripts/migrate/migrate_47_camp_course_assignment.py
回滚：DROP TABLE camp_course_assignment;（UserCourse 营戳未动，直接回退无残留）
"""
import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

import config  # noqa: E402
from sqlalchemy import create_engine, text, inspect  # noqa: E402

TABLES = {
    'camp_course_assignment': """
        CREATE TABLE camp_course_assignment (
            id INT AUTO_INCREMENT PRIMARY KEY,
            camp_session_id INT NOT NULL COMMENT '营期',
            student_user_id INT NOT NULL COMMENT '学员',
            course_id INT NOT NULL COMMENT '课程',
            source_type VARCHAR(20) NOT NULL DEFAULT 'direction'
                COMMENT 'direction=方向继承 / manual=手动 / migrated=迁移回填',
            source_ref_id INT NULL COMMENT 'direction→导生 / manual→操作人 / migrated→NULL',
            status VARCHAR(20) NOT NULL DEFAULT 'active' COMMENT 'active / ended（成员移除即 ended）',
            assigned_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            ended_at DATETIME NULL,
            UNIQUE KEY uq_camp_course_assignment (camp_session_id, student_user_id, course_id),
            INDEX ix_cca_student_course (student_user_id, course_id, status),
            INDEX ix_cca_session (camp_session_id),
            CONSTRAINT fk_cca_session FOREIGN KEY (camp_session_id) REFERENCES camp_session(id),
            CONSTRAINT fk_cca_student FOREIGN KEY (student_user_id) REFERENCES user(id),
            CONSTRAINT fk_cca_course FOREIGN KEY (course_id) REFERENCES course.id)
        ) CHARSET=utf8mb4
    """,
}

# 与 camp_ms.MS_DEFAULT_TAGS 一致（迁移脚本不 import 应用包，就地复制）
MS_DEFAULT_TAGS = ["硬件组", "软件组", "深度学习", "机械设计", "其他"]

engine = create_engine(config.SQLALCHEMY_DATABASE_URI)
insp = inspect(engine)


def parse_directions(ms_tags):
    """复刻 camp_ms._ms_directions_raw 的形状归一（dict/单课/字符串三形状）。
    课程存在性过滤在调用方按 course 表集合做（不依赖 ORM）。"""
    if not ms_tags:
        return list(MS_DEFAULT_TAGS)
    try:
        tags = json.loads(ms_tags)
    except (ValueError, TypeError):
        return list(MS_DEFAULT_TAGS)
    if not (isinstance(tags, list) and tags):
        return list(MS_DEFAULT_TAGS)
    out = []
    for t in tags[:20]:
        if isinstance(t, dict) and str(t.get("name") or "").strip():
            cids = t.get("course_ids")
            if not isinstance(cids, list):
                cids = [t.get("course_id")] if t.get("course_id") is not None else []
            out.append({"name": str(t["name"]).strip(), "course_ids": cids})
        elif isinstance(t, str) and t.strip():
            out.append(t.strip())
    return out or list(MS_DEFAULT_TAGS)


def backfill(conn):
    """双源回填，返回 (direction 行数, migrated 行数)。"""
    course_ids = {r[0] for r in conn.execute(text("SELECT id FROM course"))}
    camps = conn.execute(text(
        "SELECT id, ms_tags, created_at FROM camp_session")).fetchall()
    members = conn.execute(text(
        "SELECT camp_session_id, user_id, role, team_mentor_id, status, ended_at "
        "FROM camp_member")).fetchall()
    profiles = conn.execute(text(
        "SELECT camp_session_id, user_id, tags FROM camp_mentor_profile")).fetchall()
    # (camp, student, course) 已有分配行 → 跳过（幂等）
    existing = {(r[0], r[1], r[2]) for r in conn.execute(text(
        "SELECT camp_session_id, student_user_id, course_id "
        "FROM camp_course_assignment"))}

    ins = text(
        "INSERT IGNORE INTO camp_course_assignment "
        "(camp_session_id, student_user_id, course_id, source_type, source_ref_id, "
        " status, assigned_at, ended_at) "
        "VALUES (:sid, :uid, :cid, :st, :ref, :status, "
        " COALESCE(:assigned_at, NOW()), :ended_at)")

    def status_of(m):
        # 成员已 removed/exited → 分配 ended（ended_at 取成员行；无值兜底 NOW）
        if m[4] and m[4] != 'active':
            return 'ended', (m[5] or None)
        return 'active', None

    n_dir = n_mig = 0
    member_by_id = {(m[0], m[1]): m for m in members}

    # A. 方向源：学员归属导生 → 导生名片 tags[0] → 方向课程
    # assigned_at 用营 created_at 作时序代理（真实归属时间已不可考；同课跨营时
    # active_scope_camp 按 assigned_at 最新优先，营创建序≈入课序比统一 NOW() 忠实）
    directions_by_camp, camp_created = {}, {}
    for camp_id, ms_tags, created_at in camps:
        dirs = {}
        for d in parse_directions(ms_tags):
            name = d["name"] if isinstance(d, dict) else d
            cids = d.get("course_ids") if isinstance(d, dict) else None
            clean = []
            for cid in (cids or []):
                try:
                    cid = int(cid)
                except (ValueError, TypeError):
                    continue
                if cid in course_ids:
                    clean.append(cid)
            dirs[name] = clean
        directions_by_camp[camp_id] = dirs
        camp_created[camp_id] = created_at
    profile_tags = {(p[0], p[1]): p[2] for p in profiles}
    for m in members:
        if m[2] != 'student' or not m[3]:
            continue
        tags_raw = profile_tags.get((m[0], m[3]))
        if not tags_raw:
            continue
        try:
            tags = json.loads(tags_raw)
        except (ValueError, TypeError):
            continue
        if not (isinstance(tags, list) and tags):
            continue
        cids = directions_by_camp.get(m[0], {}).get(str(tags[0]))
        if not cids:
            continue
        st, ended_at = status_of(m)
        for cid in cids:
            if (m[0], m[1], cid) in existing:
                continue
            conn.execute(ins, {"sid": m[0], "uid": m[1], "cid": cid,
                               "st": 'direction', "ref": m[3], "status": st,
                               "assigned_at": camp_created.get(m[0]), "ended_at": ended_at})
            existing.add((m[0], m[1], cid))
            n_dir += 1

    # B. 营戳源：user_course 营戳 + 成员行存在 + A 未覆盖 → migrated/unknown
    for uc in conn.execute(text(
            "SELECT user_id, course_id, camp_session_id, enroll_time "
            "FROM user_course WHERE camp_session_id IS NOT NULL")).fetchall():
        uid, cid, sid, enroll_time = uc
        m = member_by_id.get((sid, uid))
        if m is None or (sid, uid, cid) in existing:
            continue                      # 无成员行（脏戳）或已有分配：不动
        st, ended_at = status_of(m)
        conn.execute(ins, {"sid": sid, "uid": uid, "cid": cid,
                           "st": 'migrated', "ref": None, "status": st,
                           "assigned_at": enroll_time, "ended_at": ended_at})
        existing.add((sid, uid, cid))
        n_mig += 1
    # 自愈：历史版本迁移产生的 assigned_at IS NULL 行（显式 NULL 绕过列 DEFAULT）
    # 会破坏「最新分配优先」排序——统一补 NOW()（回填时间，不伪造更早历史）
    fixed = conn.execute(text(
        "UPDATE camp_course_assignment SET assigned_at = NOW() "
        "WHERE assigned_at IS NULL")).rowcount
    if fixed:
        print(f"[+] 修正 assigned_at 为 NULL 的存量行 {fixed} 条")
    conn.commit()
    return n_dir, n_mig


with engine.connect() as conn:
    for table, ddl in TABLES.items():
        if insp.has_table(table):
            print(f"[=] {table} 已存在")
        else:
            conn.execute(text(ddl))
            conn.commit()
            print(f"[+] {table} 已创建")
    n_dir, n_mig = backfill(conn)
    print(f"[+] 回填完成：方向源 {n_dir} 行，营戳源(migrated) {n_mig} 行")
    print("[done] migrate_47 完成")

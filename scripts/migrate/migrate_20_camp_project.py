"""迁移 20：项目营组织与申报组队表组（营期升级阶段 3 / 设计方案 v1.3，幂等）。

设计（frontend/docs/营期升级重构-设计方案.md §3.7/§3.8，v1.3 修订）：
- CampUnit / ProjectProfile / ProjectApplicationVersion / CampUnitMember /
  CampMembershipEvent / CampProjectPreference 六表（阶段3 组织与申报组队）。
- CampPolicy + capabilities（JSON 能力位图：attendance/leave/seat；learning 全开、project 首期全关），
  存量策略行按所属营 category 回填，行上 NULL=按类型默认值（代码侧兜底合并）。
- 不迁移学习营现役数据（mentor_team 首期不落本表；统一 roster API 建成 unit 通用型，
  学习营迁移窗口=下个学习营开营前，v1.3 §3.8 注）。

动作：
1. 建六张新表（存在即跳过）
2. camp_policy + capabilities 列（存在即跳过）
3. 存量策略行按营 category 回填 capabilities 默认值

注意：默认值与 models.CAMP_CATEGORY_DEFAULTS 保持一致——改默认值请两处同步。

用法：python scripts/migrate/migrate_20_camp_project.py
回滚：DROP 六表 + ALTER TABLE camp_policy DROP COLUMN capabilities
"""
import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

import config  # noqa: E402
from sqlalchemy import create_engine, text, inspect  # noqa: E402

# 与 models.CAMP_CATEGORY_DEFAULTS 同步（本脚本不 import app 侧代码，避免触发启动钩子）
CAPABILITY_DEFAULTS = {
    'learning': {'attendance': True, 'leave': True, 'seat': True},
    'project': {'attendance': False, 'leave': False, 'seat': False},
}

TABLES = {
    'camp_unit': """
        CREATE TABLE camp_unit (
            id INT AUTO_INCREMENT PRIMARY KEY,
            camp_session_id INT NOT NULL COMMENT '营期（camp_session.id）',
            unit_type VARCHAR(20) NOT NULL DEFAULT 'project' COMMENT 'mentor_team / project',
            name VARCHAR(100) NOT NULL COMMENT '单元名（项目名）',
            status VARCHAR(20) NOT NULL DEFAULT 'active' COMMENT 'active / paused / terminated',
            visibility VARCHAR(20) NOT NULL DEFAULT 'camp' COMMENT 'camp 营内 / public 可发布到项目展示平台',
            owner_user_id INT NOT NULL COMMENT '负责人（user.id）',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            INDEX ix_unit_camp (camp_session_id),
            INDEX ix_unit_owner (owner_user_id),
            UNIQUE KEY uq_unit_camp_type_name (camp_session_id, unit_type, name),
            CONSTRAINT fk_unit_camp FOREIGN KEY (camp_session_id) REFERENCES camp_session(id),
            CONSTRAINT fk_unit_owner FOREIGN KEY (owner_user_id) REFERENCES user(id)
        ) CHARSET=utf8mb4
    """,
    'project_profile': """
        CREATE TABLE project_profile (
            id INT AUTO_INCREMENT PRIMARY KEY,
            unit_id INT NOT NULL COMMENT '项目单元（camp_unit.id，1:1）',
            background TEXT NULL COMMENT '项目背景',
            goal TEXT NULL COMMENT '目标',
            required_abilities TEXT NULL COMMENT '所需能力',
            recruit_note TEXT NULL COMMENT '招募说明',
            plan TEXT NULL COMMENT '计划',
            visibility VARCHAR(20) NOT NULL DEFAULT 'camp',
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY uq_profile_unit (unit_id),
            CONSTRAINT fk_profile_unit FOREIGN KEY (unit_id) REFERENCES camp_unit(id)
        ) CHARSET=utf8mb4
    """,
    'project_application_version': """
        CREATE TABLE project_application_version (
            id INT AUTO_INCREMENT PRIMARY KEY,
            camp_session_id INT NOT NULL COMMENT '营期',
            unit_id INT NULL COMMENT '过审时回填的单元（camp_unit.id）',
            version INT NOT NULL DEFAULT 1 COMMENT '版本号（退回重提递增）',
            submitted_by INT NOT NULL COMMENT '提交人（=负责人本人）',
            leader_user_id INT NOT NULL COMMENT '负责人（user.id）',
            name VARCHAR(100) NOT NULL COMMENT '项目名（快照）',
            background TEXT NULL, goal TEXT NULL, required_abilities TEXT NULL,
            recruit_note TEXT NULL, plan TEXT NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'pending' COMMENT 'pending / approved / rejected',
            reject_reason VARCHAR(500) NULL,
            reviewed_by INT NULL, reviewed_at DATETIME NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            INDEX ix_pav_camp (camp_session_id),
            INDEX ix_pav_unit (unit_id),
            INDEX ix_pav_leader (leader_user_id),
            INDEX ix_pav_status (status),
            UNIQUE KEY uq_pav_camp_leader_ver (camp_session_id, leader_user_id, version),
            CONSTRAINT fk_pav_camp FOREIGN KEY (camp_session_id) REFERENCES camp_session(id),
            CONSTRAINT fk_pav_unit FOREIGN KEY (unit_id) REFERENCES camp_unit(id),
            CONSTRAINT fk_pav_submitter FOREIGN KEY (submitted_by) REFERENCES user(id),
            CONSTRAINT fk_pav_leader FOREIGN KEY (leader_user_id) REFERENCES user(id)
        ) CHARSET=utf8mb4
    """,
    'camp_unit_member': """
        CREATE TABLE camp_unit_member (
            id INT AUTO_INCREMENT PRIMARY KEY,
            unit_id INT NOT NULL COMMENT '单元（camp_unit.id）',
            user_id INT NOT NULL,
            role VARCHAR(20) NOT NULL DEFAULT 'member' COMMENT 'leader / member',
            status VARCHAR(20) NOT NULL DEFAULT 'active' COMMENT 'active / ended（变更行状态化，不物理删）',
            started_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            ended_at DATETIME NULL,
            INDEX ix_um_unit (unit_id),
            INDEX ix_um_user (user_id),
            UNIQUE KEY uq_unit_member (unit_id, user_id),
            CONSTRAINT fk_um_unit FOREIGN KEY (unit_id) REFERENCES camp_unit(id),
            CONSTRAINT fk_um_user FOREIGN KEY (user_id) REFERENCES user(id)
        ) CHARSET=utf8mb4
    """,
    'camp_membership_event': """
        CREATE TABLE camp_membership_event (
            id INT AUTO_INCREMENT PRIMARY KEY,
            camp_session_id INT NOT NULL,
            unit_id INT NOT NULL,
            user_id INT NOT NULL COMMENT '被变更人',
            action VARCHAR(30) NOT NULL COMMENT 'select/deselect/exit/remove/adjust/leader_change/unit_status',
            source VARCHAR(30) NOT NULL DEFAULT 'leader_pick' COMMENT 'leader_pick/admin_adjust/apply/approve',
            operator_id INT NOT NULL COMMENT '操作人',
            before TEXT NULL COMMENT 'JSON 变更前',
            after TEXT NULL COMMENT 'JSON 变更后',
            reason VARCHAR(500) NULL COMMENT '管理员通道原因必填（H-005）',
            occurred_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            INDEX ix_me_camp (camp_session_id),
            INDEX ix_me_unit (unit_id),
            INDEX ix_me_user (user_id),
            CONSTRAINT fk_me_camp FOREIGN KEY (camp_session_id) REFERENCES camp_session(id),
            CONSTRAINT fk_me_unit FOREIGN KEY (unit_id) REFERENCES camp_unit(id)
        ) CHARSET=utf8mb4
    """,
    'camp_project_preference': """
        CREATE TABLE camp_project_preference (
            id INT AUTO_INCREMENT PRIMARY KEY,
            camp_session_id INT NOT NULL,
            student_user_id INT NOT NULL,
            unit_id INT NOT NULL COMMENT '意向项目（camp_unit.id，须为过审项目）',
            rank INT NOT NULL COMMENT '1-3 有序',
            note VARCHAR(200) NULL COMMENT '学员可选留言',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            INDEX ix_pp_camp (camp_session_id),
            INDEX ix_pp_student (student_user_id),
            INDEX ix_pp_unit (unit_id),
            UNIQUE KEY uq_pp_rank (camp_session_id, student_user_id, rank),
            UNIQUE KEY uq_pp_unit (camp_session_id, student_user_id, unit_id),
            CONSTRAINT fk_pp_camp FOREIGN KEY (camp_session_id) REFERENCES camp_session(id),
            CONSTRAINT fk_pp_unit FOREIGN KEY (unit_id) REFERENCES camp_unit(id)
        ) CHARSET=utf8mb4
    """,
}

engine = create_engine(config.SQLALCHEMY_DATABASE_URI)
insp = inspect(engine)

with engine.connect() as conn:
    # 1) 六张新表
    for table, ddl in TABLES.items():
        if insp.has_table(table):
            print(f"[=] {table} 已存在")
            continue
        conn.execute(text(ddl))
        conn.commit()
        print(f"[+] {table} 已创建")

    # 2) camp_policy.capabilities
    pcols = {c['name'] for c in insp.get_columns('camp_policy')}
    if 'capabilities' not in pcols:
        conn.execute(text(
            "ALTER TABLE camp_policy ADD COLUMN capabilities TEXT NULL "
            "COMMENT 'JSON 能力位图 attendance/leave/seat；NULL=按类型默认值'"))
        conn.commit()
        print("[+] camp_policy.capabilities 已添加")
    else:
        print("[=] camp_policy.capabilities 已存在")

    # 3) 存量策略行按所属营 category 回填默认值（仅填 NULL 行）
    rows = conn.execute(text(
        "SELECT p.id, s.category FROM camp_policy p "
        "JOIN camp_session s ON s.policy_id = p.id "
        "WHERE p.capabilities IS NULL")).fetchall()
    for pid, category in rows:
        caps = CAPABILITY_DEFAULTS.get(category or 'learning', CAPABILITY_DEFAULTS['learning'])
        conn.execute(text(
            "UPDATE camp_policy SET capabilities = :caps WHERE id = :pid"),
            {"caps": json.dumps(caps), "pid": pid})
        conn.commit()
        print(f"[~] 策略行 {pid}（{category}）capabilities 已回填 {caps}")
    if not rows:
        print("[=] 无待回填策略行")

    left = conn.execute(text(
        "SELECT COUNT(*) FROM camp_policy p JOIN camp_session s ON s.policy_id = p.id "
        "WHERE p.capabilities IS NULL")).scalar()
    print(f"[ok] 未回填 capabilities 的已挂策略行：{left}（应为 0；无营引用的策略行不算）")
    print("[done] migrate_20 完成")

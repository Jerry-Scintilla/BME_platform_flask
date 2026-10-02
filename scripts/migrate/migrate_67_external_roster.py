"""迁移 67：D3c 外校名册路径 + 恢复申诉骨架（规格 5.2/7.4，2026-10-02）。

新建 3 表 + identity_application 加 1 列：
  identity_roster          外校名册条目：UNIQUE(school_id, roster_ref)——批次可变、
                           人员不因新批次重建（upsert）；认领后绑定 claimed_person_id
                           （后续来访/新批次找回同一 Person）
  identity_roster_invite   认领邀请：一次使用、7 天有效、绑定具体条目与联系方式；
                           领取前验证目标邮箱控制（转发邀请不构成核验成功）
  identity_recovery_case   恢复案例骨架：丢失账号/因素的人工恢复+冷静期
                           （24h；特权/争议 72h 双人复核）——执行动作走既有运维通道，
                           本表承载状态机与审计
  identity_application.roster_id  名册认领申请回链（method='roster'）

种子：external:scuec 中南民族大学——无个人域清单（外校不自动域校验，走名册/
人工+邮箱控制证明）、无 NetID 映射、审核人 dev 74 占位。

用法（项目根）：.venv/bin/python scripts/migrate/migrate_67_external_roster.py
回滚：ALTER TABLE identity_application DROP COLUMN roster_id;
      DROP TABLE identity_recovery_case, identity_roster_invite, identity_roster;
      DELETE FROM identity_school_config WHERE school_id='external:scuec';
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

import config                # noqa: E402
from sqlalchemy import create_engine, text, inspect  # noqa: E402

engine = create_engine(config.SQLALCHEMY_DATABASE_URI)
insp = inspect(engine)

NEW_TABLES = {
    "identity_roster": """
        CREATE TABLE identity_roster (
          id INT AUTO_INCREMENT PRIMARY KEY,
          school_id VARCHAR(32) NOT NULL COMMENT 'external:<code>，FK 学校配置',
          roster_ref VARCHAR(64) NOT NULL COMMENT '稳定人员引用码（跨批次不变）',
          name VARCHAR(100) NOT NULL,
          contact_email VARCHAR(191) NOT NULL COMMENT '已确认联系方式（域小写归一）',
          institution_id VARCHAR(64) NULL COMMENT '机构标识/学号（可选）',
          owner_user_id INT NOT NULL COMMENT '导入负责人',
          scope_note VARCHAR(255) NULL COMMENT '适用营期/范围说明',
          claimed_person_id INT NULL COMMENT '认领并核验通过后绑定（后续找回同一人）',
          claimed_at DATETIME NULL,
          status VARCHAR(20) NOT NULL DEFAULT 'active' COMMENT 'active/retired',
          created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
          UNIQUE KEY uq_roster_ref (school_id, roster_ref),
          KEY idx_roster_claim (claimed_person_id),
          CONSTRAINT fk_roster_school FOREIGN KEY (school_id)
            REFERENCES identity_school_config (school_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='外校名册（负责人导入）'
    """,
    "identity_roster_invite": """
        CREATE TABLE identity_roster_invite (
          id VARCHAR(64) PRIMARY KEY COMMENT '一次性令牌（uuid4.hex，随邮件链接发出）',
          roster_id INT NOT NULL,
          issued_by INT NOT NULL,
          expires_at DATETIME NOT NULL COMMENT '7 天；过期可人工重发（新令牌）',
          used_at DATETIME NULL COMMENT '一次使用',
          created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
          KEY idx_rinv_roster (roster_id, used_at),
          CONSTRAINT fk_rinv_roster FOREIGN KEY (roster_id) REFERENCES identity_roster (id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='外校认领邀请（绑定条目，一次使用）'
    """,
    "identity_recovery_case": """
        CREATE TABLE identity_recovery_case (
          id INT AUTO_INCREMENT PRIMARY KEY,
          kind VARCHAR(20) NOT NULL COMMENT 'account_lost/factor_lost',
          target_email VARCHAR(191) NOT NULL COMMENT '丢失的账号邮箱（不回显存在性）',
          contact_email VARCHAR(191) NOT NULL COMMENT '当前可联系的邮箱（须验控制权）',
          contact_verified_at DATETIME NULL,
          statement TEXT NOT NULL COMMENT '陈述与可核对信息（不收证件影像）',
          status VARCHAR(20) NOT NULL DEFAULT 'draft'
            COMMENT 'draft（待邮箱验证）/submitted/rejected/cooldown/done',
          require_two TINYINT(1) NOT NULL DEFAULT 0 COMMENT '特权/争议：72h+双人复核',
          cooldown_until DATETIME NULL,
          decided_by INT NULL,
          decided_at DATETIME NULL,
          decision_note VARCHAR(255) NULL,
          created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
          KEY idx_rc_status (status, created_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='账号/因素恢复案例（人工+冷静期）'
    """,
}

SEED_SCUEC = """
    INSERT INTO identity_school_config
      (school_id, name, personal_email_domains, excluded_email_domains,
       email_local_matches_identifier, reviewer_user_ids, created_at, updated_at)
    VALUES
      ('external:scuec', '中南民族大学', '[]', '[]', 0, '[74]', NOW(), NOW())
    ON DUPLICATE KEY UPDATE school_id = school_id
"""


def migrate_tables(conn):
    existing = set(insp.get_table_names())
    for table, ddl in NEW_TABLES.items():
        if table in existing:
            print(f"[=] {table} 已存在")
            continue
        conn.execute(text(ddl))
        conn.commit()
        print(f"[+] 已建表 {table}")


def add_application_column(conn):
    cols = {c['name'] for c in insp.get_columns('identity_application')}
    if 'roster_id' in cols:
        print("[=] identity_application.roster_id 已存在")
        return
    conn.execute(text(
        "ALTER TABLE identity_application ADD COLUMN roster_id INT NULL "
        "COMMENT '名册条目回链（method=roster）'"))
    conn.commit()
    print("[+] identity_application: 已加列 roster_id")


def relax_challenge_actor(conn):
    """identity_challenge.actor_user_id 放宽为可空——公开流程（恢复申诉）
    的挑战无登录者（migrate_64 原建 NOT NULL）。"""
    cols = {c['name']: c for c in insp.get_columns('identity_challenge')}
    if cols.get('actor_user_id', {}).get('nullable', True) is False:
        conn.execute(text(
            "ALTER TABLE identity_challenge MODIFY actor_user_id INT NULL "
            "COMMENT '发起者；公开流程（恢复）可空'"))
        conn.commit()
        print("[+] identity_challenge.actor_user_id 已放宽为可空（公开流程）")


def seed_scuec(conn):
    row = conn.execute(text(
        "SELECT reviewer_user_ids FROM identity_school_config "
        "WHERE school_id='external:scuec'")).first()
    if row is not None:
        print(f"[=] external:scuec 配置已存在（reviewers={row[0]}），不覆盖")
        return
    conn.execute(text(SEED_SCUEC))
    conn.commit()
    print("[+] 已种子 external:scuec 中南民族大学（无域自动校验/名册+人工路径；"
          "reviewer=dev 超管 74 占位）")


if __name__ == '__main__':
    with engine.connect() as conn:
        migrate_tables(conn)
        add_application_column(conn)
        relax_challenge_actor(conn)
        seed_scuec(conn)

    from app import app  # noqa: E402,F401
    with app.app_context():
        print("[i] 外校路径：管理端导入名册 → 发邀请（邮件令牌）→ 用户领取（邮箱控制验证）"
              "→ 负责人审批（kind=roster_ref 登记）；无名单者走人工申请（kind=email）")
        print("[i] 恢复骨架：公开提交（邮箱控制验证）→ 管理端决策（24/72h 冷静期+特权双人）"
              "→ 执行走既有运维通道（find_password/人工），状态机与审计在本表")

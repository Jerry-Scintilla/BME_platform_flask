"""迁移 65：D3b 双账号认领与归并地基（成员身份确认与重复账号安全迁移，2026-10-01）。

新建 5 表（规格 3.2/7 章）：
  account_link_case            关联案例：A/B 双账号、存续人员、案例状态机
                               （collecting→proof_ready→preview_ready→awaiting_review
                               →approved_waiting_confirmation→prepared→applied/cancelled/
                               expired/failed，规格第 4 章）
  identity_attestation         一次性证明：A 绑实际 sid（security_version 快照），
                               B 用独立凭据证明（无需 B 普通会话）；版本变化即时作废
  identity_transaction_authorization  最终预览绑定：preview_digest + 双方证明 +
                               有效期不超过两端实际认证剩余窗口（5 分钟）
  identity_case_account_lock   互斥占位：PK(user_id)，归并/主号变更统一占位，
                               到期必须经服务端校验确认可释放
  identity_review_decision     每名审核人只计一份有效批准；计划变化（plan_digest）
                               或审核资格撤销后不能沿用旧批准

用法（项目根）：.venv/bin/python scripts/migrate/migrate_65_identity_link.py
回滚（仅允许在无 applied 案例前执行；升级前先 mysqldump）：
  DROP TABLE identity_transaction_authorization, identity_review_decision,
             identity_case_account_lock, identity_attestation, account_link_case;
依赖：本迁移只加表不写数据；案例流程（挑战/证明/预览/执行归并）属 D3b 服务层。
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

import config                # noqa: E402
from sqlalchemy import create_engine, text, inspect  # noqa: E402

engine = create_engine(config.SQLALCHEMY_DATABASE_URI)
insp = inspect(engine)

NEW_TABLES = {
    "account_link_case": """
        CREATE TABLE account_link_case (
          id VARCHAR(64) PRIMARY KEY COMMENT '服务器生成随机案例 ID',
          account_a INT NOT NULL COMMENT '发起方 user id（证明所有权的一端）',
          account_b INT NOT NULL COMMENT '被认领方 user id（空壳副号/老号）',
          surviving_person_id INT NULL COMMENT '批准后确定的存续人员',
          selected_primary_user_id INT NULL COMMENT '主参与号（推荐+人工改选）',
          state VARCHAR(40) NOT NULL DEFAULT 'collecting',
          preview_digest CHAR(64) NULL COMMENT '最终预览摘要（绑定授权与审批）',
          policy_version INT NOT NULL DEFAULT 1,
          version INT NOT NULL DEFAULT 1 COMMENT '乐观锁：案例状态推进+1',
          collection_expires_at DATETIME NULL COMMENT 'collecting 24h 过期（重新建案例）',
          approval_expires_at DATETIME NULL COMMENT '人工批准待确认 7 天',
          created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
          KEY idx_alc_a (account_a, state),
          KEY idx_alc_b (account_b, state),
          KEY idx_alc_state (state, updated_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='双账号关联案例'
    """,
    "identity_attestation": """
        CREATE TABLE identity_attestation (
          id VARCHAR(64) PRIMARY KEY,
          proven_user_id INT NOT NULL COMMENT '被证明归属的账号',
          security_version_snapshot INT NOT NULL COMMENT '证明时版本；变化即作废',
          actor_sid CHAR(36) NULL COMMENT 'A 端绑定实际会话；B 端独立凭据为空',
          case_id VARCHAR(64) NOT NULL,
          purpose VARCHAR(30) NOT NULL COMMENT 'link_side_a/link_side_b',
          auth_time DATETIME NOT NULL COMMENT '实际认证时间（非签发时间）',
          amr VARCHAR(50) NOT NULL COMMENT '认证方式（pwd/pwd+totp/challenge）',
          evidence_ref VARCHAR(191) NULL COMMENT '证据引用（挑战 id 等）',
          expires_at DATETIME NOT NULL COMMENT '5 分钟，不延长',
          consumed_at DATETIME NULL COMMENT '最终提交时消费；一次性',
          created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
          KEY idx_att_case (case_id, purpose),
          KEY idx_att_user (proven_user_id, created_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='一次性归属证明（A 会话/B 独立凭据）'
    """,
    "identity_transaction_authorization": """
        CREATE TABLE identity_transaction_authorization (
          id VARCHAR(64) PRIMARY KEY,
          case_id VARCHAR(64) NOT NULL,
          preview_digest CHAR(64) NOT NULL COMMENT '最终预览的内容摘要',
          attestation_a VARCHAR(64) NOT NULL,
          attestation_b VARCHAR(64) NOT NULL COMMENT '双方证明都绑定具体预览',
          policy_version INT NOT NULL DEFAULT 1,
          expires_at DATETIME NOT NULL COMMENT '不超过两端证明剩余窗口',
          consumed_at DATETIME NULL,
          created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
          KEY idx_txa_case (case_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='最终预览的事务授权'
    """,
    "identity_case_account_lock": """
        CREATE TABLE identity_case_account_lock (
          user_id INT PRIMARY KEY COMMENT '一账号同时只在一个活跃案例',
          case_id VARCHAR(64) NOT NULL,
          expires_at DATETIME NOT NULL COMMENT '证明收集期内占位；释放须服务端校验',
          created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='归并互斥占位'
    """,
    "identity_review_decision": """
        CREATE TABLE identity_review_decision (
          id INT AUTO_INCREMENT PRIMARY KEY,
          case_id VARCHAR(64) NOT NULL,
          reviewer_user_id INT NOT NULL,
          plan_digest CHAR(64) NOT NULL COMMENT '批准所绑定的执行计划摘要',
          decision VARCHAR(20) NOT NULL COMMENT 'approved/rejected',
          scope VARCHAR(100) NULL,
          version INT NOT NULL DEFAULT 1,
          decided_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
          valid_until DATETIME NOT NULL COMMENT '7 天；计划变化即失效',
          UNIQUE KEY uq_review_one (case_id, reviewer_user_id, version),
          KEY idx_review_case (case_id, decision)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='案例审核决策（每人一份有效批准）'
    """,
}


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
        migrate_tables(conn)

    from app import app  # noqa: E402,F401
    with app.app_context():
        print("[i] D3b 五表就位；案例流程（挑战/证明/预览/执行归并/申诉）属下一批服务层")
        print("[i] 本迁移零行为变化：不加开关、不写数据")

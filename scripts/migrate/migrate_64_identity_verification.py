"""迁移 64：D3a 核验与审批地基（成员身份确认与重复账号安全迁移，2026-10-01）。

新建 3 表（规格 3.2/5.1）：
  identity_school_config  学校配置版本（个人邮箱域/共享域排除/NetID-邮箱映射规则/
                          核验负责人 reviewer_user_ids——审核人就绪度按「≥2 名」判定，
                          生产上线前由管理端补齐，dev 用 seed 号占位）
  identity_application   核验申请（draft/submitted/reviewing/approved/rejected/withdrawn；
                          自填 claimed_* 只作声明，不占用唯一身份标识——占位只发生在
                          审批通过写 person_identity 的那一刻）
  identity_challenge     独立邮箱挑战（服务器生成随机 id；短码只存专用密钥 HMAC 摘要；
                          payload_digest 绑定具体申请防跨申请挪用；一次性消费）

用法（项目根）：.venv/bin/python scripts/migrate/migrate_64_identity_verification.py
回滚（仅允许在无 approved 申请前执行；升级前先 mysqldump）：
  DROP TABLE identity_challenge, identity_application, identity_school_config;
种子：sysu 一行（个人域 mail2.sysu.edu.cn、公务域 sysu.edu.cn 排除、
  reviewer_user_ids 置 dev 超管 74 占位——生产上线前必须经管理端改为 ≥2 名真人）。
  域名清单按规格 5.1「域名必须精确匹配」维护，上线前与学校文档核对。
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

import config                # noqa: E402
from sqlalchemy import create_engine, text, inspect  # noqa: E402

engine = create_engine(config.SQLALCHEMY_DATABASE_URI)
insp = inspect(engine)

NEW_TABLES = {
    "identity_school_config": """
        CREATE TABLE identity_school_config (
          school_id VARCHAR(32) PRIMARY KEY COMMENT '如 sysu；外校名册用 external:<code>',
          name VARCHAR(100) NOT NULL,
          personal_email_domains JSON NOT NULL
            COMMENT '允许的个人邮箱域（精确匹配，禁字符串包含）',
          excluded_email_domains JSON NULL COMMENT '公务/共享/校友域：明确排除转人工',
          email_local_matches_identifier TINYINT(1) NOT NULL DEFAULT 1
            COMMENT '个人邮箱本地部即身份标识（NetID）的映射规则开关',
          reviewer_user_ids JSON NOT NULL COMMENT '核验负责人（审核人）user id 列表；≥2 名才算运营就绪',
          config_version INT NOT NULL DEFAULT 1 COMMENT '配置版本；变更不追溯改写旧记录',
          updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
          created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='学校核验配置（负责人/域规则/版本）'
    """,
    "identity_application": """
        CREATE TABLE identity_application (
          id INT AUTO_INCREMENT PRIMARY KEY,
          school_id VARCHAR(32) NOT NULL,
          applicant_user_id INT NOT NULL,
          applicant_person_id INT NULL,
          claimed_name VARCHAR(100) NOT NULL COMMENT '声明姓名（预填自资料则标待核对）',
          claimed_identifier VARCHAR(191) NOT NULL COMMENT '声明 NetID；自填不占位，审批通过才登记',
          contact_email VARCHAR(191) NOT NULL COMMENT '待验证控制权的个人邮箱（归一化后存）',
          method VARCHAR(30) NOT NULL DEFAULT 'school_email' COMMENT '核验方式',
          status VARCHAR(20) NOT NULL DEFAULT 'draft'
            COMMENT 'draft/submitted/reviewing/approved/rejected/withdrawn',
          challenge_verified_at DATETIME NULL COMMENT '邮箱控制证明时间（approve 前置）',
          reviewed_by INT NULL COMMENT '审核人 user id（学校配置核验负责人）',
          reviewed_at DATETIME NULL,
          reject_reason VARCHAR(255) NULL COMMENT '拒绝保留原因；支持补交新版本',
          version INT NOT NULL DEFAULT 1,
          created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
          KEY idx_idapp_applicant (applicant_user_id, status),
          KEY idx_idapp_school_status (school_id, status),
          CONSTRAINT fk_idapp_user FOREIGN KEY (applicant_user_id) REFERENCES `user` (id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='身份核验申请（声明不占位）'
    """,
    "identity_challenge": """
        CREATE TABLE identity_challenge (
          id VARCHAR(64) PRIMARY KEY COMMENT '服务器生成随机 ID（uuid4.hex）',
          purpose VARCHAR(30) NOT NULL COMMENT 'school_identity/claim_bind/...(D3b)',
          application_id INT NULL COMMENT '绑定申请（payload 摘要的一部分，防跨申请挪用）',
          case_id VARCHAR(64) NULL COMMENT 'D3b 归并案例用',
          actor_user_id INT NOT NULL,
          target_user_id INT NULL,
          destination_digest CHAR(64) NOT NULL COMMENT '目标邮箱 HMAC 摘要（不存明文）',
          secret_digest CHAR(64) NOT NULL COMMENT '6 位短码 HMAC-SHA256 摘要（专用密钥）',
          payload_digest CHAR(64) NOT NULL COMMENT '绑定载荷摘要（申请id+邮箱+标识）',
          expires_at DATETIME NOT NULL COMMENT '5 分钟',
          attempts INT NOT NULL DEFAULT 0 COMMENT '错误计数；5 次锁死',
          consumed_at DATETIME NULL COMMENT '一次性：成功/锁死后置位',
          created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
          KEY idx_idch_app (application_id, consumed_at),
          KEY idx_idch_actor (actor_user_id, created_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='身份挑战（短码只存摘要，一次性）'
    """,
}

SEED_SYSU = """
    INSERT INTO identity_school_config
      (school_id, name, personal_email_domains, excluded_email_domains,
       email_local_matches_identifier, reviewer_user_ids, created_at, updated_at)
    VALUES
      ('sysu', '中山大学', '["mail2.sysu.edu.cn"]', '["sysu.edu.cn", "mail.sysu.edu.cn"]',
       1, '[74]', NOW(), NOW())
    ON DUPLICATE KEY UPDATE school_id = school_id
"""


def migrate_tables(conn):
    existing = set(insp.get_table_names())
    seeded = False
    for table, ddl in NEW_TABLES.items():
        if table in existing:
            print(f"[=] {table} 已存在")
            continue
        conn.execute(text(ddl))
        conn.commit()
        print(f"[+] 已建表 {table}")
        seeded = True
    return seeded


def seed_sysu(conn):
    row = conn.execute(text(
        "SELECT reviewer_user_ids FROM identity_school_config WHERE school_id='sysu'")).first()
    if row is not None:
        print(f"[=] sysu 配置已存在（reviewers={row[0]}），不覆盖")
        return
    conn.execute(text(SEED_SYSU))
    conn.commit()
    print("[+] 已种子 sysu 配置（个人域 mail2.sysu.edu.cn；reviewer=dev 超管 74 占位——"
          "生产上线前经管理端改为 ≥2 名真人）")


# 常驻 dev 服务的 create_all 会在迁移脚本前抢建表（无 KEY 声明）——此处幂等补齐
ENSURE_INDEXES = [
    ("identity_application", "idx_idapp_applicant",
     "CREATE INDEX idx_idapp_applicant ON identity_application (applicant_user_id, status)"),
    ("identity_application", "idx_idapp_school_status",
     "CREATE INDEX idx_idapp_school_status ON identity_application (school_id, status)"),
    ("identity_challenge", "idx_idch_app",
     "CREATE INDEX idx_idch_app ON identity_challenge (application_id, consumed_at)"),
    ("identity_challenge", "idx_idch_actor",
     "CREATE INDEX idx_idch_actor ON identity_challenge (actor_user_id, created_at)"),
]


def ensure_indexes(conn):
    for table, name, ddl in ENSURE_INDEXES:
        if name in {i['name'] for i in insp.get_indexes(table)}:
            continue
        conn.execute(text(ddl))
        conn.commit()
        print(f"[+] 已补索引 {name}")


if __name__ == '__main__':
    with engine.connect() as conn:
        migrate_tables(conn)
        ensure_indexes(conn)
        seed_sysu(conn)

    from app import app  # noqa: E402,F401
    with app.app_context():
        from models import IdentityApplicationModel, IdentityChallengeModel
        from models import IdentitySchoolConfigModel
        n_cfg = IdentitySchoolConfigModel.query.count()
        n_app = IdentityApplicationModel.query.count()
        n_ch = IdentityChallengeModel.query.count()
        ready = sum(1 for c in IdentitySchoolConfigModel.query.all()
                    if len(c.reviewer_user_ids or []) >= 2)
        print(f"[i] school_config={n_cfg}（审核人就绪[≥2 名]的学校 {ready} 个）；"
              f"application={n_app}；challenge={n_ch}（部署初始均应为 0）")
        print("[i] 开关默认全关（IDENTITY_UI_ENABLED/IDENTITY_VERIFICATION_ENABLED），"
              "上线顺序见 .env_example D3 节")

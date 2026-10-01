"""迁移 63：D2 人员层（成员身份确认与重复账号安全迁移，2026-10-01）。

user 加 person_id 列（可空 FK + UNIQUE(id,person_id)——主账号关系的可执行约束，规格 3.3）
+ 新建 6 表：
  person                 人员档案（provisional 回填/注册建立，合并指向存续者）
  person_identity        身份登记表（UNIQUE(issuer,kind,canonical_key)，学校 key 唯一登记的
                         数据库裁决点；只有核验通过才写入，D2 只建表不写入）
  person_primary_account 主参与账号（PK(person_id)+UNIQUE(user_id)，复合外键核对 user.person_id
                         归属真实，避免主号指针放 Person 造成循环依赖）
  identity_event         只追加账本（与业务写同事务，无 UPDATE/DELETE 路径，S09）
  identity_operation     幂等操作（UNIQUE(actor,operation_type,idempotency_key)，同 key
                         同摘要返回既有结果、同 key 不同内容冲突，8.2.6）
  identity_outbox        发件箱（核心提交成功与发送成功分离，S14；退避重试）

DDL（幂等执行，inspect 先查后改）：
  ALTER TABLE `user` ADD COLUMN person_id INT NULL, ADD UNIQUE KEY uq_user_id_person (id, person_id);
  CREATE TABLE person / person_identity / person_primary_account
       / identity_event / identity_operation / identity_outbox
  （完整列定义见下方代码，模型对应 models.py 的 PersonModel 等六类）

用法（项目根）：.venv/bin/python scripts/migrate/migrate_63_person_layer.py
回滚（仅允许在尚无 record_status='merged' 的 person 行前执行；升级前先 mysqldump）：
  DROP TABLE identity_outbox, identity_operation, identity_event,
             person_primary_account, person_identity, person;
  ALTER TABLE `user` DROP FOREIGN KEY fk_user_person, DROP INDEX uq_user_id_person, DROP COLUMN person_id;
依赖：本迁移只加表加列不回填——person 行由 D2 回填脚本与注册路径建立，全部 unverified；
  identity_operation.actor_user_id 用 0 表示系统动作（NOT NULL 保证唯一键对系统操作也生效，
  MySQL UNIQUE 对 NULL 不去重）。
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

# 注意：与 migrate_62 相同，本脚本不能在 DDL 前 import app——models 已声明新列而库里
# 还没有时，app 启动期查询会 1054。先用裸引擎做 DDL，成功后再 import app 出统计。
import config                # noqa: E402
from sqlalchemy import create_engine, text, inspect  # noqa: E402

engine = create_engine(config.SQLALCHEMY_DATABASE_URI)
insp = inspect(engine)

NEW_TABLES = {
    "person": """
        CREATE TABLE person (
          id INT AUTO_INCREMENT PRIMARY KEY,
          public_id VARCHAR(32) NOT NULL COMMENT '随机不透明人员编号(展示/引用用)',
          verified_name VARCHAR(100) NULL COMMENT '核验姓名,不唯一;自填姓名不写入',
          verification_status VARCHAR(20) NOT NULL DEFAULT 'unverified'
            COMMENT 'unverified/pending/verified/disputed/revoked',
          record_status VARCHAR(20) NOT NULL DEFAULT 'active' COMMENT 'active/merged',
          merged_to_person_id INT NULL COMMENT '合并指向存续人员;环由服务层持锁校验(规格3.4)',
          version INT NOT NULL DEFAULT 1,
          created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
          UNIQUE KEY uq_person_public_id (public_id),
          KEY idx_person_merged_to (merged_to_person_id),
          CONSTRAINT fk_person_merged_to FOREIGN KEY (merged_to_person_id) REFERENCES person (id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='人员档案(D2人员层)'
    """,
    "person_identity": """
        CREATE TABLE person_identity (
          id INT AUTO_INCREMENT PRIMARY KEY,
          issuer VARCHAR(50) NOT NULL COMMENT '签发者:sysu / external:<school>',
          kind VARCHAR(20) NOT NULL COMMENT 'netid/email/roster_ref',
          canonical_key VARCHAR(191) NOT NULL
            COMMENT '按机构规范归一化的身份key;大小写规则只按机构文档,禁止猜测性归一化',
          person_id INT NOT NULL,
          proof_status VARCHAR(20) NOT NULL DEFAULT 'verified'
            COMMENT '只有核验通过才写入本表;待提交值在申请表(D3)',
          assurance_method VARCHAR(50) NOT NULL COMMENT '核验方式:school_email/roster/manual等',
          proof_ref VARCHAR(191) NULL COMMENT '依据引用:challenge id/名册批次/审批单',
          version INT NOT NULL DEFAULT 1,
          created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
          UNIQUE KEY uq_person_identity_key (issuer, kind, canonical_key),
          KEY idx_person_identity_person (person_id),
          CONSTRAINT fk_person_identity_person FOREIGN KEY (person_id) REFERENCES person (id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='已核验身份登记(学校key唯一裁决点)'
    """,
    "person_primary_account": """
        CREATE TABLE person_primary_account (
          person_id INT NOT NULL,
          user_id INT NOT NULL COMMENT '只允许active standard账号作唯一正式参与账号(服务层校验)',
          version INT NOT NULL DEFAULT 1,
          updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
          PRIMARY KEY (person_id),
          UNIQUE KEY uq_ppa_user (user_id),
          KEY idx_ppa_user_person (user_id, person_id),
          CONSTRAINT fk_ppa_user_person FOREIGN KEY (user_id, person_id)
            REFERENCES `user` (id, person_id),
          CONSTRAINT fk_ppa_person FOREIGN KEY (person_id) REFERENCES person (id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='人员主参与账号(复合外键核对归属)'
    """,
    "identity_event": """
        CREATE TABLE identity_event (
          event_id BIGINT AUTO_INCREMENT PRIMARY KEY,
          operation_id VARCHAR(64) NULL COMMENT '关联幂等操作,可空(只读留痕类)',
          case_id VARCHAR(64) NULL COMMENT '关联link_case,D3起用',
          actor_user_id INT NULL COMMENT '操作者账号;回填/系统留痕可空',
          actor_person_id INT NULL COMMENT '操作者人员',
          target_ids JSON NULL COMMENT '目标对象:{user_id/person_id/...}',
          action VARCHAR(50) NOT NULL COMMENT 'person.create_provisional/identity.key.register等',
          `before` JSON NULL COMMENT '变更前快照,仅脱敏白名单字段(services/identity/events.py);列名保留字须反引号',
          `after` JSON NULL COMMENT '变更后快照,同上',
          evidence_refs JSON NULL COMMENT '证据引用:challenge/名册/审批单',
          reason VARCHAR(255) NULL,
          created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
          KEY idx_identity_event_operation (operation_id),
          KEY idx_identity_event_case (case_id),
          KEY idx_identity_event_actor (actor_user_id, created_at),
          KEY idx_identity_event_action (action, created_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='身份只追加账本(与业务写同事务)'
    """,
    "identity_operation": """
        CREATE TABLE identity_operation (
          operation_id VARCHAR(64) PRIMARY KEY,
          actor_user_id INT NOT NULL DEFAULT 0 COMMENT '发起账号;0=系统动作(NOT NULL使唯一键对系统也生效)',
          operation_type VARCHAR(50) NOT NULL,
          idempotency_key VARCHAR(191) NOT NULL,
          request_digest CHAR(64) NULL COMMENT '请求内容摘要;同key不同内容=冲突',
          state VARCHAR(20) NOT NULL DEFAULT 'running' COMMENT 'running/completed/failed',
          result_ref VARCHAR(191) NULL COMMENT '结果引用(事件id/产物句柄)',
          created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
          UNIQUE KEY uq_identity_operation (actor_user_id, operation_type, idempotency_key),
          KEY idx_identity_operation_state (state, updated_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='身份操作幂等登记'
    """,
    "identity_outbox": """
        CREATE TABLE identity_outbox (
          id INT AUTO_INCREMENT PRIMARY KEY,
          event_id BIGINT NOT NULL,
          channel VARCHAR(50) NOT NULL COMMENT '通知通道:notification/email等',
          payload JSON NULL COMMENT '待发送内容(脱敏,不含令牌/OTP)',
          delivery_state VARCHAR(20) NOT NULL DEFAULT 'pending' COMMENT 'pending/sent/failed',
          attempts INT NOT NULL DEFAULT 0,
          last_error VARCHAR(255) NULL,
          created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
          sent_at DATETIME NULL,
          KEY idx_outbox_state (delivery_state, created_at),
          KEY idx_outbox_event (event_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='身份通知发件箱(提交与发送分离,退避重试)'
    """,
}


def migrate_user_columns(conn):
    cols = {c['name'] for c in insp.get_columns('user')}
    if 'person_id' in cols:
        print("[=] user.person_id 已存在")
        return
    conn.execute(text(
        "ALTER TABLE `user` ADD COLUMN person_id INT NULL "
        "COMMENT '人员档案指向;D2回填/注册建立,标准账号必填(服务层校验)'"))
    conn.commit()
    print("[+] user: 已加列 person_id")


def migrate_user_indexes(conn):
    # UNIQUE(id,person_id)：主账号复合外键的引用目标。id 本身唯一故不可能因存量脏数据失败。
    indexes = {i['name'] for i in insp.get_indexes('user')}
    if 'uq_user_id_person' in indexes:
        print("[=] user: uq_user_id_person 已存在")
        return
    conn.execute(text("ALTER TABLE `user` ADD UNIQUE KEY uq_user_id_person (id, person_id)"))
    conn.commit()
    print("[+] user: 已加唯一键 uq_user_id_person (id, person_id)")


def migrate_user_fk(conn):
    # user.person_id → person.id（先建 person 表）。幂等：按引用目标探测——
    # 全新库经 create_all 引导时 MySQL 会自动命名（user_ibfk_N），按名字探测不到。
    if any(fk.get('referred_table') == 'person' for fk in insp.get_foreign_keys('user')):
        print("[=] user: person_id→person 外键已存在")
        return
    conn.execute(text(
        "ALTER TABLE `user` ADD CONSTRAINT fk_user_person "
        "FOREIGN KEY (person_id) REFERENCES person (id)"))
    conn.commit()
    print("[+] user: 已加外键 fk_user_person")


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
        migrate_user_columns(conn)
        # 顺序：先 user 加列 → 加 UNIQUE(id,person_id)（person_primary_account 的复合外键
        # 引用它，必须先就位）→ 建 person（user FK 的引用目标）与其余表 → user 加 FK
        migrate_user_indexes(conn)
        migrate_tables(conn)
        migrate_user_fk(conn)

    # DDL 已就位，此时 import app 才安全
    from app import app  # noqa: E402,F401
    with app.app_context():
        from models import (
            IdentityEventModel, IdentityOperationModel, PersonModel,
            UserModel,
        )
        n_user = UserModel.query.count()
        n_person = PersonModel.query.count()
        n_event = IdentityEventModel.query.count()
        n_op = IdentityOperationModel.query.count()
        print(f"[i] user={n_user}；person={n_person}（部署初始应为 0，D2 回填/注册路径写入）；"
              f"identity_event={n_event}；identity_operation={n_op}（均应为 0）")
        print("[i] 本迁移零行为变化：不加开关、不改读写路径，person 行全部由 D2 后续脚本建立")

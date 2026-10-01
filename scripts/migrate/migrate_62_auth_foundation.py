"""迁移 62：D1 身份安全地基（成员身份确认与重复账号安全迁移，2026-10-01）。

user 加 4 列（security_version / require_versioned_tokens / lifecycle / account_kind）
+ 新建 4 表（auth_session 会话真相源 / auth_factor MFA 因子 / auth_recovery_code 恢复码
/ auth_legacy_refresh_consumption 旧 refresh 一次性兑换）。

DDL（幂等执行，inspect 先查后改）：
  ALTER TABLE `user`
    ADD COLUMN security_version INT NOT NULL DEFAULT 0,
    ADD COLUMN require_versioned_tokens TINYINT(1) NOT NULL DEFAULT 0,
    ADD COLUMN lifecycle VARCHAR(20) NOT NULL DEFAULT 'active',
    ADD COLUMN account_kind VARCHAR(20) NOT NULL DEFAULT 'standard';
  CREATE TABLE auth_session / auth_factor / auth_recovery_code / auth_legacy_refresh_consumption
  （完整列定义见下方代码，模型对应 models.py 的 AuthSessionModel 等四类）

用法（项目根）：.venv/bin/python scripts/migrate/migrate_62_auth_foundation.py
回滚（仅允许在尚未写入 lifecycle 非 active 值前执行；升级前先 mysqldump）：
  DROP TABLE auth_legacy_refresh_consumption, auth_recovery_code, auth_factor, auth_session;
  ALTER TABLE `user` DROP COLUMN account_kind, DROP COLUMN lifecycle,
    DROP COLUMN require_versioned_tokens, DROP COLUMN security_version;
依赖：本迁移只加表加列，不回填任何值——存量账号 lifecycle 全部保持 active、
account_kind 全部 standard，D0 盘点报告人工确认后由 D2 回填服务号标注。
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

# 注意：本脚本不能在 DDL 前 import app——models 已声明新列而库里还没有时，
# app 启动期的 ensure_ai_topic_account 会立刻 1054。先用裸引擎做 DDL，成功后再
# import app 出统计（届时列已就位）。
import config                # noqa: E402
from sqlalchemy import create_engine, text, inspect  # noqa: E402

engine = create_engine(config.SQLALCHEMY_DATABASE_URI)
insp = inspect(engine)

NEW_TABLES = {
    "auth_session": """
        CREATE TABLE auth_session (
          sid CHAR(36) NOT NULL COMMENT 'uuid4,token与cookie共用的会话柄',
          user_id INT NOT NULL,
          client_type VARCHAR(10) NOT NULL DEFAULT 'user' COMMENT 'user/admin,续期端点核对',
          security_version INT NOT NULL DEFAULT 0 COMMENT '签发时账号安全版本快照',
          refresh_digest CHAR(64) NOT NULL COMMENT '当前refresh的HMAC-SHA256摘要(专用密钥)',
          generation INT NOT NULL DEFAULT 1 COMMENT '轮换代数,消费旧generation',
          auth_time DATETIME NOT NULL COMMENT '最近实际认证时间,轮换不刷新',
          amr VARCHAR(50) NOT NULL DEFAULT 'pwd' COMMENT 'pwd/pwd+totp;legacy兑换=unknown',
          revoked_at DATETIME NULL,
          expires_at DATETIME NOT NULL,
          last_used_at DATETIME NULL,
          user_agent VARCHAR(255) NULL,
          created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY (sid),
          KEY idx_auth_session_user (user_id, revoked_at, expires_at),
          KEY idx_auth_session_expires (expires_at),
          CONSTRAINT fk_auth_session_user FOREIGN KEY (user_id) REFERENCES user (id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='会话真相源(D1安全地基)'
    """,
    "auth_factor": """
        CREATE TABLE auth_factor (
          id INT AUTO_INCREMENT PRIMARY KEY,
          user_id INT NOT NULL,
          factor_type VARCHAR(20) NOT NULL DEFAULT 'totp',
          encrypted_secret TEXT NOT NULL COMMENT 'Fernet(MFA_ENC_SECRET)加密的base32 secret',
          key_version INT NOT NULL DEFAULT 1,
          state VARCHAR(20) NOT NULL DEFAULT 'pending' COMMENT 'pending/active/revoked',
          last_accepted_counter BIGINT NULL COMMENT '最近接受timestep,防同时间步重放',
          confirmed_at DATETIME NULL,
          created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
          KEY idx_auth_factor_user (user_id, factor_type, state),
          CONSTRAINT fk_auth_factor_user FOREIGN KEY (user_id) REFERENCES user (id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='MFA因子(首期TOTP,管理端)'
    """,
    "auth_recovery_code": """
        CREATE TABLE auth_recovery_code (
          id INT AUTO_INCREMENT PRIMARY KEY,
          user_id INT NOT NULL,
          code_digest CHAR(64) NOT NULL COMMENT 'HMAC-SHA256(专用密钥,code)',
          used_at DATETIME NULL,
          created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
          UNIQUE KEY uq_auth_recovery_digest (code_digest),
          KEY idx_auth_recovery_user (user_id, used_at),
          CONSTRAINT fk_auth_recovery_user FOREIGN KEY (user_id) REFERENCES user (id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='MFA恢复码(只存摘要,一次性)'
    """,
    "auth_legacy_refresh_consumption": """
        CREATE TABLE auth_legacy_refresh_consumption (
          old_jti_digest CHAR(64) NOT NULL COMMENT '旧refresh jti的HMAC摘要,UNIQUE防重放',
          user_id INT NOT NULL,
          new_sid CHAR(36) NOT NULL,
          consumed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
          expires_at DATETIME NOT NULL COMMENT '记录时间+14d,兼容期后清理窗口',
          PRIMARY KEY (old_jti_digest),
          KEY idx_legacy_expires (expires_at),
          CONSTRAINT fk_legacy_user FOREIGN KEY (user_id) REFERENCES user (id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='旧refresh一次性兑换(兼容期)'
    """,
}

USER_NEW_COLUMNS = [
    ("security_version", "ADD COLUMN security_version INT NOT NULL DEFAULT 0 "
     "COMMENT '安全版本:任何安全变更+1,旧access/refresh/短签即时失效'"),
    ("require_versioned_tokens", "ADD COLUMN require_versioned_tokens TINYINT(1) NOT NULL DEFAULT 0 "
     "COMMENT '置位后拒收无sid/version声明的旧协议token'"),
    ("lifecycle", "ADD COLUMN lifecycle VARCHAR(20) NOT NULL DEFAULT 'active' "
     "COMMENT 'active/merged/disabled;banned(status)优先;D2回填'"),
    ("account_kind", "ADD COLUMN account_kind VARCHAR(20) NOT NULL DEFAULT 'standard' "
     "COMMENT 'standard/management_aux/test/service;D0盘点标注,D2回填'"),
]


def migrate_user_columns(conn):
    cols = {c['name'] for c in insp.get_columns('user')}
    todo = [ddl for name, ddl in USER_NEW_COLUMNS if name not in cols]
    if not todo:
        print("[=] user 四列已存在（security_version/require_versioned_tokens/lifecycle/account_kind）")
        return
    conn.execute(text("ALTER TABLE `user` " + ", ".join(todo)))
    conn.commit()
    for name, _ddl in USER_NEW_COLUMNS:
        if any(name in t for t in todo):
            print(f"[+] user: 已加列 {name}")


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
        migrate_tables(conn)

    # DDL 已就位，此时 import app 才安全（列存在，启动期查询不再 1054）
    from app import app  # noqa: E402,F401
    with app.app_context():
        from models import UserModel, AuthSessionModel, AuthFactorModel
        n_user = UserModel.query.count()
        n_admin = UserModel.query.filter_by(role='super_admin').count()
        n_sess = AuthSessionModel.query.count()
        n_factor = AuthFactorModel.query.count()
        print(f"[i] user={n_user}（super_admin {n_admin}）；auth_session={n_sess}（部署初始应为 0）；"
              f"auth_factor={n_factor}（应为 0）")
        print("[i] 新列默认值即存量语义：lifecycle=active / account_kind=standard / 版本 0，"
              "行为与迁移前完全一致（列只增不改读写路径）")

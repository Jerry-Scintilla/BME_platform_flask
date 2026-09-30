"""迁移 58：内部工作台·社团协作与工作留痕（2026-09-28 设计方案，feature/work-collab M1，幂等）。

步骤：
1. 建 19 张表（create_all 限定本模块表集合，checkfirst 幂等——只补缺表、不动
   既有表，也不跟随 models.py 当前态补建任何其他缺失表，schema 冻结于清单）：
   工作区 work_workspace / 授权 work_access_grant /
   事项 work_item + 参与 work_item_participant + 任务属性 work_task /
   回复 work_reply + 修订 work_reply_revision / 待回应 work_response_request /
   转交 work_transfer_request / 提交 work_submission / 事件 work_event /
   已读 work_read_state / 文件 work_file + work_file_version + work_file_link /
   通知回执 work_notification_receipt / 提醒 work_reminder /
   组织事件 club_org_event / 业务关联 work_business_link
2. 幂等修补：work_item 的创建幂等键约束 uq_work_item_idem 由单列
   (idempotency_key) 改复合 (created_by, idempotency_key)——跨用户撞 key 不再
   互相干扰/可被占位（旧库已按单列建表的场景；新库 create_all 直接建复合）
3. 无存量数据迁移：工作区与授权由治理端点（/work/governance/*）按明确清单开通，
   不自动把历史任职批量转授权（设计方案 §17.1）
4. 附带说明：notification 表 category 新增取值 'work'、source_type 新增 'work_item'
   （纯注释约定，无 DDL）；时间约定沿用全库 naive Asia/Shanghai
   （设计方案 §8.5 写 UTC，为避免跨模块换算税统一本库约定——已知偏差，记录于案）

用法（项目根）：.venv/bin/python scripts/migrate/migrate_58_work_collab.py
回滚：DROP TABLE work_business_link, club_org_event, work_reminder,
      work_notification_receipt, work_file_link, work_file_version, work_file,
      work_read_state, work_event, work_submission, work_transfer_request,
      work_response_request, work_reply_revision, work_reply, work_task,
      work_item_participant, work_item, work_access_grant, work_workspace;
      （升级前先 mysqldump）
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from app import app          # noqa: E402,F401  (import 即装配 config)
from sqlalchemy import create_engine, text, inspect  # noqa: E402

import config                # noqa: E402

engine = create_engine(config.SQLALCHEMY_DATABASE_URI)
insp = inspect(engine)

TABLES = (
    'work_workspace', 'work_access_grant',
    'work_item', 'work_item_participant', 'work_task',
    'work_reply', 'work_reply_revision', 'work_response_request',
    'work_transfer_request', 'work_submission', 'work_event',
    'work_read_state',
    'work_file', 'work_file_version', 'work_file_link',
    'work_notification_receipt', 'work_reminder',
    'club_org_event', 'work_business_link',
)

#: 创建幂等键复合唯一约束（评审 #13）：(created_by, idempotency_key)
IDEM_INDEX_NAME = 'uq_work_item_idem'
IDEM_INDEX_COLUMNS = ['created_by', 'idempotency_key']


def _repair_work_item_idem_index(conn):
    """幂等修补已建表的 dev 库：uq_work_item_idem 单列 → 复合（幂等，可重跑）。

    仅在约束仍为旧单列形态时 DROP+ADD；已是复合或不存在（新库由 create_all
    直接建复合）则跳过。MySQL 走 ALTER 语法；其他方言（如开发用 SQLite）无
    该修补路径，靠建表语句兜底。"""
    if engine.dialect.name != 'mysql':
        print("[=] 非 MySQL 方言，跳过幂等键约束修补（新表由 create_all 建复合约束）")
        return
    try:
        # 现取 inspector：模块级 insp 创建于建表前，避免其 info_cache 陈旧
        fresh = inspect(engine)
        indexes = {i['name']: (i.get('column_names') or [])
                   for i in fresh.get_indexes('work_item')}
    except Exception:
        print("[!] 无法读取 work_item 索引，跳过修补")
        return
    current = indexes.get(IDEM_INDEX_NAME)
    if current == IDEM_INDEX_COLUMNS:
        print("[=] uq_work_item_idem 已是复合唯一约束 (created_by, idempotency_key)")
        return
    try:
        if current:
            conn.execute(text(f"ALTER TABLE work_item DROP INDEX {IDEM_INDEX_NAME}"))
            conn.commit()
        conn.execute(text(
            f"ALTER TABLE work_item ADD UNIQUE KEY {IDEM_INDEX_NAME} "
            f"({', '.join(IDEM_INDEX_COLUMNS)})"))
        conn.commit()
        print(f"[+] uq_work_item_idem 已改为复合唯一 ({', '.join(IDEM_INDEX_COLUMNS)})")
    except Exception as exc:
        print(f"[!] 幂等键约束修补失败（可手工执行 ALTER）：{exc}")


if __name__ == '__main__':
    with app.app_context():
        from exts import db
        missing = [t for t in TABLES if not insp.has_table(t)]
        if missing:
            # 限定表集合（#42）：只建本迁移清单内的表，DDL 不跟随 models.py 漂移。
            # Flask-SQLAlchemy 的 db.create_all 不收 tables 参数，走元数据层等价调用
            db.metadata.create_all(
                bind=db.engine, tables=[db.metadata.tables[t] for t in TABLES])
            print(f"[+] 已建表：{', '.join(missing)}")
        else:
            print("[=] 内部工作台表均已存在")
    with engine.connect() as conn:
        _repair_work_item_idem_index(conn)
        n = conn.execute(text("SELECT COUNT(*) FROM work_workspace")).scalar()
        print(f"[i] work_workspace 现有 {n} 行（工作区由治理端点按需开通，不预置）")

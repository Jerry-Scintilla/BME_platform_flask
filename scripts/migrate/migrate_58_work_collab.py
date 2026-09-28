"""迁移 58：内部工作台·社团协作与工作留痕（2026-09-28 设计方案，feature/work-collab M1，幂等）。

步骤：
1. 建 19 张表（db.create_all 只补缺表，checkfirst 幂等）：
   工作区 work_workspace / 授权 work_access_grant /
   事项 work_item + 参与 work_item_participant + 任务属性 work_task /
   回复 work_reply + 修订 work_reply_revision / 待回应 work_response_request /
   转交 work_transfer_request / 提交 work_submission / 事件 work_event /
   已读 work_read_state / 文件 work_file + work_file_version + work_file_link /
   通知回执 work_notification_receipt / 提醒 work_reminder /
   组织事件 club_org_event / 业务关联 work_business_link
2. 无存量数据迁移：工作区与授权由治理端点（/work/governance/*）按明确清单开通，
   不自动把历史任职批量转授权（设计方案 §17.1）
3. 附带说明：notification 表 category 新增取值 'work'、source_type 新增 'work_item'
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

if __name__ == '__main__':
    with app.app_context():
        from exts import db
        missing = [t for t in TABLES if not insp.has_table(t)]
        if missing:
            db.create_all()      # 只补缺表，不动既有表
            print(f"[+] 已建表：{', '.join(missing)}")
        else:
            print("[=] 内部工作台表均已存在")
    with engine.connect() as conn:
        n = conn.execute(text("SELECT COUNT(*) FROM work_workspace")).scalar()
        print(f"[i] work_workspace 现有 {n} 行（工作区由治理端点按需开通，不预置）")

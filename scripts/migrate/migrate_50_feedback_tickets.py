"""迁移 50：用户反馈工单（2026-09-21 管理端 IA 重构 · 设计方案阶段 6A，幂等）。

步骤：
1. 建四张工单表（feedback_ticket / feedback_ticket_attachment /
   feedback_ticket_message / feedback_ticket_event）——db.create_all 只补缺表
2. 存量迁移：InformationModel.type=4（旧报错信息）一次性迁入 FeedbackTicket
   （默认 status='new'，source_information_id 留 id 映射）；旧图片文件从
   ERROR_IMAGE_DIR 转存 storage（双后端）并登记 Attachment
3. 旧 /information/error/* 端点此后内部转调新服务（代码层兼容，不动旧表）

用法（项目根）：.venv/bin/python scripts/migrate/migrate_50_feedback_tickets.py
回滚：DROP TABLE feedback_ticket_event, feedback_ticket_message,
      feedback_ticket_attachment, feedback_ticket;（升级前先 mysqldump；
      旧 InformationModel.type=4 行不删除，映射可反查）
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from app import app          # noqa: E402,F401  (import 即装配 config)
from sqlalchemy import create_engine, text, inspect  # noqa: E402

import config                # noqa: E402

engine = create_engine(config.SQLALCHEMY_DATABASE_URI)
insp = inspect(engine)

TABLES = ('feedback_ticket', 'feedback_ticket_attachment',
          'feedback_ticket_message', 'feedback_ticket_event')


def migrate_information_type4():
    """旧 type=4 报错行 → 工单（幂等：source_information_id 已存在则跳过）。"""
    with app.app_context():
        from exts import db
        from models import (InformationModel, FeedbackTicket, FeedbackTicketAttachment)
        from storage import storage

        rows = InformationModel.query.filter_by(type=4).all()
        if not rows:
            print("[=] 无 type=4 旧报错数据")
            return
        existing = {t.source_information_id for t in FeedbackTicket.query.all()
                    if t.source_information_id}
        created = attached = 0
        for r in rows:
            if r.id in existing:
                continue
            t = FeedbackTicket(
                reporter_user_id=r.student_id,
                category='bug',
                title=(r.title or '（无标题的历史反馈）')[:200],
                description=r.content,
                status='new',
                source_information_id=r.id,
                created_at=r.create_time,
            )
            db.session.add(t)
            db.session.flush()

            # 旧图片文件转存 storage（失败不阻塞迁移——旧文件仍在原目录）
            if r.resource:
                from blueprints.information import ERROR_IMAGE_DIR
                path = os.path.join(ERROR_IMAGE_DIR, r.resource)
                if os.path.exists(path):
                    try:
                        import io as _io
                        with open(path, 'rb') as f:
                            data = f.read()
                        key = f"feedback-tickets/{t.id}/legacy-{r.resource}"
                        mime = ('image/png' if r.resource.lower().endswith('.png')
                                else 'image/jpeg')
                        storage.put_object(key, _io.BytesIO(data), length=len(data),
                                           content_type=mime)
                        db.session.add(FeedbackTicketAttachment(
                            ticket_id=t.id, storage_key=key,
                            original_name=r.resource, mime_type=mime, size=len(data)))
                        attached += 1
                    except Exception as e:
                        print(f"[!] 附件转存失败 information_id={r.id}: {e}")
            created += 1
        db.session.commit()
        print(f"[+] 迁入 {created} 条历史反馈（附件 {attached} 个）——旧 type=4 行保留")


if __name__ == '__main__':
    with app.app_context():
        from exts import db
        missing = [t for t in TABLES if not insp.has_table(t)]
        if missing:
            db.create_all()      # 只补缺表，不动既有表
            print(f"[+] 已建表：{', '.join(missing)}")
        else:
            print("[=] 工单表均已存在")
    migrate_information_type4()
    with engine.connect() as conn:
        n = conn.execute(text("SELECT COUNT(*) FROM feedback_ticket")).scalar()
        print(f"[i] feedback_ticket 现有 {n} 行")

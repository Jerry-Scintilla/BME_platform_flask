"""迁移 35：组会·布置与任务四表（2026-09-17，幂等）。

组会升级为教学工作单元（一次组会=纪要+布置+提交+审阅）：
- camp_meeting_chapter_plan：课内进度布置（本次指定到下次组会前完成/认证的章节）
- camp_meeting_task / camp_meeting_task_submission / camp_meeting_task_attachment：
  课外任务（文字/文件提交，每人一条 upsert），导生审阅 + 按学生分文件夹一键打包 zip。

用法：python scripts/migrate/migrate_35_camp_meeting_assignments.py
回滚：DROP TABLE camp_meeting_task_attachment, camp_meeting_task_submission,
      camp_meeting_task, camp_meeting_chapter_plan;（camp_meeting 主表保留）
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

import config  # noqa: E402
from sqlalchemy import create_engine, text, inspect  # noqa: E402

TABLES = {
    'camp_meeting_chapter_plan': """
        CREATE TABLE camp_meeting_chapter_plan (
            id INT AUTO_INCREMENT PRIMARY KEY,
            meeting_id INT NOT NULL COMMENT '所属组会',
            course_id INT NOT NULL COMMENT '章节所属课程（冗余，矩阵列分组用）',
            chapter_id INT NOT NULL COMMENT '布置章节',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            INDEX ix_cmtp_meeting (meeting_id),
            CONSTRAINT fk_cmtp_meeting FOREIGN KEY (meeting_id)
                REFERENCES camp_meeting(id) ON DELETE CASCADE,
            CONSTRAINT fk_cmtp_course FOREIGN KEY (course_id) REFERENCES course(id),
            CONSTRAINT fk_cmtp_chapter FOREIGN KEY (chapter_id) REFERENCES chapter(id),
            UNIQUE KEY uq_cmtplan_chapter (meeting_id, chapter_id)
        ) CHARSET=utf8mb4
    """,
    'camp_meeting_task': """
        CREATE TABLE camp_meeting_task (
            id INT AUTO_INCREMENT PRIMARY KEY,
            meeting_id INT NOT NULL COMMENT '所属组会',
            title VARCHAR(200) NOT NULL COMMENT '任务标题',
            note VARCHAR(500) COMMENT '任务说明',
            submit_type VARCHAR(10) NOT NULL DEFAULT 'any' COMMENT 'file=需交文件/text=需交文字/any=任一',
            created_by INT NOT NULL COMMENT '布置人',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            INDEX ix_cmtask_meeting (meeting_id),
            CONSTRAINT fk_cmtask_meeting FOREIGN KEY (meeting_id)
                REFERENCES camp_meeting(id) ON DELETE CASCADE,
            CONSTRAINT fk_cmtask_creator FOREIGN KEY (created_by) REFERENCES user(id)
        ) CHARSET=utf8mb4
    """,
    'camp_meeting_task_submission': """
        CREATE TABLE camp_meeting_task_submission (
            id INT AUTO_INCREMENT PRIMARY KEY,
            task_id INT NOT NULL COMMENT '所属任务',
            student_user_id INT NOT NULL COMMENT '提交学员',
            content TEXT COMMENT '文字提交（可空=纯文件）',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            INDEX ix_cmtsub_task (task_id),
            INDEX ix_cmtsub_student (student_user_id),
            CONSTRAINT fk_cmtsub_task FOREIGN KEY (task_id)
                REFERENCES camp_meeting_task(id) ON DELETE CASCADE,
            CONSTRAINT fk_cmtsub_student FOREIGN KEY (student_user_id) REFERENCES user(id),
            UNIQUE KEY uq_cmtsub_task_student (task_id, student_user_id)
        ) CHARSET=utf8mb4
    """,
    'camp_meeting_task_attachment': """
        CREATE TABLE camp_meeting_task_attachment (
            id INT AUTO_INCREMENT PRIMARY KEY,
            submission_id INT NOT NULL COMMENT '所属提交',
            object_key VARCHAR(255) NOT NULL,
            filename VARCHAR(200) NOT NULL,
            size INT,
            content_type VARCHAR(100),
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            INDEX ix_cmtatt_submission (submission_id),
            CONSTRAINT fk_cmtatt_submission FOREIGN KEY (submission_id)
                REFERENCES camp_meeting_task_submission(id) ON DELETE CASCADE
        ) CHARSET=utf8mb4
    """,
}

engine = create_engine(config.SQLALCHEMY_DATABASE_URI)
insp = inspect(engine)

with engine.connect() as conn:
    for table, ddl in TABLES.items():
        if insp.has_table(table):
            print(f"[=] {table} 已存在")
            continue
        conn.execute(text(ddl))
        conn.commit()
        print(f"[+] {table} 已创建")
    print("[done] migrate_35 完成")

"""迁移 31：营期学习进度快照表（2026-09-14，幂等）。

camp_learning_progress：营内打点写本表（营期维度从零，不读不写全局 learning_progress）。
写入分流 = /learningProgress/lesson/update 按 user_course.camp_session_id 戳；结营 close
迁移把 completed 行合并回全局（learning 态丢弃）。UQ(camp,user,lesson) 保证幂等打点与
跨营同课快照互不污染。

用法：python scripts/migrate/migrate_31_camp_learning_progress.py
回滚：DROP TABLE camp_learning_progress;
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

import config  # noqa: E402
from sqlalchemy import create_engine, text, inspect  # noqa: E402

TABLES = {
    'camp_learning_progress': """
        CREATE TABLE camp_learning_progress (
            id INT AUTO_INCREMENT PRIMARY KEY,
            camp_session_id INT NOT NULL COMMENT '营期',
            user_id INT NOT NULL COMMENT '学员',
            course_id INT NOT NULL COMMENT '课程（冗余，章节聚合/合并用）',
            lesson_id INT NOT NULL COMMENT '课时',
            status VARCHAR(20) DEFAULT 'learning' COMMENT 'learning / completed（营内无 not_started 行）',
            duration INT DEFAULT 0,
            detail JSON,
            start_time DATETIME,
            completed_time DATETIME,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            INDEX ix_clp_camp (camp_session_id),
            INDEX ix_clp_user (user_id),
            INDEX ix_clp_lesson (lesson_id),
            UNIQUE KEY uq_clp_camp_user_lesson (camp_session_id, user_id, lesson_id),
            CONSTRAINT fk_clp_camp FOREIGN KEY (camp_session_id) REFERENCES camp_session(id),
            CONSTRAINT fk_clp_user FOREIGN KEY (user_id) REFERENCES user(id),
            CONSTRAINT fk_clp_course FOREIGN KEY (course_id) REFERENCES course(id),
            CONSTRAINT fk_clp_lesson FOREIGN KEY (lesson_id) REFERENCES lesson(id)
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
    print("[done] migrate_31 完成")

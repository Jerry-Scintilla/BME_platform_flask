"""迁移 24：方向制学习·章节认证表（2026-09-12，幂等）。

camp_chapter_certification 单表：学员随导生继承方向课程后，导生按章认证学习进度；
全章认证齐由应用层自动置 user_course.status=completed。认证人留痕（改派后新导师可继续）。

用法：python scripts/migrate/migrate_24_chapter_certification.py
回滚：DROP TABLE camp_chapter_certification
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

import config  # noqa: E402
from sqlalchemy import create_engine, text, inspect  # noqa: E402

TABLES = {
    'camp_chapter_certification': """
        CREATE TABLE camp_chapter_certification (
            id INT AUTO_INCREMENT PRIMARY KEY,
            camp_session_id INT NOT NULL COMMENT '营期',
            student_user_id INT NOT NULL COMMENT '学员',
            chapter_id INT NOT NULL COMMENT '被认证章节',
            course_id INT NOT NULL COMMENT '方向课程',
            mentor_user_id INT NOT NULL COMMENT '认证导生（留痕）',
            certified_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            INDEX ix_cert_camp (camp_session_id),
            INDEX ix_cert_student (camp_session_id, student_user_id),
            INDEX ix_cert_chapter (chapter_id),
            UNIQUE KEY uq_cert_camp_student_chapter (camp_session_id, student_user_id, chapter_id),
            CONSTRAINT fk_cert_camp FOREIGN KEY (camp_session_id) REFERENCES camp_session(id),
            CONSTRAINT fk_cert_student FOREIGN KEY (student_user_id) REFERENCES user(id),
            CONSTRAINT fk_cert_chapter FOREIGN KEY (chapter_id) REFERENCES chapter(id),
            CONSTRAINT fk_cert_course FOREIGN KEY (course_id) REFERENCES course(id)
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
    print("[done] migrate_24 完成")

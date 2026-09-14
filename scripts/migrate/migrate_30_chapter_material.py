"""迁移 30：培训营·章节材料两表（2026-09-14，幂等）。

camp_chapter_material + camp_chapter_material_attachment：学员按章提交材料（文字+附件），
提交即可见、追加式、无审核流；导生按章认证/评分时查看下载作为依据。
附件本体走 storage 层（STORAGE_BACKEND=minio|local），object_key 规则
camp/{sid}/chapter/{chapter_id}/{uuid}{ext}。

用法：python scripts/migrate/migrate_30_chapter_material.py
回滚：DROP TABLE camp_chapter_material_attachment; DROP TABLE camp_chapter_material;
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

import config  # noqa: E402
from sqlalchemy import create_engine, text, inspect  # noqa: E402

TABLES = {
    'camp_chapter_material': """
        CREATE TABLE camp_chapter_material (
            id INT AUTO_INCREMENT PRIMARY KEY,
            camp_session_id INT NOT NULL COMMENT '营期',
            course_id INT NOT NULL COMMENT '章节所属课程（冗余，聚合用）',
            chapter_id INT NOT NULL COMMENT '章节',
            student_user_id INT NOT NULL COMMENT '提交学员',
            content TEXT COMMENT '文字说明（可空=纯附件）',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            INDEX ix_cmat_camp (camp_session_id),
            INDEX ix_cmat_student (camp_session_id, student_user_id),
            INDEX ix_cmat_chapter (chapter_id),
            CONSTRAINT fk_cmat_camp FOREIGN KEY (camp_session_id) REFERENCES camp_session(id),
            CONSTRAINT fk_cmat_course FOREIGN KEY (course_id) REFERENCES course(id),
            CONSTRAINT fk_cmat_chapter FOREIGN KEY (chapter_id) REFERENCES chapter(id),
            CONSTRAINT fk_cmat_student FOREIGN KEY (student_user_id) REFERENCES user(id)
        ) CHARSET=utf8mb4
    """,
    'camp_chapter_material_attachment': """
        CREATE TABLE camp_chapter_material_attachment (
            id INT AUTO_INCREMENT PRIMARY KEY,
            material_id INT NOT NULL COMMENT '所属材料',
            object_key VARCHAR(255) NOT NULL,
            filename VARCHAR(200) NOT NULL,
            size INT,
            content_type VARCHAR(100),
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            INDEX ix_cmatt_material (material_id),
            CONSTRAINT fk_cmatt_material FOREIGN KEY (material_id)
                REFERENCES camp_chapter_material(id) ON DELETE CASCADE
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
    print("[done] migrate_30 完成")

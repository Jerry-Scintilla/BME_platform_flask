"""迁移 18：选导生·学员收藏表 camp_mentor_favorite（幂等）。

背景（2026-09，前端同事 60b39fd 导生集市名片改版）：学员逛市集时可收藏心仪导生
辅助整理（不限数量、不参与配对/导出）。前端已上线容错降级（接口 404 时显示
"收藏暂不可用"），本迁移落表后配套端点（camp_ms GET/PUT/DELETE /<sid>/favorites）
即可启用。契约：GET → {mentor_ids: [...]}；PUT/DELETE → {mentor_id, favorited}。

- 建表 camp_mentor_favorite（营期+学员+导生三元组唯一，防重复收藏）

用法（项目根目录）：python scripts/migrate/migrate_18_mentor_favorite.py
回滚：DROP TABLE camp_mentor_favorite
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

import config  # noqa: E402
from sqlalchemy import create_engine, text, inspect  # noqa: E402

engine = create_engine(config.SQLALCHEMY_DATABASE_URI)
insp = inspect(engine)

with engine.connect() as conn:
    if not insp.has_table('camp_mentor_favorite'):
        conn.execute(text("""
            CREATE TABLE camp_mentor_favorite (
                id INT AUTO_INCREMENT PRIMARY KEY,
                camp_session_id INT NOT NULL,
                student_user_id INT NOT NULL,
                mentor_user_id INT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE KEY uq_ms_fav_mentor (camp_session_id, student_user_id, mentor_user_id),
                KEY ix_camp_mentor_favorite_camp_session_id (camp_session_id),
                KEY ix_camp_mentor_favorite_student_user_id (student_user_id),
                KEY ix_camp_mentor_favorite_mentor_user_id (mentor_user_id),
                CONSTRAINT fk_fav_session FOREIGN KEY (camp_session_id) REFERENCES camp_session (id),
                CONSTRAINT fk_fav_student FOREIGN KEY (student_user_id) REFERENCES user (id),
                CONSTRAINT fk_fav_mentor FOREIGN KEY (mentor_user_id) REFERENCES user (id)
            ) CHARSET=utf8mb4
        """))
        conn.commit()
        print("[+] camp_mentor_favorite 已创建")
    else:
        print("[=] camp_mentor_favorite 已存在")
    print("[done] migrate_18 完成")

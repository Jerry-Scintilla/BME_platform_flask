"""迁移 29：项目营·节点评价表（2026-09-13，幂等）。

camp_node_evaluation 单表：负责人对每位成员在各交付节点的评价（分数 0-100 + 评语）。
09-13 拍板：文件提交入口本轮不上线，交付验收简化为节点评价制（老师验收退出）；
提交/审核链（camp_submission_version）原样保留，后续上线文件提交管理时复用。
节点完成态读时派生（评齐=完成），不落 milestone.status。

用法：python scripts/migrate/migrate_29_node_evaluation.py
回滚：DROP TABLE camp_node_evaluation
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

import config  # noqa: E402
from sqlalchemy import create_engine, text, inspect  # noqa: E402

TABLES = {
    'camp_node_evaluation': """
        CREATE TABLE camp_node_evaluation (
            id INT AUTO_INCREMENT PRIMARY KEY,
            camp_session_id INT NOT NULL COMMENT '营期',
            unit_id INT NOT NULL COMMENT '项目单元',
            milestone_id INT NOT NULL COMMENT '交付节点',
            member_user_id INT NOT NULL COMMENT '被评成员（active 单元成员，不含负责人本人）',
            score INT NOT NULL COMMENT '评分 0-100',
            comment VARCHAR(500) COMMENT '评语（选填）',
            leader_user_id INT NOT NULL COMMENT '评价负责人（留痕）',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            INDEX ix_neval_camp (camp_session_id),
            INDEX ix_neval_unit (unit_id),
            INDEX ix_neval_milestone (milestone_id),
            UNIQUE KEY uq_neval_milestone_member (milestone_id, member_user_id),
            CONSTRAINT fk_neval_camp FOREIGN KEY (camp_session_id) REFERENCES camp_session(id),
            CONSTRAINT fk_neval_unit FOREIGN KEY (unit_id) REFERENCES camp_unit(id),
            CONSTRAINT fk_neval_milestone FOREIGN KEY (milestone_id) REFERENCES camp_milestone(id),
            CONSTRAINT fk_neval_member FOREIGN KEY (member_user_id) REFERENCES user(id),
            CONSTRAINT fk_neval_leader FOREIGN KEY (leader_user_id) REFERENCES user(id)
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
    print("[done] migrate_29 完成")

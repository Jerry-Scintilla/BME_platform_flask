"""迁移 53：课程书架表 course_shelf（2026-09-21，幂等）。

背景：本轮课程详情页改造移除「加入学习」步骤（自主学=登录即学），新增与
user_course 选课完全解耦的课程书架（收藏）能力。书架只表达收藏关系：
不建立选课、不影响 can_learn 门禁/营期归属/学习进度/课成判定；
加入与移出均幂等；下架课不删书架行。

表结构：
  id          自增主键
  user_id     收藏人（FK user.id）
  course_id   课程（FK course.id）
  created_at  收藏时间
  UQ(user_id, course_id)          —— 一人一课一条，幂等基础
  IX(user_id, created_at)         —— 我的书架按时间倒序
  IX(course_id)                   —— 课程维度反查

用法：python scripts/migrate/migrate_53_course_shelf.py
回滚：DROP TABLE course_shelf;（与选课无关，无残留）
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

import config  # noqa: E402
from sqlalchemy import create_engine, text, inspect  # noqa: E402

TABLES = {
    'course_shelf': """
        CREATE TABLE course_shelf (
            id INT AUTO_INCREMENT PRIMARY KEY,
            user_id INT NOT NULL COMMENT '收藏人',
            course_id INT NOT NULL COMMENT '课程',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '收藏时间',
            UNIQUE KEY uq_course_shelf_user_course (user_id, course_id),
            INDEX ix_course_shelf_user_created (user_id, created_at),
            INDEX ix_course_shelf_course (course_id),
            CONSTRAINT fk_course_shelf_user FOREIGN KEY (user_id) REFERENCES user(id),
            CONSTRAINT fk_course_shelf_course FOREIGN KEY (course_id) REFERENCES course(id))
        CHARSET=utf8mb4
    """,
}

engine = create_engine(config.SQLALCHEMY_DATABASE_URI)
insp = inspect(engine)

with engine.connect() as conn:
    for table, ddl in TABLES.items():
        if insp.has_table(table):
            print(f"[=] 表 {table} 已存在")
            continue
        conn.execute(text(ddl))
        conn.commit()
        print(f"[+] 表 {table} 已创建")
    print("[done] migrate_53 完成")

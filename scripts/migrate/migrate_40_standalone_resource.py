"""迁移 40：学习资源中心·平台资料表（2026-09-20，幂等）。

standalone_resource：管理员上传的独立资料（不挂课程），课程资料仍走 course_resource
按课程归组。文件本体在对象存储（storage.py，STORAGE_BACKEND=minio|local），
object_key 规则 resources/{category}/{uuid}{ext}；下载走 media_sign 短签代理端点。
category 为应用层枚举（software/handbook/standard/other，见 resource_center.py）。

用法：python scripts/migrate/migrate_40_standalone_resource.py
回滚：DROP TABLE standalone_resource;
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

import config  # noqa: E402
from sqlalchemy import create_engine, text, inspect  # noqa: E402

TABLES = {
    'standalone_resource': """
        CREATE TABLE standalone_resource (
            id INT AUTO_INCREMENT PRIMARY KEY,
            name VARCHAR(200) NOT NULL COMMENT '展示名（含扩展名）',
            description VARCHAR(500) COMMENT '一句话说明（选填）',
            category VARCHAR(50) NOT NULL DEFAULT 'other' COMMENT '分类枚举：software/handbook/standard/other',
            object_key VARCHAR(300) NOT NULL COMMENT '对象存储 key：resources/{category}/{uuid}{ext}',
            size INT NOT NULL DEFAULT 0 COMMENT '字节数',
            content_type VARCHAR(100),
            sort_order INT NOT NULL DEFAULT 0,
            uploader_id INT NULL COMMENT '上传人（审计留痕）',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            INDEX ix_standalone_category (category),
            CONSTRAINT fk_standalone_uploader FOREIGN KEY (uploader_id) REFERENCES user(id)
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
    print("[done] migrate_40 完成")

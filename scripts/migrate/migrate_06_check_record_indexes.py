"""迁移 06：给 check_record 加 (user_id, date) 复合索引（幂等）。

- 覆盖全仓库最高频查询模式 WHERE user_id=? AND date BETWEEN ?
  （codecheck.py / user.py / camp.py 多处）。
- app.py 启动既不跑 db.create_all() 也不跑 flask db upgrade，且 create_all 不给
  既有表补索引（见 migrate_02 注释 / 姐妹项目 3DFarm 同款坑），故必须用本脚本
  单独 CREATE INDEX（MySQL 8 默认 ALGORITHM=INPLACE LOCK=NONE，不阻塞写）。
- models.py 的 CheckRecord.__table_args__ 已同步声明同名索引，供全新空库
  db.create_all() 时随表建出；本脚本负责让"既有库"也补上。

用法（在项目根目录）：python scripts/migrate/migrate_06_check_record_indexes.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from sqlalchemy import text, inspect  # noqa: E402
from app import app          # noqa: E402
from exts import db          # noqa: E402

INDEX_NAME = 'ix_check_record_user_date'
TABLE = 'check_record'

with app.app_context():
    engine = db.engine
    insp = inspect(engine)
    existing = {idx['name'] for idx in insp.get_indexes(TABLE)}
    if INDEX_NAME in existing:
        print(f"[=] {TABLE}.{INDEX_NAME} 已存在，跳过")
    else:
        with engine.connect() as conn:
            conn.execute(text(
                f'CREATE INDEX `{INDEX_NAME}` ON `{TABLE}` (`user_id`, `date`)'
            ))
            conn.commit()
        print(f"[+] 已创建 {TABLE}.{INDEX_NAME} (user_id, date)")
    print("[done] migrate_06 完成")

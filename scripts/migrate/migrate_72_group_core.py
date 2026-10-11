"""迁移 72：只新增小组核心五表，不改营期表、不回填、不切换业务。

默认输出 MySQL DDL，不连接数据库；显式 --apply 才在配置的库中幂等建表。
不导入 app，避免启动调度器和 app 的全库 create_all。
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from sqlalchemy import create_engine, inspect, text  # noqa: E402
from sqlalchemy.dialects import mysql  # noqa: E402
from sqlalchemy.schema import CreateIndex, CreateTable, CreateColumn  # noqa: E402

from exts import db  # noqa: E402
import models  # noqa: E402,F401
from group_models import GROUP_CORE_TABLES  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='在配置的数据库中新建缺失表')
    args = parser.parse_args()
    if not args.apply:
        dialect = mysql.dialect()
        for table in GROUP_CORE_TABLES:
            print(str(CreateTable(table).compile(dialect=dialect)) + ';')
            for index in sorted(table.indexes, key=lambda value: value.name):
                print(str(CreateIndex(index).compile(dialect=dialect)) + ';')
        return
    import config
    engine = create_engine(config.SQLALCHEMY_DATABASE_URI)
    try:
        inspector = inspect(engine)
        required = {'user', 'person'}
        if not required.issubset(inspector.get_table_names()):
            raise RuntimeError('请先完成账号与 Person 层迁移，再建立小组核心')
        # 严格限制到五张新表；已有表 checkfirst 跳过，存量结构差异需单独处理。
        db.metadata.create_all(engine, tables=list(GROUP_CORE_TABLES), checkfirst=True)
        # 兼容早期开发环境已建的 DATETIME(0)，新角色不能因秒级舍入延迟生效。
        with engine.begin() as connection:
            for table in GROUP_CORE_TABLES:
                existing = {c['name']: c for c in inspect(connection).get_columns(table.name)}
                for column in table.c:
                    if isinstance(column.type, models.db.DateTime) and getattr(existing[column.name]['type'], 'fsp', 0) != 6:
                        definition = str(CreateColumn(column).compile(dialect=mysql.dialect()))
                        connection.execute(text(f'ALTER TABLE `{table.name}` MODIFY COLUMN {definition}'))
        print('小组核心五表已就绪；未迁移成员、未启用营期适配。')
    finally:
        engine.dispose()


if __name__ == '__main__':
    main()

"""迁移 76：组会任务/章节布置时间提升至微秒，与小组职责生效时间精度一致。

默认离线输出 DDL；--apply 才修改结构。不修改旧时间值或重算任务义务，不导入 app。
"""
import argparse
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from sqlalchemy import create_engine, inspect, text

TABLES = ('camp_meeting_task', 'camp_meeting_chapter_plan')


def ddl(table):
    return f'ALTER TABLE {table} MODIFY COLUMN created_at DATETIME(6) NULL'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if not args.apply:
        for table in TABLES:
            print(ddl(table) + ';')
        return
    import config
    engine = create_engine(config.SQLALCHEMY_DATABASE_URI)
    try:
        with engine.begin() as connection:
            inspector = inspect(connection)
            if not {*TABLES, 'camp_maintenance_fence'}.issubset(inspector.get_table_names()):
                raise RuntimeError('请先完成既有组会迁移及迁移 72–75')
            for table in TABLES:
                column = next(c for c in inspector.get_columns(table) if c['name'] == 'created_at')
                if getattr(column['type'], 'fsp', 0) != 6:
                    connection.execute(text(ddl(table)))
        print('任务布置时间精度已就绪；未重新计算历史任务义务。')
    finally:
        engine.dispose()

if __name__ == '__main__':
    main()

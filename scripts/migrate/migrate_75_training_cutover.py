"""迁移 75：旧培训映射批次与按营维护标记。默认离线 DDL；--apply 仅添加结构。"""
import argparse
import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateTable, CreateColumn
from models import CampSession, TrainingMigrationRun, CampMaintenanceFence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    column = str(CreateColumn(CampSession.__table__.c.training_migration_paused).compile(dialect=mysql.dialect()))
    ddl = f'ALTER TABLE camp_session ADD COLUMN {column}'
    if not args.apply:
        print(ddl + ';')
        print(str(CreateTable(TrainingMigrationRun.__table__).compile(dialect=mysql.dialect())) + ';')
        print(str(CreateTable(CampMaintenanceFence.__table__).compile(dialect=mysql.dialect())) + ';')
        return
    import config
    engine = create_engine(config.SQLALCHEMY_DATABASE_URI)
    try:
        with engine.begin() as connection:
            inspector = inspect(connection)
            if not {'training_group_profile', 'camp_teaching_event'}.issubset(inspector.get_table_names()):
                raise RuntimeError('须先完成迁移 72、73、74')
            if 'training_migration_paused' not in {c['name'] for c in inspector.get_columns('camp_session')}:
                connection.execute(text(ddl))
            TrainingMigrationRun.__table__.create(connection, checkfirst=True)
            CampMaintenanceFence.__table__.create(connection, checkfirst=True)
    finally:
        engine.dispose()


if __name__ == '__main__':
    main()

"""迁移 74：培训组扩展、学习身份、考勤义务及课程责任。默认离线 DDL。

--apply 仅添加结构；不设置旧营切换标记、不回填人员、不导入 app 或运行调度器。
"""
import argparse
import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateTable, CreateIndex, CreateColumn
import models
from models import TrainingGroupProfile, CampTeachingEvent

COLUMNS = [('camp_session', 'training_groups_ready'), ('camp_member', 'learning_enabled'),
           ('camp_member', 'attendance_required'), ('camp_member', 'recruitment_unit_id'),
           ('camp_course_assignment', 'unit_id')]
FOREIGN_KEYS = [
    ('camp_member', 'fk_cm_recruitment_unit', 'recruitment_unit_id'),
    ('camp_course_assignment', 'fk_assignment_training_unit', 'unit_id')]
TABLES = [TrainingGroupProfile.__table__, CampTeachingEvent.__table__]


def column_ddl(table, column):
    definition = str(CreateColumn(models.db.metadata.tables[table].c[column]).compile(dialect=mysql.dialect()))
    return f'ALTER TABLE {table} ADD COLUMN {definition}'


def foreign_ddl(table, name, column):
    return f'ALTER TABLE {table} ADD CONSTRAINT {name} FOREIGN KEY ({column}) REFERENCES camp_unit(id)'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if not args.apply:
        for t, c in COLUMNS:
            print(column_ddl(t, c) + ';')
        for t, name, c in FOREIGN_KEYS:
            print(foreign_ddl(t, name, c) + ';')
        for table in TABLES:
            print(str(CreateTable(table).compile(dialect=mysql.dialect())) + ';')
            for index in table.indexes:
                print(str(CreateIndex(index).compile(dialect=mysql.dialect())) + ';')
        print('CREATE INDEX ix_camp_course_assignment_unit_id ON camp_course_assignment (unit_id);')
        return
    import config
    engine = create_engine(config.SQLALCHEMY_DATABASE_URI)
    try:
        with engine.begin() as conn:
            inspector = inspect(conn)
            required = {'camp_session', 'camp_member', 'camp_unit', 'camp_group', 'camp_course_assignment', 'legacy_unit_membership_map'}
            if not required.issubset(inspector.get_table_names()):
                raise RuntimeError('缺少前置表，请先完成迁移 72/73 和既有营期迁移')
            for table, column in COLUMNS:
                if column not in {c['name'] for c in inspect(conn).get_columns(table)}:
                    conn.execute(text(column_ddl(table, column)))
            for table, name, column in FOREIGN_KEYS:
                if not any(fk['constrained_columns'] == [column] for fk in inspect(conn).get_foreign_keys(table)):
                    conn.execute(text(foreign_ddl(table, name, column)))
            models.db.metadata.create_all(conn, tables=TABLES, checkfirst=True)
            if not any(i['column_names'] == ['unit_id'] for i in inspect(conn).get_indexes('camp_course_assignment')):
                conn.execute(text('CREATE INDEX ix_camp_course_assignment_unit_id ON camp_course_assignment (unit_id)'))
    finally:
        engine.dispose()


if __name__ == '__main__':
    main()

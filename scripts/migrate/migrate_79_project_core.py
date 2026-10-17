"""迁移79：独立项目及日历；默认只输出DDL，--apply仅创建缺失表，不迁移存量项目。"""
import argparse
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from sqlalchemy import create_engine,inspect,text
from sqlalchemy.schema import CreateTable,CreateIndex
from sqlalchemy.dialects import mysql
import models
from project_models import PROJECT_CORE_TABLES


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    if not args.apply:
        for table in PROJECT_CORE_TABLES:
            print(str(CreateTable(table).compile(dialect=mysql.dialect()))+';')
            for index in table.indexes:print(str(CreateIndex(index).compile(dialect=mysql.dialect()))+';')
        return
    import config
    engine=create_engine(config.SQLALCHEMY_DATABASE_URI)
    try:
        for table in PROJECT_CORE_TABLES:table.create(engine,checkfirst=True)
        # 支持本轮开发灰度早期结构的增量补齐，仅涉及新项目表。
        if 'migration_ready' not in {c['name'] for c in inspect(engine).get_columns('project_core_project')}:
            with engine.begin() as connection:connection.execute(text('ALTER TABLE project_core_project ADD COLUMN migration_ready BOOLEAN NOT NULL DEFAULT 1'))
        print('独立项目结构已就绪；存量项目、成员、评分和积分未迁移。')
    finally:engine.dispose()


if __name__=='__main__':main()

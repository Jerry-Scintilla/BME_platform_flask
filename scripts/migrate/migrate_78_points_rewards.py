"""迁移 78：训练营积分规则与发放队列。默认仅输出建表语句，--apply 才执行。"""
import argparse
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from sqlalchemy import create_engine
from sqlalchemy.schema import CreateTable, CreateIndex
from sqlalchemy.dialects import mysql
import models
from points_models import PointsRule, PointsRuleVersion, PointsReward, PointsEvent

TABLES=[m.__table__ for m in (PointsRule,PointsRuleVersion,PointsReward,PointsEvent)]

def main():
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('--apply',action='store_true'); args=p.parse_args()
    if not args.apply:
        for table in TABLES:
            print(str(CreateTable(table).compile(dialect=mysql.dialect()))+';')
            for index in table.indexes: print(str(CreateIndex(index).compile(dialect=mysql.dialect()))+';')
        return
    import config
    engine=create_engine(config.SQLALCHEMY_DATABASE_URI)
    try:
        for table in TABLES: table.create(engine,checkfirst=True)
        print('积分奖励表已就绪；未启用规则，未发放积分。')
    finally: engine.dispose()

if __name__=='__main__': main()

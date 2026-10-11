"""迁移 73：营期组绑定、旧成员映射及人员外键级联。默认仅离线输出 DDL。

--apply 仅修改结构，不迁移任何成员；不导入 app、不启动调度器。
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateTable, CreateIndex
import models
from models import LegacyUnitMembershipMap

ADD_COLUMN = 'ALTER TABLE camp_unit ADD COLUMN group_id INT NULL'
ADD_UNIQUE = 'ALTER TABLE camp_unit ADD UNIQUE KEY uq_camp_unit_group (group_id)'
ADD_FK = ('ALTER TABLE camp_unit ADD CONSTRAINT fk_camp_unit_group '
          'FOREIGN KEY (group_id) REFERENCES camp_group(id)')
CASCADE_FK = ('ALTER TABLE group_membership ADD CONSTRAINT fk_group_member_user_person '
              'FOREIGN KEY (user_id, person_id) REFERENCES user(id, person_id) ON UPDATE CASCADE')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    table = LegacyUnitMembershipMap.__table__
    if not args.apply:
        for ddl in (ADD_COLUMN, ADD_UNIQUE, ADD_FK):
            print(ddl + ';')
        print(str(CreateTable(table).compile(dialect=mysql.dialect())) + ';')
        for index in table.indexes:
            print(str(CreateIndex(index).compile(dialect=mysql.dialect())) + ';')
        print('-- 若迁移 72 已创建不带级联的外键，先 DROP 原 fk_group_member_user_person 再执行：')
        print(CASCADE_FK + ';')
        return
    import config
    engine = create_engine(config.SQLALCHEMY_DATABASE_URI)
    try:
        with engine.begin() as conn:
            inspector = inspect(conn)
            required = {'camp_unit', 'camp_unit_member', 'camp_group', 'group_membership'}
            if not required.issubset(inspector.get_table_names()):
                raise RuntimeError('缺少前置表，请先完成迁移 72 和既有项目表迁移')
            if 'group_id' not in {column['name'] for column in inspector.get_columns('camp_unit')}:
                conn.execute(text(ADD_COLUMN))
            if not any(index['column_names'] == ['group_id'] and index.get('unique')
                       for index in inspector.get_indexes('camp_unit')):
                conn.execute(text(ADD_UNIQUE))
            if not any(fk['constrained_columns'] == ['group_id'] for fk in inspector.get_foreign_keys('camp_unit')):
                conn.execute(text(ADD_FK))
            fk = next((item for item in inspector.get_foreign_keys('group_membership')
                       if item['constrained_columns'] == ['user_id', 'person_id']), None)
            if fk is None or fk.get('options', {}).get('onupdate') != 'CASCADE':
                if fk is not None:
                    # 约束名来自服务端 introspection，反引号转义后使用。
                    name = fk['name'].replace('`', '``')
                    conn.execute(text(f'ALTER TABLE group_membership DROP FOREIGN KEY `{name}`'))
                conn.execute(text(CASCADE_FK))
            table.create(conn, checkfirst=True)
        print('营期绑定结构已就绪；未映射成员、未切换任何旧单元。')
    finally:
        engine.dispose()


if __name__ == '__main__':
    main()

"""迁移 81：平台级积分规则 + 文章先审后发。默认仅输出 DDL，--apply 才执行；不回填历史。

- points_rule / points_reward 的 camp_id 改可空（NULL = 平台级规则与发放，外键保留）
- points_rule_version 加 daily_checkin 时长奖励参数（bonus_unit_hours / bonus_amount / bonus_cap）
- article_v2 加审核留痕（submitted_at / reviewed_by / reviewed_at / reject_reason）
"""
import argparse
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from sqlalchemy import create_engine, inspect, text

DDL_POINTS_RULE_NULLABLE = 'ALTER TABLE points_rule MODIFY camp_id INT NULL'
DDL_POINTS_REWARD_NULLABLE = 'ALTER TABLE points_reward MODIFY camp_id INT NULL'
DDL_VERSION_COLUMNS = (
    'ALTER TABLE points_rule_version '
    'ADD COLUMN bonus_unit_hours INT NULL, '
    'ADD COLUMN bonus_amount INT NULL, '
    'ADD COLUMN bonus_cap INT NULL'
)
DDL_ARTICLE_COLUMNS = (
    'ALTER TABLE article_v2 '
    'ADD COLUMN submitted_at DATETIME NULL, '
    'ADD COLUMN reviewed_by INT NULL, '
    'ADD COLUMN reviewed_at DATETIME NULL, '
    'ADD COLUMN reject_reason VARCHAR(512) NULL'
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if not args.apply:
        for ddl in (DDL_POINTS_RULE_NULLABLE, DDL_POINTS_REWARD_NULLABLE,
                    DDL_VERSION_COLUMNS, DDL_ARTICLE_COLUMNS):
            print(ddl + ';')
        return
    import config
    engine = create_engine(config.SQLALCHEMY_DATABASE_URI)
    try:
        with engine.begin() as connection:
            insp = inspect(connection)
            rule_nullable = {c['name']: c['nullable'] for c in insp.get_columns('points_rule')}
            if not rule_nullable.get('camp_id', False):
                connection.execute(text(DDL_POINTS_RULE_NULLABLE))
            reward_nullable = {c['name']: c['nullable'] for c in insp.get_columns('points_reward')}
            if not reward_nullable.get('camp_id', False):
                connection.execute(text(DDL_POINTS_REWARD_NULLABLE))
            version_cols = {c['name'] for c in insp.get_columns('points_rule_version')}
            if not {'bonus_unit_hours', 'bonus_amount', 'bonus_cap'} <= version_cols:
                connection.execute(text(DDL_VERSION_COLUMNS))
            article_cols = {c['name'] for c in insp.get_columns('article_v2')}
            if not {'submitted_at', 'reviewed_by', 'reviewed_at', 'reject_reason'} <= article_cols:
                connection.execute(text(DDL_ARTICLE_COLUMNS))
        print('平台积分与文章审核字段已就绪；未建规则，未发放积分，不追溯历史。')
    finally:
        engine.dispose()


if __name__ == '__main__':
    main()

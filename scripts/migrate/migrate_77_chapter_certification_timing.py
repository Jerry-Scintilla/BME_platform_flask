"""迁移 77：认证时效快照。默认仅输出 DDL，--apply 才更新配置库；不回算历史。"""
import argparse
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from sqlalchemy import create_engine, inspect, text

DDL = 'ALTER TABLE camp_chapter_certification ADD COLUMN meeting_timings JSON NULL'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if not args.apply:
        print(DDL + ';')
        return
    import config
    engine = create_engine(config.SQLALCHEMY_DATABASE_URI)
    try:
        with engine.begin() as connection:
            columns = inspect(connection).get_columns('camp_chapter_certification')
            if 'meeting_timings' not in {c['name'] for c in columns}:
                connection.execute(text(DDL))
        print('认证时效快照字段已就绪；历史记录未回算。')
    finally:
        engine.dispose()


if __name__ == '__main__':
    main()

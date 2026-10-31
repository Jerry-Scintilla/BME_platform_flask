"""Discovery schema. Print MySQL DDL by default; apply only with --apply."""
import argparse
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.schema import CreateTable, CreateIndex
from sqlalchemy.dialects import mysql
from models import CommunityVisit


def apply(engine):
    CommunityVisit.__table__.create(engine, checkfirst=True)
    if 'featured_at' not in {c['name'] for c in inspect(engine).get_columns('discussion_thread')}:
        with engine.begin() as connection:
            connection.execute(text('ALTER TABLE discussion_thread ADD COLUMN featured_at DATETIME NULL'))
    with engine.begin() as connection:
        connection.execute(text('UPDATE discussion_thread SET featured_at=created_at WHERE is_essence=1 AND featured_at IS NULL'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if args.apply:
        import config
        engine = create_engine(config.SQLALCHEMY_DATABASE_URI)
        try:
            apply(engine)
        finally:
            engine.dispose()
    else:
        dialect = mysql.dialect()
        print(str(CreateTable(CommunityVisit.__table__).compile(dialect=dialect)) + ';')
        for index in CommunityVisit.__table__.indexes:
            print(str(CreateIndex(index).compile(dialect=dialect)) + ';')
        print('ALTER TABLE discussion_thread ADD COLUMN featured_at DATETIME NULL;')
        print('UPDATE discussion_thread SET featured_at=created_at WHERE is_essence=1 AND featured_at IS NULL;')


if __name__ == '__main__':
    main()

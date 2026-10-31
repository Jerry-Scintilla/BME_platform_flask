"""Reading-week schema. Offline DDL by default; --apply updates the configured DB.

Run with old application workers stopped. Additive, rerunnable; never drops content.
Backfill preserves actual reply targets and aborts on cycles or cross-thread links.
"""
import argparse
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.schema import CreateTable
from sqlalchemy.dialects import mysql
from models import CommunityTag, CommunityBook, DiscussionThreadTag

TABLES = [CommunityTag.__table__, CommunityBook.__table__, DiscussionThreadTag.__table__]
COLUMNS = {
    'discussion_thread': [('book_id', 'INT NULL'), ('author_closed_at', 'DATETIME NULL'), ('client_request_id', 'VARCHAR(64) NULL')],
    'discussion_reply': [('root_reply_id', 'INT NULL'), ('client_request_id', 'VARCHAR(64) NULL')],
}
INDEXES = [
    ('discussion_thread', 'ix_discussion_thread_book_id', 'book_id', False),
    ('discussion_thread', 'uq_thread_request', 'author_id, client_request_id', True),
    ('discussion_reply', 'ix_discussion_reply_root_reply_id', 'root_reply_id', False),
    ('discussion_reply', 'uq_reply_request', 'author_id, client_request_id', True),
    ('discussion_thread_tag', 'ix_thread_tag_tag_thread', 'tag_id, thread_id', False),
    ('discussion_reply', 'ix_reply_root_status_created', 'root_reply_id, status, created_at, id', False),
]


def backfill(connection):
    rows = connection.execute(text('SELECT id, thread_id, parent_reply_id, root_reply_id FROM discussion_reply')).mappings().all()
    by_id = {r['id']: r for r in rows}
    roots = {}
    def resolve(row):
        trail, seen, cursor = [], set(), row
        while cursor['parent_reply_id']:
            if cursor['id'] in roots:
                root = roots[cursor['id']]
                break
            if cursor['id'] in seen:
                raise RuntimeError(f"Reply cycle at {cursor['id']}; fix the legacy data before retrying")
            seen.add(cursor['id']); trail.append(cursor['id'])
            parent = by_id.get(cursor['parent_reply_id'])
            if not parent or parent['thread_id'] != row['thread_id']:
                raise RuntimeError(f"Broken reply relation at {cursor['id']}; no targets were rewritten")
            cursor = parent
        else:
            root = cursor['id']
        for reply_id in trail:
            roots[reply_id] = root
        return root
    updates = []
    for row in rows:
        root = resolve(row) if row['parent_reply_id'] else None
        if row['root_reply_id'] != root:
            updates.append(dict(root=root, id=row['id']))
    if updates:
        connection.execute(text('UPDATE discussion_reply SET root_reply_id=:root WHERE id=:id'), updates)
    return len(updates)


def apply(engine):
    # Create metadata tables first; existing user/discussion tables are never recreated.
    for table in TABLES:
        table.create(engine, checkfirst=True)
    for table, columns in COLUMNS.items():
        existing = {c['name'] for c in inspect(engine).get_columns(table)}
        for name, definition in columns:
            if name not in existing:
                with engine.begin() as connection:
                    connection.execute(text(f'ALTER TABLE {table} ADD COLUMN {name} {definition}'))
    for table, name, columns, unique in INDEXES:
        inspector = inspect(engine)
        names = {i['name'] for i in inspector.get_indexes(table)} | {i['name'] for i in inspector.get_unique_constraints(table)}
        if name not in names:
            with engine.begin() as connection:
                connection.execute(text(f'CREATE {"UNIQUE " if unique else ""}INDEX {name} ON {table} ({columns})'))
    if engine.dialect.name == 'mysql':
        fks = inspect(engine).get_foreign_keys('discussion_thread')
        if not any(f['constrained_columns'] == ['book_id'] for f in fks):
            with engine.begin() as connection:
                connection.execute(text('ALTER TABLE discussion_thread ADD CONSTRAINT fk_thread_book FOREIGN KEY (book_id) REFERENCES community_book(id)'))
        # MySQL TEXT is only 64KiB; MEDIUMTEXT comfortably stores 10k Unicode codepoints.
        for table in ('discussion_thread', 'discussion_reply'):
            content = next(c for c in inspect(engine).get_columns(table) if c['name'] == 'content')
            if str(content['type']).upper() == 'TEXT':
                with engine.begin() as connection:
                    connection.execute(text(f'ALTER TABLE {table} MODIFY content MEDIUMTEXT NOT NULL'))
    with engine.begin() as connection:
        count = backfill(connection)
    return count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if not args.apply:
        for table in TABLES:
            print(str(CreateTable(table).compile(dialect=mysql.dialect())) + ';')
        for table, columns in COLUMNS.items():
            for name, definition in columns:
                print(f'ALTER TABLE {table} ADD COLUMN {name} {definition};')
        for table, name, columns, unique in INDEXES:
            print(f'CREATE {"UNIQUE " if unique else ""}INDEX {name} ON {table} ({columns});')
        print('ALTER TABLE discussion_thread ADD CONSTRAINT fk_thread_book FOREIGN KEY (book_id) REFERENCES community_book(id);')
        for table in ('discussion_thread', 'discussion_reply'):
            print(f'ALTER TABLE {table} MODIFY content MEDIUMTEXT NOT NULL;')
        print('-- apply also validates and backfills root_reply_id for all legacy reply depths')
        return
    import config
    engine = create_engine(config.SQLALCHEMY_DATABASE_URI)
    try:
        print(f'Reading schema ready; backfilled {apply(engine)} replies.')
    finally:
        engine.dispose()


if __name__ == '__main__':
    main()

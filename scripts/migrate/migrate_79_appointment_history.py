"""Explicit MySQL migration; no app import, schedulers, or fabricated audit events.

Run with --apply against an explicitly selected local database first. Without
--apply only the planned statements are printed; no database is contacted.
"""
import argparse
import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))
from sqlalchemy import create_engine, inspect, text

COLUMNS = {
    'person_id': 'INT NULL', 'org_slot_snapshot': 'VARCHAR(10) NULL',
    'record_source': "VARCHAR(20) NOT NULL DEFAULT 'legacy'",
    'source_note': 'VARCHAR(500) NULL', 'historical_year': 'INT NULL',
    'appointed_at': 'DATETIME NULL', 'ended_at': 'DATETIME NULL',
}


def upgrade(connection):
    inspector = inspect(connection)
    columns = {c['name'] for c in inspector.get_columns('club_officer')}
    for name, ddl in COLUMNS.items():
        if name not in columns:
            connection.execute(text(f'ALTER TABLE club_officer ADD COLUMN {name} {ddl}'))
    connection.execute(text('ALTER TABLE club_officer MODIFY COLUMN term_start DATE NULL'))
    indexes = {i['name'] for i in inspect(connection).get_indexes('club_officer')}
    if 'ix_club_officer_person_id' not in indexes:
        connection.execute(text('CREATE INDEX ix_club_officer_person_id ON club_officer (person_id)'))
    connection.execute(text('UPDATE club_officer o JOIN user u ON u.id=o.user_id SET o.person_id=u.person_id WHERE o.person_id IS NULL'))
    connection.execute(text("UPDATE club_officer o JOIN club_position p ON p.id=o.title_id SET o.org_slot_snapshot=COALESCE(p.org_slot, IF(p.sort_rank < 10, 'club', 'group')) WHERE o.org_slot_snapshot IS NULL"))
    # Operation times are taken only from real events, never from migration time.
    for kind, field in [('officer_appointed', 'appointed_at'), ('officer_ended', 'ended_at')]:
        connection.execute(text(f"UPDATE club_officer o JOIN (SELECT officer_id, MIN(occurred_at) AS t FROM club_org_event WHERE kind=:kind GROUP BY officer_id) e ON e.officer_id=o.id SET o.{field}=e.t WHERE o.{field} IS NULL"), {'kind': kind})


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if not args.apply:
        print('Plan: add appointment history fields, allow unknown historical dates, preserve existing rows and real event times. Use --apply only after selecting and backing up the target database.')
    else:
        import config
        with create_engine(config.SQLALCHEMY_DATABASE_URI).begin() as connection:
            upgrade(connection)
        print('Appointment history migration complete')

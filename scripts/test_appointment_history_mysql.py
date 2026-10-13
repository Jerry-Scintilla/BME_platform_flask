"""Explicit disposable LOCAL MySQL verification of migration 79.

Only --run creates a uniquely named test database. Never touches sysu_bme tables.
"""
import argparse
import importlib.util
import os
import sys
import uuid
from pathlib import Path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


def run():
    import config
    uri = make_url(config.SQLALCHEMY_DATABASE_URI)
    if uri.host not in ('127.0.0.1', 'localhost', '::1'):
        raise RuntimeError('This verifier accepts a local MySQL host only')
    name = 'bme_history_verify_' + uuid.uuid4().hex[:12]
    admin = create_engine(uri.set(database=None))
    engine = None
    created = False
    try:
        with admin.begin() as c:
            c.execute(text(f'CREATE DATABASE `{name}` CHARACTER SET utf8mb4'))
            created = True
        engine = create_engine(uri.set(database=name))
        with engine.begin() as c:
            for ddl in (
                'CREATE TABLE user (id INT PRIMARY KEY, person_id INT)',
                'CREATE TABLE club_position (id INT PRIMARY KEY, org_slot VARCHAR(10), sort_rank INT)',
                'CREATE TABLE club_officer (id INT PRIMARY KEY, user_id INT, title_id INT, term_start DATE NOT NULL, term_end DATE NULL, status VARCHAR(20))',
                'CREATE TABLE club_org_event (id INT PRIMARY KEY, officer_id INT, kind VARCHAR(30), occurred_at DATETIME)',
            ):
                c.execute(text(ddl))
            c.execute(text('INSERT INTO user VALUES (1, 42)'))
            c.execute(text("INSERT INTO club_position VALUES (1,'club',1)"))
            c.execute(text("INSERT INTO club_officer VALUES (1,1,1,'2025-01-01',NULL,'active'),(2,1,1,'2024-01-01','2024-12-31','ended')"))
            c.execute(text("INSERT INTO club_org_event VALUES (1,1,'officer_appointed','2025-01-03 09:00:00')"))
        spec = importlib.util.spec_from_file_location('migration79', Path(__file__).parent / 'migrate/migrate_79_appointment_history.py')
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        for _ in range(2):
            with engine.begin() as c:
                migration.upgrade(c)
        with engine.begin() as c:
            rows = c.execute(text('SELECT * FROM club_officer ORDER BY id')).mappings().all()
            assert len(rows) == 2
            assert rows[0]['term_start'].isoformat() == '2025-01-01'
            assert rows[0]['person_id'] == 42
            assert rows[0]['org_slot_snapshot'] == 'club'
            assert rows[0]['record_source'] == 'legacy'
            assert rows[0]['appointed_at'].isoformat() == '2025-01-03T09:00:00'
            assert rows[1]['appointed_at'] is None and rows[1]['ended_at'] is None
            c.execute(text("INSERT INTO club_officer (id,user_id,status,record_source,historical_year) VALUES (3,1,'ended','backfill',2023)"))
            assert c.execute(text('SELECT term_start FROM club_officer WHERE id=3')).scalar() is None
        print('PASS: local MySQL migration, repeat upgrade, retained dates, actual-event times, and unknown historical dates')
    finally:
        if engine:
            engine.dispose()
        if created:
            with admin.begin() as c:
                c.execute(text(f'DROP DATABASE `{name}`'))
        admin.dispose()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--run', action='store_true')
    if parser.parse_args().run:
        run()
    else:
        print('Use --run to verify migration 79 in a disposable local MySQL database')

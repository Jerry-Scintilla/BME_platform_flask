"""Explicit admission migration. Default is a plan without DB access."""
import argparse
import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))
from sqlalchemy import text


def upgrade(connection):
    connection.execute(text('''CREATE TABLE IF NOT EXISTS workbench_admission (
        user_id INT PRIMARY KEY, person_id INT NOT NULL, status VARCHAR(20) NOT NULL DEFAULT 'active',
        granted_by INT NULL, reason VARCHAR(200) NOT NULL, created_at DATETIME NOT NULL,
        revoked_at DATETIME NULL, INDEX ix_workbench_admission_person_id (person_id),
        CONSTRAINT fk_workbench_admission_user FOREIGN KEY (user_id) REFERENCES user(id)
    ) CHARACTER SET utf8mb4'''))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    if parser.parse_args().apply:
        import config
        from sqlalchemy import create_engine
        with create_engine(config.SQLALCHEMY_DATABASE_URI).begin() as connection:
            upgrade(connection)
        print('Workbench admission migration complete')
    else:
        print('Plan: add application admission independent of teaching and group membership. --apply executes on the selected database.')

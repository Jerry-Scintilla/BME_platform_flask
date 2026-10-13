"""Export only work-domain records and minimal identity projections. No credentials."""
import argparse
import hashlib
import json
import sys
from datetime import date, datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlalchemy import create_engine, inspect, text


def serialize(value):
    if isinstance(value, (date, datetime)): return value.isoformat()
    raise TypeError(type(value).__name__)


def export(output):
    import config
    engine = create_engine(config.SQLALCHEMY_DATABASE_URI)
    tables, user_ids = {}, set()
    with engine.connect().execution_options(isolation_level='REPEATABLE READ') as connection:
        with connection.begin():
            names = inspect(connection).get_table_names()
            selected = [n for n in names if n.startswith('work_') or n in (
                'club_group', 'club_position', 'club_membership', 'club_officer', 'club_org_event', 'person_workspace_restriction')]
            for name in selected:
                rows = [dict(r) for r in connection.execute(text('SELECT * FROM ' + name)).mappings()]
                tables[name] = rows
                for row in rows:
                    for key, value in row.items():
                        if key.endswith('user_id') or key in ('created_by', 'updated_by', 'invited_by', 'granted_by', 'appointed_by', 'ended_by', 'submitted_by', 'decided_by'):
                            if isinstance(value, int) and value > 0: user_ids.add(value)
            tables['work_notification'] = [dict(r) for r in connection.execute(text(
                "SELECT id,user_id,title,content,source_type,source_id,is_read,created_at FROM notification WHERE category='work'")).mappings()]
            user_ids.update(r['user_id'] for r in tables['work_notification'])
            users = []
            for uid in sorted(user_ids):
                row = connection.execute(text('SELECT u.id,u.username,u.person_id,u.avatar_url,u.status,u.lifecycle,u.security_version,p.verification_status FROM user u LEFT JOIN person p ON p.id=u.person_id WHERE u.id=:uid'), {'uid':uid}).mappings().first()
                if row: users.append(dict(row))
            tables['directory_user'] = users
    engine.dispose()
    payload = json.loads(json.dumps(tables, default=serialize, ensure_ascii=False))
    hashes = {name:hashlib.sha256(json.dumps(rows,sort_keys=True,ensure_ascii=False).encode()).hexdigest() for name,rows in payload.items()}
    result = {'schema_version':1,'issuer':'bme-training','tables':payload,'sha256':hashes}
    path = Path(output)
    with path.open('x',encoding='utf-8') as handle:json.dump(result,handle,ensure_ascii=False,indent=2)
    path.chmod(0o600)
    print(json.dumps({'exported_tables':{n:len(rows) for n,rows in payload.items()},'output':str(path)},ensure_ascii=False))


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',required=True)
    export(parser.parse_args().output)

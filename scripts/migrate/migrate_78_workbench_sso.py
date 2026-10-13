"""Independent workbench SSO code table. Preview by default, --apply to create."""
import argparse
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from sqlalchemy import create_engine
from sqlalchemy.schema import CreateTable
from sqlalchemy.dialects import mysql
from workbench_sso_models import WorkbenchSSOCode
import models  # register the existing user FK target

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if not args.apply:
        print(CreateTable(WorkbenchSSOCode.__table__).compile(dialect=mysql.dialect()))
    else:
        import config
        engine = create_engine(config.SQLALCHEMY_DATABASE_URI)
        WorkbenchSSOCode.__table__.create(engine, checkfirst=True)
        engine.dispose()
        print('Workbench SSO code table ready; no users or permissions changed.')

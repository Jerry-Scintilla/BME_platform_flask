"""营期成员映射只读预览；无 apply 参数，不迁移任何数据。"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument('--project-unit', type=int)
    scope.add_argument('--training-camp', type=int)
    args = parser.parse_args()
    from flask import Flask
    from sqlalchemy import inspect
    import config
    from exts import db
    import models
    from services.camp_groups.migration import preview_project
    from services.camp_groups.training_migration import preview as preview_training
    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = config.SQLALCHEMY_DATABASE_URI
    db.init_app(app)
    with app.app_context():
        try:
            if args.project_unit is not None:
                if 'group_id' not in {c['name'] for c in inspect(db.engine).get_columns('camp_unit')}:
                    raise RuntimeError('请先完成迁移 73 的结构准备；本工具不会自动建表')
                result = preview_project(args.project_unit)
            else:
                result = preview_training(args.training_camp)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        finally:
            db.session.rollback()
            db.session.remove()
            db.engine.dispose()


if __name__ == '__main__':
    main()

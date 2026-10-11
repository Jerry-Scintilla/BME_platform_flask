"""旧培训营映射工具：默认 preview；写操作需显式 action、--apply 和有效管理员。

只导入最小 Flask 数据库上下文，不启动 app、调度器、建表或通知。输出含人员与教学档案，须安全保存。
"""
import argparse
import json
import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--camp', type=int, required=True)
    parser.add_argument('--action', choices=['preview','freeze','map','verify','switch','resume','restore','export'], default='preview')
    parser.add_argument('--actor', type=int)
    parser.add_argument('--run', type=int)
    parser.add_argument('--expected-digest')
    parser.add_argument('--decisions', help='JSON 文件，格式见 docs/group-core.md')
    parser.add_argument('--output', help='新文件路径；以 0600 创建，拒绝覆盖既有文件')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    writing = args.action in {'freeze','map','switch','resume','restore'}
    if writing and (not args.apply or not args.actor):
        parser.error('写操作需要 --apply 和 --actor；默认预览不写入')
    if args.action not in ('preview','freeze') and not args.run:
        parser.error('此操作需要 --run 指定迁移批次')
    if args.action == 'map' and not args.expected_digest:
        parser.error('映射需要预览输出的 --expected-digest')
    if writing and args.output:
        parser.error('写命令不接受 --output；先执行 export 保存快照，再单独写入')
    from flask import Flask
    from contextlib import ExitStack
    from sqlalchemy import inspect
    import config
    from exts import db
    from services.camp_groups import training_migration as migration, maintenance
    from services.groups import GroupError
    decisions = json.loads(open(args.decisions, encoding='utf8').read()) if args.decisions else None
    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = config.SQLALCHEMY_DATABASE_URI
    db.init_app(app)
    with app.app_context():
        try:
            inspector = inspect(db.engine)
            if not {'training_migration_run', 'camp_maintenance_fence'}.issubset(inspector.get_table_names()) or 'training_migration_paused' not in {c['name'] for c in inspector.get_columns('camp_session')}:
                raise GroupError('缺少迁移 75 结构；工具不会自动建表')
            with ExitStack() as stack:
                if writing:
                    stack.enter_context(maintenance.lease(f'camp:{args.camp}'))
                stack.enter_context(maintenance.lease('scheduler'))
                if args.action == 'preview':
                    result = migration.preview(args.camp, decisions)
                elif args.action == 'verify':
                    result = migration.verify(args.camp, args.run)
                elif args.action == 'export':
                    run = migration._run(args.camp, args.run)
                    result = dict(check=migration.verify(args.camp,args.run), baseline=run.baseline,
                        mapping=run.mapping, journal=run.journal, current=migration.state_document(args.camp))
                else:
                    with db.session.begin_nested():
                        if args.action == 'freeze':
                            run = migration.freeze(args.camp,args.actor)
                        elif args.action == 'map':
                            run = migration.apply_mapping(args.camp,args.run,actor_user_id=args.actor,
                                expected_digest=args.expected_digest,decisions=decisions)
                        else:
                            run = getattr(migration,args.action)(args.camp,args.run,args.actor)
                        result = dict(run_id=run.id, state=run.state)
                    db.session.commit()
                if not writing:
                    db.session.rollback()
            output = json.dumps(result,ensure_ascii=False,indent=2)
            if args.output:
                fd = os.open(args.output,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
                with os.fdopen(fd,'w',encoding='utf8') as handle:
                    handle.write(output+'\n')
            else:
                print(output)
        except GroupError as exc:
            db.session.rollback()
            print(json.dumps({'error':str(exc),'machine':exc.machine},ensure_ascii=False), file=sys.stderr)
            return 1
        finally:
            db.session.rollback()
            db.session.remove()
            db.engine.dispose()
    return 0


if __name__ == '__main__':
    sys.exit(main())

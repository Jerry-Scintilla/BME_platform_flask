"""按营共享/独占维护栅栏；独立连接跨业务提交持锁，SQLite 仅用于单进程检查。"""
from contextlib import contextmanager, ExitStack
from functools import wraps
from threading import RLock
from sqlalchemy import select, text, create_engine
from sqlalchemy.pool import NullPool
from flask import request, g, jsonify, current_app
from hashlib import sha256
from exts import db
from services.groups import GroupError

_local_lock = RLock()


@contextmanager
def lease(key):
    with leases([key]):
        yield


def fence_statement(table, ids, *, shared, nowait=False):
    statement = select(table.c.camp_session_id).where(table.c.camp_session_id.in_(ids)).order_by(table.c.camp_session_id)
    # MySQL 的旧 LOCK IN SHARE MODE 语法不接受 NOWAIT；非等待共享锁使用 8.0+ FOR SHARE。
    if shared and nowait:
        return statement.suffix_with('FOR SHARE NOWAIT', dialect='mysql')
    return statement.with_for_update(read=shared, nowait=nowait)


@contextmanager
def leases(keys, *, timeout=0, shared=False):
    if db.engine.dialect.name == 'sqlite':
        with _local_lock:
            yield None
        return
    if db.engine.dialect.name != 'mysql':
        raise GroupError('维护栅栏尚不支持此数据库', status=503)
    lock_engine = current_app.extensions.get('camp_maintenance_lock_engine')
    if lock_engine is None:
        lock_engine = create_engine(db.engine.url, poolclass=NullPool)
        current_app.extensions['camp_maintenance_lock_engine'] = lock_engine
    from models import CampMaintenanceFence, CampSession
    from sqlalchemy.dialects.mysql import insert
    from sqlalchemy.exc import OperationalError
    ids = sorted({int(key.split(':', 1)[1]) for key in keys if key.startswith('camp:')})
    names = sorted({key for key in keys if not key.startswith('camp:')})
    with lock_engine.connect() as connection:
        namespace = sha256((db.engine.url.database or '').encode()).hexdigest()[:12]
        held = []
        try:
            # 首次访问才补技术锁行；已存在时不 INSERT，避免重复键检查取得排他锁。
            if ids:
                table = CampMaintenanceFence.__table__
                present = set(connection.execute(select(table.c.camp_session_id).where(table.c.camp_session_id.in_(ids))).scalars())
                missing = set(ids) - present
                if missing:
                    valid = connection.execute(select(CampSession.id).where(CampSession.id.in_(missing))).scalars().all()
                    if valid:
                        connection.execute(insert(table).prefix_with('IGNORE'), [{'camp_session_id': sid} for sid in valid])
                connection.commit()
                connection.execute(text('SET SESSION innodb_lock_wait_timeout = :seconds'), {'seconds': max(1, timeout)})
                connection.execute(fence_statement(table, ids, shared=shared, nowait=timeout == 0)).all()
            for key in names:
                name = f'bme:{namespace}:{key}'
                if connection.execute(text('SELECT GET_LOCK(:name, :timeout)'), {'name': name, 'timeout': timeout}).scalar() != 1:
                    raise GroupError('营期操作正在处理中，请稍后重试', machine='CAMP_MAINTENANCE_BUSY', status=503)
                held.append(name)
            yield connection
        except OperationalError as exc:
            if getattr(exc.orig, 'args', [None])[0] in (1205, 1213, 3572):
                raise GroupError('营期维护正在处理中，请稍后重试', machine='CAMP_MAINTENANCE_BUSY', status=503) from exc
            raise
        finally:
            try:
                connection.rollback()
                for name in reversed(held):
                    connection.execute(text('SELECT RELEASE_LOCK(:name)'), {'name': name})
            except Exception:
                connection.invalidate()
                raise


def scheduler_guard(fn):
    @wraps(fn)
    def guarded(*args, **kwargs):
        try:
            with lease('scheduler'):
                return fn(*args, **kwargs)
        except GroupError as exc:
            if exc.machine == 'CAMP_MAINTENANCE_BUSY':
                return 0
            raise
    return guarded


def assert_writable(camp):
    if camp and camp.training_migration_paused:
        raise GroupError('本营正在核对旧名单，暂时停止业务操作', machine='CAMP_MIGRATION_PAUSED', status=423)


def _request_scopes():
    import models as m
    args = request.view_args or {}
    if 'sid' in args:
        return [args['sid']]
    path = request.path
    resource = None
    if '/leave/' in path and 'lid' in args:
        resource = db.session.get(m.CampLeave, args['lid'])
    elif '/join-requests/' in path and 'rid' in args:
        resource = db.session.get(m.CampJoinRequest, args['rid'])
    elif '/units/' in path and 'uid' in args:
        resource = db.session.get(m.CampUnit, args['uid'])
    elif '/materials/' in path:
        if 'mid' in args:
            resource = db.session.get(m.CampChapterMaterial, args['mid'])
        elif 'aid' in args:
            attachment = db.session.get(m.CampChapterMaterialAttachment, args['aid'])
            resource = db.session.get(m.CampChapterMaterial, attachment.material_id) if attachment else None
    elif '/meetings/' in path:
        meeting_id = args.get('mid')
        if 'tid' in args:
            task = db.session.get(m.CampMeetingTask, args['tid'])
            meeting_id = task.meeting_id if task else None
        elif 'aid' in args:
            if '/task-attachments/' in path:
                attachment = db.session.get(m.CampMeetingTaskAttachment, args['aid'])
                submission = db.session.get(m.CampMeetingTaskSubmission, attachment.submission_id) if attachment else None
                task = db.session.get(m.CampMeetingTask, submission.task_id) if submission else None
                meeting_id = task.meeting_id if task else None
            else:
                attachment = db.session.get(m.CampMeetingAttachment, args['aid'])
                meeting_id = attachment.meeting_id if attachment else None
        resource = db.session.get(m.CampMeeting, meeting_id) if meeting_id else None
    if resource is not None:
        return [resource.camp_session_id]
    # 未显式定位的个人聚合/学习入口可能推导多个营；保守覆盖所有培训营。
    # 该分支也覆盖 GET 内的历史自动收官写入，不能把 GET 一律当纯读。
    return [r[0] for r in db.session.execute(select(m.CampSession.id).where(m.CampSession.category == 'learning'))]


def install(app):
    @app.before_request
    def camp_maintenance_guard():
        endpoint = request.blueprint or ''
        if not (endpoint.startswith('camp') or endpoint == 'learningProgress'):
            return
        from models import CampSession
        stack = ExitStack()
        try:
            ids = sorted(set(_request_scopes()))
            connection = stack.enter_context(leases([f'camp:{sid}' for sid in ids], timeout=10, shared=True))
            # 锁行与业务行分离；用独立连接的新快照读维护状态，不给 CampSession 加排他锁。
            query = select(CampSession.training_migration_paused).where(CampSession.id.in_(ids))
            paused = (connection or db.session).execute(query).scalars().all()
            if any(paused):
                raise GroupError('本营正在核对旧名单，暂时停止业务操作', machine='CAMP_MIGRATION_PAUSED', status=423)
            # 作用域解析/账号校验可能已建立旧快照；只结束干净读事务，再从持锁后的版本读取业务。
            if not (db.session.new or db.session.dirty or db.session.deleted):
                db.session.rollback()
            else:
                raise GroupError('营期请求前存在未提交操作，请稍后重试', machine='CAMP_MAINTENANCE_BUSY', status=503)
            g.camp_maintenance_leases = stack
        except GroupError as exc:
            stack.close()
            db.session.rollback()
            return jsonify(code=exc.status, message=str(exc), machine=exc.machine), exc.status
        except Exception:
            stack.close()
            raise

    @app.teardown_request
    def release_camp_leases(_error):
        stack = g.pop('camp_maintenance_leases', None)
        if stack:
            stack.close()

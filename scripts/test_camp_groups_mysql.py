"""营期小组 MySQL 隔离回归；只允许本机 bme_group_test_* 临时库。

需显式 BME_TEST_MYSQL_URI，绝不读取业务数据库地址；不导入 app、不启动调度器。
在空临时库运行迁移 72–76（含重复执行），再以真实 MySQL 执行小组回归。
运行过程中清空临时库内数据，不可指定共享开发库。
"""
import os
import sys
import unittest
import runpy
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlalchemy import create_engine, text, inspect
from sqlalchemy.engine import make_url
from flask import Flask
import config
from exts import db
import models
import test_group_core as foundation


def main():
    uri = os.environ.get('BME_TEST_MYSQL_URI', '')
    if not uri:
        raise SystemExit('请提供本机隔离库 BME_TEST_MYSQL_URI')
    url = make_url(uri)
    if url.host not in ('127.0.0.1', 'localhost') or not (url.database or '').startswith('bme_group_test_'):
        raise SystemExit('只允许 localhost / 127.0.0.1 的 bme_group_test_* 临时库')
    engine = create_engine(url)
    if inspect(engine).get_table_names():
        raise SystemExit('需要全新空库，拒绝覆盖已有表')
    db.metadata.create_all(engine)
    from group_models import GROUP_CORE_TABLES
    new_tables = [t.name for t in GROUP_CORE_TABLES] + ['legacy_unit_membership_map', 'training_group_profile', 'camp_teaching_event', 'training_migration_run', 'camp_maintenance_fence']
    columns = {'camp_unit': ['group_id'], 'camp_session': ['training_groups_ready', 'training_migration_paused'],
               'camp_member': ['learning_enabled', 'attendance_required', 'recruitment_unit_id'],
               'camp_course_assignment': ['unit_id']}
    with engine.begin() as conn:
        conn.execute(text('SET FOREIGN_KEY_CHECKS=0'))
        for table, names in columns.items():
            for fk in inspect(conn).get_foreign_keys(table):
                if any(c in names for c in fk['constrained_columns']):
                    conn.execute(text(f"ALTER TABLE `{table}` DROP FOREIGN KEY `{fk['name']}`"))
            for name in names:
                conn.execute(text(f'ALTER TABLE `{table}` DROP COLUMN `{name}`'))
        for table in ('camp_meeting_task', 'camp_meeting_chapter_plan'):
            conn.execute(text(f'ALTER TABLE `{table}` MODIFY COLUMN created_at DATETIME NULL'))
        for table in new_tables:
            conn.execute(text(f'DROP TABLE `{table}`'))
        conn.execute(text('SET FOREIGN_KEY_CHECKS=1'))
    # 迁移脚本实际入口；配置仅在这个隔离进程中覆盖。
    for attempt in range(2):
        with patch.object(config, 'SQLALCHEMY_DATABASE_URI', uri):
            for num in (72, 73, 74, 75, 76):
                script = next(Path(__file__).parent.joinpath('migrate').glob(f'migrate_{num}_*.py'))
                with patch.object(sys, 'argv', [str(script), '--apply']):
                    runpy.run_path(str(script), run_name='__main__')
        print(f'迁移 72–76 第 {attempt + 1} 次通过', flush=True)
    for table, names in columns.items():
        assert set(names) <= {c['name'] for c in inspect(engine).get_columns(table)}

    original_teardown = foundation.GroupCoreSmoke.tearDown
    def setup(case):
        case.app = Flask(__name__)
        case.app.config['SQLALCHEMY_DATABASE_URI'] = uri
        db.init_app(case.app)
        case.ctx = case.app.app_context(); case.ctx.push()
        with db.engine.begin() as conn:
            conn.execute(text('SET FOREIGN_KEY_CHECKS=0'))
            for table in inspect(conn).get_table_names():
                conn.execute(text(f'TRUNCATE TABLE `{table}`'))
            conn.execute(text('SET FOREIGN_KEY_CHECKS=1'))
        case.settings = patch.multiple(config, IDENTITY_ENFORCEMENT_MODE='shadow', IDENTITY_VERIFY_GATE_DEFAULT='shadow', IDENTITY_VERIFY_GATES_RAW='')
        case.settings.start()
        case.actor = case.make_user('teacher', admin=True)
        case.owner = case.make_user('owner')
        case.student = case.make_user('student')
        db.session.commit()
        case.policy = foundation.FixturePolicy('training')
        case.project_policy = foundation.FixturePolicy('project')
        case.service = foundation.GroupService({'training': case.policy, 'project': case.project_policy})
        case.context = foundation.GovernanceContext('training', 10)
        case.sequence = 0
    def teardown(case):
        locks = case.app.extensions.get('camp_maintenance_lock_engine')
        if locks: locks.dispose()
        original_teardown(case)
    foundation.GroupCoreSmoke.setUp = setup
    foundation.GroupCoreSmoke.tearDown = teardown
    modules = ['test_group_core', 'test_camp_group_adapter', 'test_training_groups', 'test_training_cutover', 'test_training_operations', 'test_camp_group_cleanup']
    import test_training_groups as fixtures
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from services.camp_groups import training, training_access, maintenance
    from services.groups import GroupError

    class ConcurrencyTests(unittest.TestCase):
        setUp = fixtures.TrainingSmoke.setUp
        tearDown = teardown
        make_user = fixtures.TrainingSmoke.make_user

        def race(self, jobs):
            barrier = Barrier(len(jobs))
            app, sid, actor = self.app, self.camp.id, self.actor.id
            def worker(unit_id, uid):
                with app.app_context():
                    try:
                        # 显式先读快照，覆盖 InnoDB 默认 REPEATABLE READ 下的竞争。
                        db.session.get(models.CampSession, sid)
                        barrier.wait(timeout=10)
                        unit = db.session.get(models.CampUnit, unit_id)
                        training.add_role(unit, uid, 'learner', actor)
                        db.session.commit()
                        return 'ok'
                    except GroupError as exc:
                        db.session.rollback()
                        return exc.machine
                    finally:
                        db.session.remove()
            with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
                return list(pool.map(lambda args: worker(*args), jobs))

        def test_last_slot_is_atomic(self):
            db.session.get(models.TrainingGroupProfile, self.a.id).capacity = 1
            db.session.commit()
            results = self.race([(self.a.id, self.student.id), (self.a.id, self.other.id)])
            self.assertEqual(results.count('ok'), 1, results)
            self.assertEqual(sum(r in ('GROUP_CAPACITY_FULL', 'GROUP_VERSION_CONFLICT') for r in results), 1, results)
            db.session.rollback()
            self.assertEqual(len({m.user_id for r, m, u in training_access.role_query(self.camp.id, role='learner').all() if u.id == self.a.id}), 1)

        def test_same_course_has_one_responsible_group(self):
            results = self.race([(self.a.id, self.student.id), (self.b.id, self.student.id)])
            self.assertEqual(results.count('ok'), 1, results)
            self.assertEqual(results.count('COURSE_GROUP_CONFLICT'), 1, results)
            db.session.rollback()
            self.assertEqual(models.CampCourseAssignment.query.filter_by(student_user_id=self.student.id).count(), 1)

        def test_shared_business_and_exclusive_maintenance(self):
            app, sid = self.app, self.camp.id
            key = f'camp:{sid}'
            def acquire(shared):
                with app.app_context():
                    try:
                        with maintenance.leases([key], shared=shared):
                            return 'ok'
                    except GroupError as exc:
                        return exc.machine
            with maintenance.leases([key], shared=True):
                with ThreadPoolExecutor(max_workers=1) as pool:
                    self.assertEqual(pool.submit(acquire, True).result(timeout=10), 'ok')
                    self.assertEqual(pool.submit(acquire, False).result(timeout=10), 'CAMP_MAINTENANCE_BUSY')
            self.assertEqual(acquire(False), 'ok')

    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName(name) for name in modules)
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(ConcurrencyTests))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    engine.dispose()
    raise SystemExit(0 if result.wasSuccessful() else 1)

if __name__ == '__main__':
    main()

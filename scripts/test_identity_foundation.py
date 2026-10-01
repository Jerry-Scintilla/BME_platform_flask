"""D2 人员层的隔离回归；只使用 SQLite（assertions 覆盖规格第 3/8 章与 D2 完成标准）。

覆盖：
  模型/约束层：person 三件套同事务建立 / identity key 唯一裁决 / primary 唯一
               与复合外键生效（SQLite 开 PRAGMA foreign_keys）
  归一化：     canonicalize 只按机构规则（域名小写、本地部/netid 原样、拒猜测）
  账本：       白名单脱敏 / 与业务写同事务回滚（注入失败）
  幂等：       run_idempotent 同 key 同摘要复用 / 同 key 不同内容冲突 / 系统动作 actor=0
  并发：       同 email 并发注册不双建（文件 SQLite 两连接，唯一约束裁决）

用法（项目根）：.venv/bin/python scripts/test_identity_foundation.py
"""
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import sqlite3
from flask import Flask
from sqlalchemy import event
from sqlalchemy.dialects.mysql import LONGTEXT, MEDIUMTEXT
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.compiler import compiles


@compiles(MEDIUMTEXT, 'sqlite')
@compiles(LONGTEXT, 'sqlite')
def _mysql_text_on_sqlite(_element, _compiler, **_kwargs):
    return 'TEXT'


from exts import db  # noqa: E402
import models  # noqa: E402,F401  # 先于 create_all：不导入则 db.metadata 为空、建不出表


def _enable_sqlite_fk(engine):
    @event.listens_for(engine, 'connect')
    def _set_fk_pragma(dbapi_conn, connection_record):
        if isinstance(dbapi_conn, sqlite3.Connection):
            cursor = dbapi_conn.cursor()
            cursor.execute('PRAGMA foreign_keys=ON')
            cursor.close()


def _enable_sqlite_savepoints(engine):
    """pysqlite 事务处理与 SAVEPOINT 不兼容的官方 workaround（SQLAlchemy 文档
    「Serializable isolation / Savepoints」节）：关驱动隐式事务、手动 BEGIN。"""

    @event.listens_for(engine, 'connect')
    def _do_connect(dbapi_conn, connection_record):
        if isinstance(dbapi_conn, sqlite3.Connection):
            dbapi_conn.isolation_level = None

    @event.listens_for(engine, 'begin')
    def _do_begin(conn):
        conn.exec_driver_sql('BEGIN')


class IdentityFoundationTestBase(unittest.TestCase):
    """共用脚手架：内存 SQLite（外键强制 + savepoint 可用）+ 各用例自建用户/人员。

    pysqlite 驱动默认事务处理不支持 SAVEPOINT（回滚保存点会把整个会话事务标记
    PendingRollback）——services/identity 的「冲突只回滚保存点」语义依赖 savepoint，
    按 SQLAlchemy 官方配方关掉驱动隐式事务、手动发 BEGIN（仅测试栈；MySQL 天然支持）。
    """

    def setUp(self):
        self.app = Flask(__name__)
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        db.init_app(self.app)
        self.context = self.app.app_context()
        self.context.push()
        _enable_sqlite_fk(db.engine)
        _enable_sqlite_savepoints(db.engine)
        db.create_all()
        from models import UserModel
        self.UserModel = UserModel

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def make_user(self, email='u@example.com', **kw):
        user = self.UserModel(username=kw.pop('username', '用户'), email=email, **kw)
        user.set_password('x' * 32)
        db.session.add(user)
        db.session.flush()
        return user


class PersonLayerTest(IdentityFoundationTestBase):
    """person 三件套（Person + user.person_id + primary）与唯一/复合约束。"""

    def test_create_provisional_builds_all_three_rows(self):
        from models import PersonPrimaryAccountModel
        from services.identity import person as identity_person
        user = self.make_user()
        person, created = identity_person.create_provisional(user)
        db.session.commit()
        self.assertTrue(created)
        self.assertIsNotNone(person.id)
        self.assertEqual(user.person_id, person.id)
        self.assertEqual(person.verification_status, 'unverified')
        self.assertEqual(person.record_status, 'active')
        self.assertTrue(person.public_id.startswith('p'))
        self.assertEqual(len(person.public_id), 17)
        ppa = db.session.get(PersonPrimaryAccountModel, person.id)
        self.assertEqual((ppa.person_id, ppa.user_id), (person.id, user.id))

    def test_create_provisional_idempotent(self):
        from models import PersonModel, PersonPrimaryAccountModel
        from services.identity import person as identity_person
        user = self.make_user()
        p1, c1 = identity_person.create_provisional(user)
        db.session.commit()
        p2, c2 = identity_person.create_provisional(user)
        db.session.commit()
        self.assertTrue(c1)
        self.assertFalse(c2)
        self.assertEqual(p1.id, p2.id)
        self.assertEqual(PersonModel.query.count(), 1)
        self.assertEqual(PersonPrimaryAccountModel.query.count(), 1)

    def test_create_provisional_repairs_missing_primary(self):
        """半途状态（有 person 无 primary）能被幂等入口修复，不双建人员。"""
        from models import PersonPrimaryAccountModel
        from services.identity import person as identity_person
        user = self.make_user()
        person, _ = identity_person.create_provisional(user, ensure_primary=False)
        db.session.commit()
        self.assertEqual(PersonPrimaryAccountModel.query.count(), 0)
        _p2, _c = identity_person.create_provisional(user)  # ensure_primary 默认开
        db.session.commit()
        self.assertEqual(PersonPrimaryAccountModel.query.count(), 1)
        self.assertEqual(PersonPrimaryAccountModel.query.first().user_id, user.id)

    def test_primary_user_unique(self):
        """同一 user 不能成为两个人的主参与账号（UNIQUE(user_id) 裁决）。"""
        from models import PersonPrimaryAccountModel
        from services.identity import person as identity_person
        user = self.make_user()
        other = self.make_user(email='o@example.com')
        identity_person.create_provisional(user)
        p2, _ = identity_person.create_provisional(other)
        db.session.add(PersonPrimaryAccountModel(person_id=p2.id, user_id=user.id))
        with self.assertRaises(IntegrityError):
            db.session.flush()

    def test_primary_person_pk(self):
        """同一 person 只能有一个主参与账号（PK(person_id) 裁决）。"""
        from models import PersonPrimaryAccountModel
        from services.identity import person as identity_person
        user = self.make_user()
        other = self.make_user(email='o@example.com')
        person, _ = identity_person.create_provisional(user)
        identity_person.create_provisional(other)
        db.session.add(PersonPrimaryAccountModel(person_id=person.id, user_id=other.id))
        with self.assertRaises(IntegrityError):
            db.session.flush()

    def test_composite_fk_rejects_mismatched_pair(self):
        """复合外键生效：ppa(user_id, person_id) 与 user(person_id) 不一致即拒。"""
        from models import PersonPrimaryAccountModel
        from services.identity import person as identity_person
        user = self.make_user()
        other = self.make_user(email='o@example.com')
        third = self.make_user(email='t@example.com')
        person, _ = identity_person.create_provisional(user)
        identity_person.create_provisional(other)
        db.session.commit()
        # user 归属 person，却把 primary 指成 (person, other)——组合不存在于 user 表
        db.session.add(PersonPrimaryAccountModel(
            person_id=person.id, user_id=other.id))
        with self.assertRaises(IntegrityError):
            db.session.flush()
        db.session.rollback()
        # 修复：third 自建 person（不建 primary），真实组合 (person3, third) 合法
        person3, _ = identity_person.create_provisional(third, ensure_primary=False)
        db.session.add(PersonPrimaryAccountModel(
            person_id=person3.id, user_id=third.id))
        db.session.flush()  # 不抛

    def test_merge_skeleton_not_implemented(self):
        from services.identity import person as identity_person
        with self.assertRaises(NotImplementedError):
            identity_person.merge_persons()


class RegistryTest(IdentityFoundationTestBase):
    """身份登记：唯一裁决、不覆盖、同人幂等、归一化。"""

    def _person(self, email):
        from services.identity import person as identity_person
        user = self.make_user(email=email)
        person, _ = identity_person.create_provisional(user)
        db.session.commit()
        return person

    def test_register_and_conflict_no_overwrite(self):
        from models import PersonIdentityModel
        from services.identity import registry
        p1 = self._person('a@example.com')
        p2 = self._person('b@example.com')
        row = registry.register_identity_key(
            p1, issuer='sysu', kind='netid', key='zhangsan01',
            assurance_method='school_email', proof_ref='challenge#1')
        db.session.commit()
        self.assertEqual(row.person_id, p1.id)
        self.assertEqual(row.proof_status, 'verified')
        # 冲突：不覆盖、不改已提交者，转可恢复冲突
        with self.assertRaises(registry.IdentityKeyConflict) as ctx:
            registry.register_identity_key(
                p2, issuer='sysu', kind='netid', key='zhangsan01',
                assurance_method='school_email')
        self.assertEqual(ctx.exception.existing_person_id, p1.id)
        db.session.commit()
        kept = PersonIdentityModel.query.filter_by(
            issuer='sysu', kind='netid', canonical_key='zhangsan01').one()
        self.assertEqual(kept.person_id, p1.id)  # 归属未被动过
        # 冲突后外层事务仍可用（可恢复语义）：p2 登记别的 key 正常
        row2 = registry.register_identity_key(
            p2, issuer='sysu', kind='netid', key='lisi02',
            assurance_method='school_email')
        db.session.commit()
        self.assertEqual(row2.person_id, p2.id)

    def test_same_person_same_key_idempotent(self):
        from models import PersonIdentityModel
        from services.identity import registry
        p1 = self._person('a@example.com')
        r1 = registry.register_identity_key(
            p1, issuer='sysu', kind='email', key='zs@sysu.edu.cn',
            assurance_method='school_email')
        r2 = registry.register_identity_key(
            p1, issuer='sysu', kind='email', key=' zs@SYSU.edu.cn ',  # trim+域名折叠后同 key
            assurance_method='school_email')
        db.session.commit()
        self.assertEqual(r1.id, r2.id)
        self.assertEqual(PersonIdentityModel.query.count(), 1)

    def test_local_part_case_not_folded(self):
        """本地部大小写不折叠（禁止猜测性归一化）：ZS 与 zs 是两个 key。"""
        from models import PersonIdentityModel
        from services.identity import registry
        p1 = self._person('a@example.com')
        registry.register_identity_key(
            p1, issuer='sysu', kind='email', key='ZS@sysu.edu.cn',
            assurance_method='school_email')
        registry.register_identity_key(
            p1, issuer='sysu', kind='email', key='zs@sysu.edu.cn',
            assurance_method='school_email')
        db.session.commit()
        self.assertEqual(PersonIdentityModel.query.count(), 2)

    def test_different_issuer_or_kind_not_conflict(self):
        from services.identity import registry
        p1 = self._person('a@example.com')
        p2 = self._person('b@example.com')
        registry.register_identity_key(
            p1, issuer='sysu', kind='netid', key='ref-001',
            assurance_method='school_email')
        registry.register_identity_key(
            p2, issuer='external:scnu', kind='roster_ref', key='ref-001',
            assurance_method='roster')
        db.session.commit()  # 不同 issuer/kind 各自成立

    def test_canonicalize_conservative(self):
        from services.identity import registry as r
        # email：仅域名小写，本地部原样（禁止删点号/加号猜测）
        self.assertEqual(r.canonicalize('email', 'ZS.Mail+tag@SYSU.edu.cn '),
                         'ZS.Mail+tag@sysu.edu.cn')
        # netid / roster_ref：原样保留（大小写敏感存储）
        self.assertEqual(r.canonicalize('netid', ' ZhangSan01 '), 'ZhangSan01')
        self.assertEqual(r.canonicalize('roster_ref', ' R-2026-001 '), 'R-2026-001')
        for bad in ('', '   ', None, 123):
            with self.assertRaises(ValueError):
                r.canonicalize('netid', bad)
        with self.assertRaises(ValueError):
            r.canonicalize('email', 'no-at-sign')
        with self.assertRaises(ValueError):
            r.canonicalize('passport', 'x')


class EventsLedgerTest(IdentityFoundationTestBase):
    """只追加账本：白名单脱敏 + 与业务写同事务回滚。"""

    def test_snapshot_allowlist_filters(self):
        from services.identity import events
        snap = events.sanitize_snapshot({
            'person_id': 7, 'verification_status': 'unverified',
            'password': 'secret-here', 'email': 'a@b.c',       # 白名单外：丢弃
            'nested': {'x': 1},                                  # 非标量：丢弃
            'lifecycle': 'active',
        })
        self.assertEqual(snap, {'person_id': 7,
                                'verification_status': 'unverified',
                                'lifecycle': 'active'})

    def test_record_event_stores_sanitized(self):
        from models import IdentityEventModel
        from services.identity import events
        events.record_event(
            'person.create_provisional',
            actor_user_id=1,
            target_ids={'user_id': 1, 'person_id': 2, 'raw_blob': {'x': 1}},
            before={'verification_status': 'unverified', 'token': 'eyJ...'},
            after={'verification_status': 'verified', 'otp': '123456'},
            evidence_refs={'challenge_id': 'ch-1', 'secret': 'xxx'},
            reason='x' * 300)
        db.session.commit()
        ev = IdentityEventModel.query.one()
        self.assertEqual(ev.target_ids, {'user_id': 1, 'person_id': 2})
        self.assertEqual(ev.before, {'verification_status': 'unverified'})
        self.assertEqual(ev.after, {'verification_status': 'verified'})
        self.assertEqual(ev.evidence_refs, {'challenge_id': 'ch-1'})
        self.assertEqual(len(ev.reason), 255)

    def test_ledger_rolls_back_with_business_write(self):
        """账本与业务写同事务：注入失败后，业务变更与事件一起回滚（S09）。"""
        from models import IdentityEventModel, PersonModel
        from services.identity import events, person as identity_person

        user = self.make_user()
        db.session.commit()  # 业务行先落库（真实场景：注册前半程已有提交）
        try:
            person, _ = identity_person.create_provisional(user)
            events.record_event('person.create_provisional',
                                actor_user_id=user.id,
                                target_ids={'user_id': user.id, 'person_id': person.id})
            db.session.flush()
            raise RuntimeError('注入失败：模拟业务写后半途异常')
        except RuntimeError:
            db.session.rollback()
        self.assertEqual(IdentityEventModel.query.count(), 0)   # 事件没了
        self.assertEqual(PersonModel.query.count(), 0)          # 人员没了
        survivor = self.UserModel.query.filter_by(email='u@example.com').one()
        self.assertIsNone(survivor.person_id)                   # 业务行回滚到变更前

    def test_run_idempotent_first_run_and_replay(self):
        from models import IdentityOperationModel
        from services.identity import events
        calls = []

        def fn(op):
            calls.append(op.operation_id)
            events.record_event('identity.key.register', operation_id=op.operation_id,
                                actor_user_id=1, target_ids={'person_id': 9})
            return 'person#9'

        op1, created1 = events.run_idempotent(
            1, 'identity.key.register', 'key-001', {'k': 'v'}, fn)
        db.session.commit()
        self.assertTrue(created1)
        self.assertEqual(op1.state, 'completed')
        self.assertEqual(op1.result_ref, 'person#9')
        self.assertEqual(len(calls), 1)

        # 同 key 同摘要：返回既有结果，fn 不再执行
        op2, created2 = events.run_idempotent(
            1, 'identity.key.register', 'key-001', {'k': 'v'}, fn)
        db.session.commit()
        self.assertFalse(created2)
        self.assertEqual(op2.operation_id, op1.operation_id)
        self.assertEqual(len(calls), 1)
        self.assertEqual(IdentityOperationModel.query.count(), 1)

    def test_run_idempotent_same_key_different_digest_conflicts(self):
        from services.identity import events
        from services.identity.errors import OperationConflict
        events.run_idempotent(1, 'identity.key.register', 'key-002',
                              {'k': 'v1'}, lambda op: 'r1')
        db.session.commit()
        with self.assertRaises(OperationConflict):
            events.run_idempotent(1, 'identity.key.register', 'key-002',
                                  {'k': 'v2'}, lambda op: 'r2')

    def test_run_idempotent_system_actor_zero(self):
        """actor=None（系统动作）归一为 0，唯一键对系统操作也生效。"""
        from services.identity import events
        from services.identity.errors import OperationConflict
        events.run_idempotent(None, 'person.backfill', 'batch-1',
                              {'b': 1}, lambda op: 'done')
        db.session.commit()
        with self.assertRaises(OperationConflict):
            events.run_idempotent(None, 'person.backfill', 'batch-1',
                                  {'b': 2}, lambda op: 'done2')

    def test_run_idempotent_actor_scoped_keys(self):
        """同 key 不同 actor 互不冲突（唯一键含 actor）。"""
        from services.identity import events
        events.run_idempotent(1, 'identity.key.register', 'shared-key',
                              {'k': 1}, lambda op: 'a')
        events.run_idempotent(2, 'identity.key.register', 'shared-key',
                              {'k': 1}, lambda op: 'b')
        db.session.commit()


class ConcurrentProvisionalTest(unittest.TestCase):
    """并发建 Person：同 email 并发注册只准一个成功（唯一约束裁决）。

    内存 SQLite 每连接独立建库，无法模拟并发——用文件库 + 两线程各自 app context。
    """

    def test_concurrent_same_email_single_person(self):
        with tempfile.TemporaryDirectory() as td:
            app = Flask(__name__)
            app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{td}/并发.db'
            app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {'connect_args': {'timeout': 15}}
            db.init_app(app)
            with app.app_context():
                db.create_all()
            results = []
            barrier = threading.Barrier(2)

            def worker(n):
                from services.identity import person as identity_person
                ctx = app.app_context()
                ctx.push()
                try:
                    barrier.wait(timeout=10)  # 对齐起跑线
                    from models import UserModel
                    user = UserModel(email='same@example.com', username=f'竞争者{n}')
                    user.set_password('x' * 32)
                    db.session.add(user)
                    identity_person.create_provisional(user)
                    db.session.commit()
                    results.append('ok')
                except IntegrityError:
                    db.session.rollback()
                    results.append('conflict')
                except Exception as exc:  # noqa: BLE001
                    db.session.rollback()
                    results.append(f'error:{exc}')
                finally:
                    ctx.pop()

            threads = [threading.Thread(target=worker, args=(n,)) for n in (1, 2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)
            self.assertEqual(sorted(results), ['conflict', 'ok'], results)
            with app.app_context():
                from models import PersonModel, PersonPrimaryAccountModel, UserModel
                self.assertEqual(UserModel.query.filter_by(email='same@example.com').count(), 1)
                self.assertEqual(PersonModel.query.count(), 1)
                self.assertEqual(PersonPrimaryAccountModel.query.count(), 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)

"""D3a 核验与审批的隔离回归；SQLite + 内存 Redis 桩（规格 5.1/8.1/16 章 P0 语义）。

覆盖：
  学校配置：域名精确匹配（拒包含式/共享域/未知域）、NetID-邮箱映射、
            审核人配置（≥2 就绪、全量替换校验、版本递增）
  申请状态机：draft→challenge→submit→approve/reject/withdraw；重复活跃申请；
            声明变更使邮箱证明作废；非本人操作 404（防枚举）
  挑战：     一次性消费、错 5 次锁死、过期作废、重发使旧码失效、payload 绑定
  审批：     非配置审核人 403（super_admin 不旁路）、key 冲突不覆盖不泄露归属、
            同事务 person verified + registry + 账本、幂等重放

用法（项目根）：.venv/bin/python scripts/test_identity_verification.py
"""
import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import sqlite3
from flask import Flask
from sqlalchemy import event
from sqlalchemy.dialects.mysql import LONGTEXT, MEDIUMTEXT
from sqlalchemy.ext.compiler import compiles


@compiles(MEDIUMTEXT, 'sqlite')
@compiles(LONGTEXT, 'sqlite')
def _mysql_text_on_sqlite(_element, _compiler, **_kwargs):
    return 'TEXT'


from exts import db  # noqa: E402
import models  # noqa: E402,F401  # 先于 create_all，否则 metadata 为空建不出表


def _enable_sqlite_fk(engine):
    @event.listens_for(engine, 'connect')
    def _set_fk_pragma(dbapi_conn, connection_record):
        if isinstance(dbapi_conn, sqlite3.Connection):
            cursor = dbapi_conn.cursor()
            cursor.execute('PRAGMA foreign_keys=ON')
            cursor.close()


def _enable_sqlite_savepoints(engine):
    @event.listens_for(engine, 'connect')
    def _do_connect(dbapi_conn, connection_record):
        if isinstance(dbapi_conn, sqlite3.Connection):
            dbapi_conn.isolation_level = None

    @event.listens_for(engine, 'begin')
    def _do_begin(conn):
        conn.exec_driver_sql('BEGIN')


class _FakeRedis:
    """verification 挑战限流的内存 Redis 桩（同 test_auth_foundation）。"""

    def __init__(self):
        self.store = {}

    def ping(self):
        return True

    def exists(self, key):
        return 1 if key in self.store else 0

    def ttl(self, key):
        return self.store[key][1] if key in self.store else -2

    def get(self, key):
        v = self.store.get(key)
        return v[0] if v else None

    def setex(self, key, ttl, value):
        self.store[key] = (value, ttl)

    def incr(self, key):
        v = int(self.store.get(key, ('0', 0))[0]) + 1
        self.store[key] = (str(v), self.store.get(key, ('0', 90000))[1])
        return v

    def expire(self, key, window):
        if key in self.store:
            self.store[key] = (self.store[key][0], window)

    def delete(self, key):
        self.store.pop(key, None)


class VerificationTestBase(unittest.TestCase):
    """共用脚手架：内存 SQLite（外键+savepoint）+ Redis 桩 + sysu 配置 + 人员。"""

    def setUp(self):
        self.app = Flask(__name__)
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        db.init_app(self.app)
        self.context = self.app.app_context()
        self.context.push()
        _enable_sqlite_fk(db.engine)
        _enable_sqlite_savepoints(db.engine)
        db.create_all()
        import services.identity.verification as verification
        self.fake = _FakeRedis()
        self._orig_redis = verification.redis_client
        verification.redis_client = self.fake
        from models import IdentitySchoolConfigModel, UserModel
        from services.identity import person as identity_person
        cfg = IdentitySchoolConfigModel(
            school_id='sysu', name='中山大学',
            personal_email_domains=['mail2.sysu.edu.cn'],
            excluded_email_domains=['sysu.edu.cn', 'mail.sysu.edu.cn'],
            email_local_matches_identifier=True,
            reviewer_user_ids=[])
        db.session.add(cfg)
        db.session.commit()
        self.cfg = cfg
        self.UserModel = UserModel
        self.identity_person = identity_person
        self.verification = verification

    def tearDown(self):
        self.verification.redis_client = self._orig_redis
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def make_user(self, email, username='用户', role='user'):
        user = self.UserModel(username=username, email=email, role=role)
        user.set_password('x' * 32)
        db.session.add(user)
        db.session.flush()
        self.identity_person.create_provisional(user)
        db.session.commit()
        return user

    def make_app(self, user, identifier='netid01', email=None, name='张三'):
        app = self.verification.create_or_update_application(
            user, school_id='sysu', claimed_name=name,
            claimed_identifier=identifier,
            contact_email=email or f'{identifier}@mail2.sysu.edu.cn')
        db.session.commit()
        return app


class SchoolConfigTest(VerificationTestBase):
    """域名精确匹配与映射规则（规格 5.1.2/5.1.3）。"""

    def test_domain_exact_match(self):
        from services.auth_context import AuthRejected
        from services.identity import school
        school_ex = AuthRejected
        # 共享/公务域：明确排除
        with self.assertRaises(school_ex) as c:
            school.validate_contact_email(self.cfg, 'x@sysu.edu.cn', 'x')
        self.assertEqual(c.exception.machine, 'EMAIL_DOMAIN_EXCLUDED')
        # 未知域拒绝；包含式匹配不算（evilmail2.sysu.edu.cn 不是 mail2.*）
        for bad in ('x@gmail.com', 'x@evil-mail2.sysu.edu.cn',
                    'x@mail2.sysu.edu.cn.evil.com'):
            with self.assertRaises(school_ex) as c:
                school.validate_contact_email(self.cfg, bad, 'x')
            self.assertEqual(c.exception.machine, 'EMAIL_DOMAIN_NOT_ALLOWED')
        # 个人域 + NetID 本地部一致：通过，域名归一化
        got = school.validate_contact_email(
            self.cfg, 'netid01@MAIL2.SYSU.EDU.CN ', 'netid01')
        self.assertEqual(got, 'netid01@mail2.sysu.edu.cn')

    def test_identifier_email_mapping(self):
        from services.auth_context import AuthRejected
        from services.identity import school
        with self.assertRaises(AuthRejected) as c:
            school.validate_contact_email(self.cfg, 'other@mail2.sysu.edu.cn',
                                          'netid01')
        self.assertEqual(c.exception.machine, 'IDENTIFIER_EMAIL_MISMATCH')

    def test_reviewer_config(self):
        from services.identity import school
        u1 = self.make_user('r1@x.dev', role='super_admin')
        u2 = self.make_user('r2@x.dev', role='super_admin')
        self.assertFalse(self.cfg.reviewers_ready)  # 0 名
        school.update_school_config(self.cfg, reviewer_user_ids=[u1.id])
        db.session.commit()
        self.assertFalse(self.cfg.reviewers_ready)  # 1 名：未就绪但不拦功能
        self.assertTrue(self.cfg.is_reviewer(u1.id))
        school.update_school_config(self.cfg, reviewer_user_ids=[u1.id, u2.id])
        db.session.commit()
        self.assertTrue(self.cfg.reviewers_ready)   # ≥2 就绪
        self.assertEqual(self.cfg.config_version, 3)
        self.assertFalse(self.cfg.is_reviewer(999)) # 不在名单即无权

    def test_reviewer_config_rejects_bad_ids(self):
        from services.identity import school
        from services.identity.errors import IdentityError
        with self.assertRaises(IdentityError):
            school.update_school_config(self.cfg, reviewer_user_ids=[99999])
        with self.assertRaises(IdentityError):
            school.update_school_config(self.cfg, reviewer_user_ids=[])
        # 运营规则（2026-10-02）：非管理员账号不能被添加为审核人
        plain = self.make_user('plain@x.dev')
        with self.assertRaises(IdentityError):
            school.update_school_config(self.cfg, reviewer_user_ids=[plain.id])

    def test_reviewer_gate_requires_admin(self):
        """名单内但非管理员（如被降级）：解析过滤、审批门槛拒绝——降级即失效。"""
        from services.identity import school
        from services.auth_context import AuthRejected
        plain = self.make_user('plain@x.dev')
        self.cfg.reviewer_user_ids = [plain.id]  # 绕过添加校验模拟存量脏数据
        self.assertEqual(school.resolve_reviewers(self.cfg), [])
        with self.assertRaises(AuthRejected) as c:
            school.require_reviewer(self.cfg, plain)
        self.assertEqual(c.exception.machine, 'NOT_REVIEWER')


class ApplicationFlowTest(VerificationTestBase):
    """申请状态机与挑战语义（规格 5.1/8.1）。"""

    def test_draft_and_active_conflict(self):
        from services.auth_context import AuthRejected
        user = self.make_user('a@x.dev')
        app = self.make_app(user)
        self.assertEqual(app.status, 'draft')
        app2 = self.verification.create_or_update_application(
            user, school_id='sysu', claimed_name='张三',
            claimed_identifier='netid01',
            contact_email='netid01@mail2.sysu.edu.cn')
        db.session.commit()
        self.assertEqual(app2.id, app.id)  # 同一活跃申请，更新而非新建
        # 已提交后不能再改
        app.challenge_verified_at = datetime.now()
        self.verification.submit_application(user, app)
        db.session.commit()
        with self.assertRaises(AuthRejected) as c:
            self.verification.create_or_update_application(
                user, school_id='sysu', claimed_name='张三',
                claimed_identifier='netid01',
                contact_email='netid01@mail2.sysu.edu.cn')
        self.assertEqual(c.exception.machine, 'APPLICATION_ACTIVE')

    def test_submit_requires_challenge(self):
        from services.auth_context import AuthRejected
        user = self.make_user('a@x.dev')
        app = self.make_app(user)
        with self.assertRaises(AuthRejected) as c:
            self.verification.submit_application(user, app)
        self.assertEqual(c.exception.machine, 'CHALLENGE_REQUIRED')

    def test_claim_change_invalidates_proof(self):
        user = self.make_user('a@x.dev')
        app = self.make_app(user, identifier='netid01')
        code, _ch = self.verification.issue_challenge(user, app)
        db.session.commit()
        self.assertTrue(self.verification.verify_challenge(user, app, code))
        db.session.commit()
        self.assertIsNotNone(app.challenge_verified_at)
        # 换声明：旧证明作废
        self.verification.create_or_update_application(
            user, school_id='sysu', claimed_name='张三',
            claimed_identifier='netid02',
            contact_email='netid02@mail2.sysu.edu.cn')
        db.session.commit()
        self.assertIsNone(app.challenge_verified_at)
        # 旧验证码即使重放也无效（payload 已换）
        self.assertFalse(self.verification.verify_challenge(user, app, code))
        db.session.commit()

    def test_challenge_one_time_and_attempts(self):
        user = self.make_user('a@x.dev')
        app = self.make_app(user)
        code, ch = self.verification.issue_challenge(user, app)
        db.session.commit()
        for i in range(4):  # 错 4 次仍可再试
            self.assertFalse(self.verification.verify_challenge(user, app, '000000'))
            db.session.commit()
        self.assertTrue(self.verification.verify_challenge(user, app, code))
        db.session.commit()
        self.assertIsNotNone(ch.consumed_at)
        # 已消费：同码再用无效
        self.assertFalse(self.verification.verify_challenge(user, app, code))
        db.session.commit()

    def test_challenge_lockout_after_five_wrong(self):
        user = self.make_user('a@x.dev')
        app = self.make_app(user)
        code, ch = self.verification.issue_challenge(user, app)
        db.session.commit()
        for _ in range(5):
            self.assertFalse(self.verification.verify_challenge(user, app, '000000'))
            db.session.commit()
        self.assertIsNotNone(ch.consumed_at)  # 锁死
        self.assertFalse(self.verification.verify_challenge(user, app, code))
        db.session.commit()

    def test_resend_invalidates_old_code(self):
        user = self.make_user('a@x.dev')
        app = self.make_app(user)
        code1, ch1 = self.verification.issue_challenge(user, app)
        db.session.commit()
        self.fake.delete(f'idch:cd:{app.contact_email}')  # 跳过冷却
        code2, ch2 = self.verification.issue_challenge(user, app)
        db.session.commit()
        self.assertNotEqual(code1, code2)
        self.assertFalse(self.verification.verify_challenge(user, app, code1))  # 旧码失效
        db.session.commit()
        self.assertTrue(self.verification.verify_challenge(user, app, code2))
        db.session.commit()

    def test_expired_challenge_rejected(self):
        user = self.make_user('a@x.dev')
        app = self.make_app(user)
        code, ch = self.verification.issue_challenge(user, app)
        db.session.commit()
        ch.expires_at = datetime.now() - timedelta(seconds=1)
        db.session.commit()
        self.assertFalse(self.verification.verify_challenge(user, app, code))
        db.session.commit()

    def test_owner_check_no_enumeration(self):
        from services.auth_context import AuthRejected
        u1 = self.make_user('a@x.dev')
        u2 = self.make_user('b@x.dev')
        app = self.make_app(u1)
        with self.assertRaises(AuthRejected) as c:
            self.verification.issue_challenge(u2, app)
        self.assertEqual(c.exception.status, 404)
        self.assertEqual(c.exception.machine, 'NOT_FOUND')

    def test_withdraw_and_reapply(self):
        from services.auth_context import AuthRejected
        user = self.make_user('a@x.dev')
        app = self.make_app(user)
        self.verification.withdraw_application(user, app)
        db.session.commit()
        self.assertEqual(app.status, 'withdrawn')
        with self.assertRaises(AuthRejected):
            self.verification.withdraw_application(user, app)  # 终态不可再撤
        db.session.commit()
        # 终态后可重新建申请（新行）
        app2 = self.verification.create_or_update_application(
            user, school_id='sysu', claimed_name='张三',
            claimed_identifier='netid01',
            contact_email='netid01@mail2.sysu.edu.cn')
        db.session.commit()
        self.assertNotEqual(app2.id, app.id)


class ApprovalFlowTest(VerificationTestBase):
    """审批：负责人门槛、key 冲突不覆盖、同事务登记（规格 5.1.4/5.1.5/8.2.4）。"""

    def setUp(self):
        super().setUp()
        from services.identity import school
        self.reviewer = self.make_user('rev@x.dev', role='super_admin')
        self.outsider_admin = self.make_user('boss@x.dev', role='super_admin')
        school.update_school_config(
            self.cfg, reviewer_user_ids=[self.reviewer.id])
        db.session.commit()

    def _submitted_app(self, user, identifier='netid01'):
        app = self.make_app(user, identifier=identifier)
        self.fake.delete(f'idch:cd:{app.contact_email}')  # 同目标多人共用邮箱：清冷却
        code, _ = self.verification.issue_challenge(user, app)
        db.session.commit()
        self.assertTrue(self.verification.verify_challenge(user, app, code))
        self.verification.submit_application(user, app)
        db.session.commit()
        return app

    def test_non_reviewer_admin_forbidden(self):
        from services.auth_context import AuthRejected
        user = self.make_user('a@x.dev')
        app = self._submitted_app(user)
        # super_admin 但不在名单：不旁路
        with self.assertRaises(AuthRejected) as c:
            self.verification.approve_application(app, self.outsider_admin)
        self.assertEqual(c.exception.machine, 'NOT_REVIEWER')
        # 名单内审核人：通过
        self.verification.approve_application(app, self.reviewer)
        db.session.commit()

    def test_approve_registers_key_and_person_verified(self):
        from models import IdentityEventModel, PersonIdentityModel, PersonModel
        user = self.make_user('a@x.dev')
        app = self._submitted_app(user)
        person = db.session.get(PersonModel, user.person_id)
        self.verification.approve_application(app, self.reviewer, note='名册核对通过')
        db.session.commit()
        self.assertEqual(app.status, 'approved')
        self.assertEqual(app.reviewed_by, self.reviewer.id)
        self.assertEqual(person.verification_status, 'verified')
        self.assertEqual(person.verified_name, '张三')
        row = PersonIdentityModel.query.filter_by(
            issuer='sysu', kind='netid', canonical_key='netid01').one()
        self.assertEqual(row.person_id, person.id)
        self.assertEqual(row.proof_status, 'verified')
        self.assertEqual(row.assurance_method, 'school_email')
        actions = {e.action for e in IdentityEventModel.query.all()}
        self.assertIn('identity.application.approve', actions)
        self.assertIn('identity.challenge.verify', actions)
        # 幂等重放：同审核人再批不炸不重复
        self.verification.approve_application(app, self.reviewer)
        db.session.commit()
        self.assertEqual(PersonIdentityModel.query.count(), 1)

    def test_key_conflict_no_overwrite_no_leak(self):
        from services.auth_context import AuthRejected
        from models import PersonIdentityModel
        u1 = self.make_user('a@x.dev')
        u2 = self.make_user('b@x.dev')
        app1 = self._submitted_app(u1, identifier='samenetid')
        app2 = self._submitted_app(u2, identifier='samenetid')
        self.verification.approve_application(app1, self.reviewer)
        db.session.commit()
        with self.assertRaises(AuthRejected) as c:
            self.verification.approve_application(app2, self.reviewer)
        self.assertEqual(c.exception.machine, 'IDENTITY_KEY_CONFLICT')
        # 不泄露归属人 id；不覆盖已提交者
        self.assertNotIn(str(u1.person_id), str(c.exception))
        row = PersonIdentityModel.query.filter_by(
            issuer='sysu', kind='netid', canonical_key='samenetid').one()
        self.assertEqual(row.person_id, u1.person_id)
        self.assertEqual(app2.status, 'submitted')  # 申请留待人工

    def test_reject_keeps_reason_and_allows_new_version(self):
        from models import PersonModel
        user = self.make_user('a@x.dev')
        app = self._submitted_app(user)
        self.verification.reject_application(app, self.reviewer, reason='名册无此姓名')
        db.session.commit()
        self.assertEqual(app.status, 'rejected')
        self.assertEqual(app.reject_reason, '名册无此姓名')
        person = db.session.get(PersonModel, user.person_id)
        self.assertEqual(person.verification_status, 'unverified')
        # 补交新版本 = 重新建申请
        app2 = self.make_app(user, identifier='netid01')
        self.assertEqual(app2.status, 'draft')

    def test_approve_requires_challenge_even_if_submitted(self):
        from services.auth_context import AuthRejected
        user = self.make_user('a@x.dev')
        app = self.make_app(user)
        app.status = 'submitted'  # 绕过状态机构造非法态
        db.session.commit()
        with self.assertRaises(AuthRejected) as c:
            self.verification.approve_application(app, self.reviewer)
        self.assertEqual(c.exception.machine, 'CHALLENGE_REQUIRED')


if __name__ == '__main__':
    unittest.main(verbosity=2)

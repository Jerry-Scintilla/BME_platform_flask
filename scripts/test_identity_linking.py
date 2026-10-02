"""D3b 认领与空壳归并的隔离回归；SQLite + 内存 Redis 桩（规格 7/8 章 P0 语义）。

覆盖：
  案例创建：actor 固定、重复案例 409、占位锁互斥
  双端证明：A 绑 sid+近期认证；B 独立凭据（错误统一 422 不泄露存在性、
            5 次锁案例、MFA 启用必须过 TOTP、B 特权转人工）
  空壳扫描：全空=空壳；任一模块有数据=blocker；B 封禁/特权=blocker
  预览：    digest 绑定计划（B 数据变化→digest 变）；授权≤双证明窗口；收据可验
  确认归并：一个事务内——B 侧 primary 先删、身份登记迁移、B.person_id 归存续、
            B lifecycle=merged、B_p.merged 指向、核验态继承、双端 bump 撤会话
            （A 当前 sid 保留）、证明/授权消费、占位释放、幂等重放
  人工审：  当事人不可自审；有 blocker 需两名批准；驳回取消案例
  撤回：    终态前可撤、锁释放；applied 撤回被拒（逆操作=新审批任务）

用法（项目根）：.venv/bin/python scripts/test_identity_linking.py
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
import models  # noqa: E402,F401


def _enable_sqlite_fk(engine):
    @event.listens_for(engine, 'connect')
    def _p(dbapi_conn, _rec):
        if isinstance(dbapi_conn, sqlite3.Connection):
            cur = dbapi_conn.cursor()
            cur.execute('PRAGMA foreign_keys=ON')
            cur.close()


def _enable_sqlite_savepoints(engine):
    @event.listens_for(engine, 'connect')
    def _c(dbapi_conn, _rec):
        if isinstance(dbapi_conn, sqlite3.Connection):
            dbapi_conn.isolation_level = None

    @event.listens_for(engine, 'begin')
    def _b(conn):
        conn.exec_driver_sql('BEGIN')


class _FakeRedis:
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

    def delete(self, key):
        self.store.pop(key, None)


class LinkingTestBase(unittest.TestCase):
    """脚手架：SQLite（外键+savepoint）+ Redis 桩 + A/B 双账号人员 + A 会话。"""

    def setUp(self):
        self.app = Flask(__name__)
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.app.config['JWT_SECRET_KEY'] = 'test-secret-key-with-at-least-32-characters'
        db.init_app(self.app)
        self.context = self.app.app_context()
        self.context.push()
        _enable_sqlite_fk(db.engine)
        _enable_sqlite_savepoints(db.engine)
        db.create_all()
        import services.identity.linking as linking
        self.fake = _FakeRedis()
        self._orig_redis = linking.redis_client
        linking.redis_client = self.fake
        from flask_jwt_extended import JWTManager
        JWTManager(self.app)
        from models import UserModel
        from services.identity import person as identity_person
        from services.identity.registry import register_identity_key
        self.UserModel = UserModel
        self.identity_person = identity_person
        self.register_identity_key = register_identity_key
        self.linking = linking

    def tearDown(self):
        self.linking.redis_client = self._orig_redis
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def make_user(self, email, **kw):
        user = self.UserModel(username=kw.pop('username', '用户'), email=email, **kw)
        user.set_password('x' * 32)
        db.session.add(user)
        db.session.flush()
        self.identity_person.create_provisional(user)
        db.session.commit()
        return user

    def make_actor(self, user, amr='pwd', auth_age_seconds=0):
        """构造带近期认证 v2 会话的 actor（ActorContext 形状）。"""
        from services import auth_sessions
        from services.auth_context import ActorContext
        with self.app.test_request_context('/x'):
            access, refresh, session = auth_sessions.issue_session(
                user, client_type='user', amr=amr)
        if auth_age_seconds:
            session.auth_time = datetime.now() - timedelta(seconds=auth_age_seconds)
        db.session.commit()
        actor = ActorContext(user=user, session=session,
                             claims={'token_schema': 'v2'}, is_legacy=False)
        return actor, session

    def verified_person(self, user, identifier='net01'):
        person = db.session.get(models.PersonModel, user.person_id)
        self.register_identity_key(
            person, issuer='sysu', kind='netid', key=identifier,
            assurance_method='school_email')
        person.verification_status = 'verified'
        person.verified_name = '核验过的人'
        db.session.commit()
        return person


class CaseCreationTest(LinkingTestBase):

    def test_create_and_duplicate(self):
        from services.auth_context import AuthRejected
        a = self.make_user('a@x.dev')
        actor, _ = self.make_actor(a)
        case = self.linking.create_case(actor)
        db.session.commit()
        self.assertEqual(case.state, 'collecting')
        self.assertEqual(case.account_a, a.id)
        with self.assertRaises(AuthRejected) as c:
            self.linking.create_case(actor)
        self.assertEqual(c.exception.machine, 'CASE_EXISTS')

    def test_admin_cannot_initiate(self):
        from services.auth_context import AuthRejected
        boss = self.make_user('boss@x.dev', role='super_admin')
        actor, _ = self.make_actor(boss)
        with self.assertRaises(AuthRejected):
            self.linking.create_case(actor)

    def test_object_permission_no_sharing(self):
        """对象级权限：他人（含同 Person 的其他账号）不可访问案例。"""
        from services.auth_context import AuthRejected
        a = self.make_user('a@x.dev')
        actor, _ = self.make_actor(a)
        case = self.linking.create_case(actor)
        db.session.commit()
        other = self.make_user('o@x.dev')
        other_actor, _ = self.make_actor(other)
        with self.assertRaises(AuthRejected) as c:
            self.linking.get_case_for(other_actor, case.id)
        self.assertEqual(c.exception.machine, 'NOT_FOUND')


class ProofTest(LinkingTestBase):

    def setUp(self):
        super().setUp()
        self.a = self.make_user('a@x.dev')
        self.actor, self.session = self.make_actor(self.a)
        self.case = self.linking.create_case(self.actor)
        db.session.commit()
        self.b = self.make_user('b@x.dev')

    def test_prove_initiator_binds_sid(self):
        att = self.linking.prove_initiator(self.actor, self.case)
        db.session.commit()
        self.assertEqual(att.purpose, 'link_side_a')
        self.assertEqual(att.actor_sid, self.session.sid)
        self.assertEqual(att.security_version_snapshot, self.a.security_version)

    def test_prove_initiator_requires_recent_auth(self):
        from services.auth_context import AuthRejected
        self.session.auth_time = datetime.now() - timedelta(seconds=9999)
        db.session.commit()
        with self.assertRaises(AuthRejected) as c:
            self.linking.prove_initiator(self.actor, self.case)
        self.assertEqual(c.exception.machine, 'REAUTH_REQUIRED')

    def _prove_both(self, totp=None):
        self.linking.prove_initiator(self.actor, self.case)
        self.linking.prove_target(self.actor, self.case,
                                  target_email='b@x.dev', password='x' * 32, totp=totp)
        db.session.commit()

    def test_prove_target_success_moves_to_proof_ready(self):
        self._prove_both()
        self.assertEqual(self.case.state, 'proof_ready')
        self.assertEqual(self.case.account_b, self.b.id)
        from models import IdentityCaseAccountLockModel
        self.assertIsNotNone(db.session.get(IdentityCaseAccountLockModel, self.b.id))

    def test_wrong_password_unified_422_no_leak(self):
        from services.auth_context import AuthRejected
        with self.assertRaises(AuthRejected) as c:
            self.linking.prove_target(self.actor, self.case,
                                      target_email='ghost@x.dev', password='wrong')
        self.assertEqual((c.exception.status, c.exception.machine),
                         (422, 'TARGET_PROOF_FAILED'))
        # 存在的 B 密码错也同话术（防枚举）
        with self.assertRaises(AuthRejected) as c:
            self.linking.prove_target(self.actor, self.case,
                                      target_email='b@x.dev', password='wrong')
        self.assertEqual((c.exception.status, c.exception.machine),
                         (422, 'TARGET_PROOF_FAILED'))

    def test_target_proof_lockout(self):
        from services.auth_context import AuthRejected
        for _ in range(5):  # 5 次错误耗尽额度
            with self.assertRaises(AuthRejected) as c:
                self.linking.prove_target(self.actor, self.case,
                                          target_email='b@x.dev', password='wrong')
            self.assertEqual(c.exception.machine, 'TARGET_PROOF_FAILED')
        # 第 6 次直接锁案例（PROOF_LIMIT）
        with self.assertRaises(AuthRejected) as c:
            self.linking.prove_target(self.actor, self.case,
                                      target_email='b@x.dev', password='x' * 32)
        self.assertEqual(c.exception.machine, 'PROOF_LIMIT')
        self.assertEqual(self.case.state, 'failed')

    def test_target_with_totp_required(self):
        import pyotp
        from services.auth_mfa import start_totp_enrollment
        _uri, secret, factor = start_totp_enrollment(self.b)
        factor.state = 'active'
        factor.confirmed_at = datetime.now()
        db.session.commit()
        from services.auth_context import AuthRejected
        with self.assertRaises(AuthRejected) as c:  # 无 TOTP 码 → 422
            self.linking.prove_target(self.actor, self.case,
                                      target_email='b@x.dev', password='x' * 32)
        self.assertEqual(c.exception.machine, 'TARGET_PROOF_FAILED')
        ok_code = pyotp.TOTP(secret).now()
        self.linking.prove_initiator(self.actor, self.case)
        att = self.linking.prove_target(self.actor, self.case,
                                        target_email='b@x.dev', password='x' * 32,
                                        totp=ok_code)
        db.session.commit()
        self.assertEqual(att.amr, 'pwd+totp')

    def test_privileged_target_goes_manual(self):
        from services.auth_context import AuthRejected
        self.b.role = 'super_admin'
        db.session.commit()
        self.linking.prove_initiator(self.actor, self.case)
        with self.assertRaises(AuthRejected) as c:
            self.linking.prove_target(self.actor, self.case,
                                      target_email='b@x.dev', password='x' * 32)
        self.assertEqual(c.exception.machine, 'MANUAL_REQUIRED')
        self.assertEqual(self.case.state, 'awaiting_review')


class ShellScanTest(LinkingTestBase):

    def test_clean_user_is_shell(self):
        b = self.make_user('b@x.dev')
        is_shell, blockers = self.linking.shell_scan(b)
        self.assertTrue(is_shell, blockers)
        self.assertEqual(blockers, [])

    def test_any_business_data_blocks(self):
        from models import ArticleV2Model
        b = self.make_user('b@x.dev')
        rel = ArticleV2Model(author_id=b.id, title='B 的内容')
        db.session.add(rel)
        db.session.commit()
        is_shell, blockers = self.linking.shell_scan(b)
        self.assertFalse(is_shell)
        self.assertIn('target_has_business_data', blockers)
        db.session.delete(rel)
        db.session.commit()
        is_shell, blockers = self.linking.shell_scan(b)
        self.assertTrue(is_shell)

    def test_banned_blocks(self):
        b = self.make_user('b@x.dev')
        b.status = 'banned'
        db.session.commit()
        is_shell, blockers = self.linking.shell_scan(b)
        self.assertFalse(is_shell)
        self.assertIn('target_banned', blockers)


class PreviewConfirmTest(LinkingTestBase):
    """预览绑定 + 空壳归并全事务正确性（7.1.8-10/7.2）。"""

    def setUp(self):
        super().setUp()
        self.a = self.make_user('a@x.dev')               # QQ 老号（未核验）
        self.actor, self.session = self.make_actor(self.a)
        self.b = self.make_user('b@x.dev')               # 空壳校园号（已核验）
        self.verified_person(self.b, 'qq01')
        self.case = self.linking.create_case(self.actor)
        self.linking.prove_initiator(self.actor, self.case)
        self.linking.prove_target(self.actor, self.case,
                                  target_email='b@x.dev', password='x' * 32)
        db.session.commit()

    def _preview(self):
        out = self.linking.build_preview(self.actor, self.case)
        db.session.commit()
        return out

    def test_preview_shell_goes_preview_ready(self):
        out = self._preview()
        self.assertEqual(self.case.state, 'preview_ready')
        self.assertTrue(out['plan']['shell'])
        self.assertEqual(out['plan']['moving_keys'], ['sysu|netid|qq01'])
        self.assertTrue(out['receipt'])

    def test_preview_digest_binds_data(self):
        out1 = self._preview()
        from models import ArticleV2Model
        db.session.add(ArticleV2Model(author_id=self.b.id, title='新内容'))
        db.session.commit()
        self.case.state = 'proof_ready'  # 允许重预览
        self.case.version += 1
        out2 = self._preview()
        self.assertNotEqual(out1['digest'], out2['digest'])
        self.assertFalse(out2['plan']['shell'])

    def test_receipt_roundtrip(self):
        out = self._preview()
        res = self.linking.check_receipt(out['receipt'])
        self.assertEqual(res['state'], 'pending')
        self.linking.confirm_link(self.actor, self.case,
                                  preview_digest=out['digest'])
        db.session.commit()
        res = self.linking.check_receipt(out['receipt'])
        self.assertEqual(res['state'], 'applied')

    def test_confirm_merges_everything_atomically(self):
        from models import (IdentityAttestationModel,
                            IdentityTransactionAuthorizationModel, PersonModel,
                            PersonPrimaryAccountModel)
        out = self._preview()
        # A 另开的会话（应被撤）；B 的会话（应被撤）
        from services import auth_sessions
        with self.app.test_request_context('/x'):
            _ta, _tr, a_other = auth_sessions.issue_session(self.a, client_type='user', amr='pwd')
            _tb, _tr, b_sess = auth_sessions.issue_session(self.b, client_type='user', amr='pwd')
        db.session.commit()
        b_person_id = self.b.person_id  # 归并后 self.b.person_id 会指向存续人员
        a_person_id = self.a.person_id

        result = self.linking.confirm_link(self.actor, self.case,
                                           preview_digest=out['digest'])
        db.session.commit()
        self.assertEqual(result['state'], 'applied')

        person_a = db.session.get(PersonModel, a_person_id)
        person_b = db.session.get(PersonModel, b_person_id)
        # 人员与账号归属
        self.assertEqual(self.b.person_id, person_a.id)          # B 归存续人员
        self.assertEqual(self.b.lifecycle, 'merged')
        self.assertEqual(person_b.record_status, 'merged')
        self.assertEqual(person_b.merged_to_person_id, person_a.id)
        self.assertIsNone(db.session.get(PersonPrimaryAccountModel, person_b.id))
        self.assertIsNotNone(db.session.get(PersonPrimaryAccountModel, person_a.id))
        # 学校身份归属 A 的 Person；核验态继承
        from models import PersonIdentityModel
        moved = PersonIdentityModel.query.filter_by(person_id=person_a.id).all()
        self.assertEqual(len(moved), 1)
        self.assertEqual(person_a.verification_status, 'verified')
        self.assertEqual(person_a.verified_name, '核验过的人')
        # 双端 bump：A 旧会话撤/当前保留；B 全撤
        db.session.refresh(a_other)
        db.session.refresh(b_sess)
        db.session.refresh(self.session)
        self.assertIsNotNone(a_other.revoked_at)
        self.assertIsNotNone(b_sess.revoked_at)
        self.assertIsNone(self.session.revoked_at)
        self.assertEqual(self.b.security_version, 1)
        self.assertEqual(self.a.security_version, 1)
        self.assertTrue(self.b.require_versioned_tokens)
        # 证明/授权消费；案例终态；锁释放
        for att in IdentityAttestationModel.query.filter_by(case_id=self.case.id).all():
            self.assertIsNotNone(att.consumed_at)
        authz = IdentityTransactionAuthorizationModel.query.filter_by(
            case_id=self.case.id).one()
        self.assertIsNotNone(authz.consumed_at)
        from models import IdentityCaseAccountLockModel
        self.assertIsNone(db.session.get(IdentityCaseAccountLockModel, self.a.id))
        self.assertIsNone(db.session.get(IdentityCaseAccountLockModel, self.b.id))
        # outbox 双端通知
        from models import IdentityOutboxModel
        self.assertEqual(IdentityOutboxModel.query.count(), 2)

    def test_confirm_idempotent_replay(self):
        out = self._preview()
        r1 = self.linking.confirm_link(self.actor, self.case,
                                       preview_digest=out['digest'])
        db.session.commit()
        r2 = self.linking.confirm_link(self.actor, self.case,
                                       preview_digest=out['digest'])
        db.session.commit()
        self.assertFalse(r1['replay'])
        self.assertTrue(r2['replay'])

    def test_confirm_stale_digest_409(self):
        from services.auth_context import AuthRejected
        out = self._preview()
        with self.assertRaises(AuthRejected) as c:
            self.linking.confirm_link(self.actor, self.case, preview_digest='deadbeef')
        self.assertEqual((c.exception.status, c.exception.machine),
                         (409, 'STALE_PREVIEW'))

    def test_confirm_after_b_changes_data_409(self):
        from services.auth_context import AuthRejected
        from models import ArticleV2Model
        out = self._preview()
        db.session.add(ArticleV2Model(author_id=self.b.id, title='预览后新增'))
        db.session.commit()
        with self.assertRaises(AuthRejected) as c:
            self.linking.confirm_link(self.actor, self.case,
                                      preview_digest=out['digest'])
        self.assertEqual(c.exception.machine, 'STALE_PREVIEW')

    def test_confirm_expired_attestation_requires_reproof(self):
        from services.auth_context import AuthRejected
        from models import IdentityAttestationModel
        out = self._preview()
        for att in IdentityAttestationModel.query.filter_by(case_id=self.case.id).all():
            att.expires_at = datetime.now() - timedelta(seconds=1)
        db.session.commit()
        with self.assertRaises(AuthRejected) as c:
            self.linking.confirm_link(self.actor, self.case,
                                      preview_digest=out['digest'])
        self.assertEqual(c.exception.machine, 'REAUTH_REQUIRED')

    def test_version_drift_invalidates_attestation(self):
        """B 证明后重置密码（版本+1）→ 旧证明作废（规格 3.2 attestation）。"""
        from services.auth_context import AuthRejected
        from models import IdentityAttestationModel
        att = IdentityAttestationModel.query.filter_by(
            case_id=self.case.id, purpose='link_side_b').one()
        self.b.security_version += 1
        db.session.commit()
        self.linking._transition(self.case, 'collecting', self.a.id, '重测')
        db.session.commit()
        self.assertIsNone(self.linking._live_attestation(self.case, 'link_side_b'))


class ReviewPathTest(LinkingTestBase):
    """有阻断项 → 双人审批 → 用户确认（7.4）。"""

    def setUp(self):
        super().setUp()
        self.a = self.make_user('a@x.dev')
        self.actor, _ = self.make_actor(self.a)
        self.b = self.make_user('b@x.dev')
        from models import ArticleV2Model
        db.session.add(ArticleV2Model(author_id=self.b.id, title='B 有内容'))
        db.session.commit()
        self.case = self.linking.create_case(self.actor)
        self.linking.prove_initiator(self.actor, self.case)
        self.linking.prove_target(self.actor, self.case,
                                  target_email='b@x.dev', password='x' * 32)
        db.session.commit()
        self.out = self.linking.build_preview(self.actor, self.case)
        db.session.commit()
        self.r1 = self.make_user('rev1@x.dev', role='user')
        self.r2 = self.make_user('rev2@x.dev', role='user')

    def test_blocked_case_needs_two_approvals(self):
        from services.auth_context import AuthRejected
        self.assertEqual(self.case.state, 'awaiting_review')
        self.linking.review_case(self.case, self.r1, decision='approved')
        db.session.commit()
        self.assertEqual(self.case.state, 'awaiting_review')  # 一名不够
        self.linking.review_case(self.case, self.r2, decision='approved')
        db.session.commit()
        self.assertEqual(self.case.state, 'approved_waiting_confirmation')

    def test_same_reviewer_second_vote_not_double_counted(self):
        self.linking.review_case(self.case, self.r1, decision='approved')
        db.session.commit()
        self.linking.review_case(self.case, self.r1, decision='approved')
        db.session.commit()
        self.assertEqual(self.case.state, 'awaiting_review')

    def test_party_cannot_review(self):
        from services.auth_context import AuthRejected
        with self.assertRaises(AuthRejected):
            self.linking.review_case(self.case, self.a, decision='approved')
        with self.assertRaises(AuthRejected):
            self.linking.review_case(self.case, self.b, decision='approved')

    def test_reject_cancels_and_releases(self):
        from models import IdentityCaseAccountLockModel
        self.linking.review_case(self.case, self.r1, decision='rejected', reason='名册不符')
        db.session.commit()
        self.assertEqual(self.case.state, 'cancelled')
        self.assertIsNone(db.session.get(IdentityCaseAccountLockModel, self.a.id))

    def test_nonshell_confirm_keeps_active_with_continuation_then_freeze(self):
        """7.3：有业务数据的 B 归并后保持 active（续办宽限 14 天），冻结动作才 merged。"""
        from models import IdentityExceptionGrantModel
        frz1 = self.make_user('frz1@x.dev')
        frz2 = self.make_user('frz2@x.dev')
        self.linking.review_case(self.case, frz1, decision='approved')
        self.linking.review_case(self.case, frz2, decision='approved')
        db.session.commit()
        result = self.linking.confirm_link(self.actor, self.case,
                                           preview_digest=self.out['digest'])
        db.session.commit()
        self.assertEqual(result['state'], 'applied')
        db.session.refresh(self.b)
        self.assertEqual(self.b.lifecycle, 'active')      # 不斩断在途业务
        self.assertEqual(self.b.person_id, self.a.person_id)  # 已归存续人员（nonprimary）
        grant = IdentityExceptionGrantModel.query.filter_by(
            user_id=self.b.id, operation_scope='camp_join').one()
        self.assertEqual(grant.state, 'active')
        # 冻结：merged + 撤宽限 + bump
        reviewer = self.make_user('frz@x.dev')
        self.linking.freeze_merged_account(self.case, reviewer)
        db.session.commit()
        db.session.refresh(self.b)
        self.assertEqual(self.b.lifecycle, 'merged')
        db.session.refresh(grant)
        self.assertEqual(grant.state, 'expired')
        # 幂等
        self.linking.freeze_merged_account(self.case, reviewer)
        db.session.commit()

    def test_approved_then_confirm_applies(self):
        from models import PersonModel
        self.linking.review_case(self.case, self.r1, decision='approved')
        self.linking.review_case(self.case, self.r2, decision='approved')
        db.session.commit()
        result = self.linking.confirm_link(self.actor, self.case,
                                           preview_digest=self.out['digest'])
        db.session.commit()
        self.assertEqual(result['state'], 'applied')
        self.assertEqual(self.b.person_id, self.a.person_id)


class WithdrawTest(LinkingTestBase):

    def test_withdraw_releases_locks(self):
        from models import IdentityCaseAccountLockModel
        a = self.make_user('a@x.dev')
        actor, _ = self.make_actor(a)
        case = self.linking.create_case(actor)
        db.session.commit()
        self.linking.withdraw_case(actor, case)
        db.session.commit()
        self.assertEqual(case.state, 'cancelled')
        self.assertIsNone(db.session.get(IdentityCaseAccountLockModel, a.id))
        # 撤回后可重新建案例
        case2 = self.linking.create_case(actor)
        db.session.commit()
        self.assertNotEqual(case2.id, case.id)

    def test_applied_cannot_withdraw(self):
        from services.auth_context import AuthRejected
        a = self.make_user('a@x.dev')
        b = self.make_user('b@x.dev')
        actor, _ = self.make_actor(a)
        case = self.linking.create_case(actor)
        self.linking.prove_initiator(actor, case)
        self.linking.prove_target(actor, case, target_email='b@x.dev', password='x' * 32)
        db.session.commit()
        out = self.linking.build_preview(actor, case)
        db.session.commit()
        self.linking.confirm_link(actor, case, preview_digest=out['digest'])
        db.session.commit()
        with self.assertRaises(AuthRejected) as c:
            self.linking.withdraw_case(actor, case)
        self.assertEqual(c.exception.machine, 'BAD_STATE')


if __name__ == '__main__':
    unittest.main(verbosity=2)

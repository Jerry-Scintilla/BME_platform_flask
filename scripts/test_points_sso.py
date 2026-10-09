"""积分商城 SSO 的隔离回归：只使用内存 SQLite 与独立内存限流器，不连 Redis、
不访问真实积分中心。覆盖规划修订版 §6.1 用例表：登录与账号、开关与邮箱、
业务调用顺序、绑定冲突、异常映射、签名规范化串与实际字节、nonce、配置校验、限流。
"""
import base64
import hashlib
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import patch

# 直接执行 scripts/test_points_sso.py 时，将仓库根目录加入模块搜索路径。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flask import Flask
from flask_jwt_extended import JWTManager, create_access_token
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from requests.models import Response
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy.dialects.mysql import LONGTEXT, MEDIUMTEXT
from sqlalchemy.ext.compiler import compiles
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.exceptions import InvalidSignature

import points_center_client
from points_center_client import (
    EMPTY_BODY_SHA256,
    PointsCenterError,
    init_points_center,
)
from exts import db
from models import UserModel


@compiles(MEDIUMTEXT, 'sqlite')
@compiles(LONGTEXT, 'sqlite')
def _mysql_text_on_sqlite(_element, _compiler, **_kwargs):
    return 'TEXT'


VALID_CALLBACK = "https://store.example.edu.cn/sso/callback"
VALID_BASE_URL = "http://172.25.56.83:18081"
PLATFORM_ID = "sysu_camp_test"
KEY_ID = "camp-key-test"


def _generate_key_b64():
    """生成测试用 raw(32B) Base64 Ed25519 私钥，同时返回私钥对象。"""
    key = Ed25519PrivateKey.generate()
    raw = key.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    return base64.b64encode(raw).decode("ascii"), key


def _make_response(status_code=200, payload=None, text="", headers=None):
    resp = Response()
    resp.status_code = status_code
    if payload is not None:
        resp._content = json.dumps(payload).encode("utf-8")
        resp.headers["Content-Type"] = "application/json"
    else:
        resp._content = text.encode("utf-8")
    for k, v in (headers or {}).items():
        resp.headers[k] = v
    return resp


def _build_app(enabled=True):
    """搭建与生产等价的隔离 app：内存库 + JWT + 积分中心配置 + 内存限流器。"""
    app = Flask(__name__)
    key_b64, key = _generate_key_b64()
    app.config.update(
        SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
        JWT_SECRET_KEY='test-secret-key-with-at-least-32-characters',
        POINTS_CENTER_ENABLED=enabled,
        POINTS_CENTER_BASE_URL=VALID_BASE_URL,
        POINTS_PLATFORM_ID=PLATFORM_ID,
        POINTS_KEY_ID=KEY_ID,
        POINTS_PRIVATE_KEY_B64=key_b64,
        POINTS_STORE_CALLBACK_URL=VALID_CALLBACK,
    )
    db.init_app(app)
    JWTManager(app)
    init_points_center(app)
    return app, key


class PointsSSORouteTest(unittest.TestCase):
    """POST /points-sso/ticket 路由层行为（client 全部 mock）。"""

    def setUp(self):
        # 缺失业务 mock 时立即失败，防止隔离用例意外访问真实积分中心。
        request_guard = patch('points_center_client.requests.request',
                              side_effect=AssertionError('隔离测试禁止真实积分中心请求'))
        request_guard.start()
        self.addCleanup(request_guard.stop)
        self.app, _ = _build_app(enabled=True)
        self.test_limiter = Limiter(key_func=get_remote_address, storage_uri="memory://")
        self.test_limiter.init_app(self.app)
        from blueprints.points_sso import bp as points_sso_bp
        self.app.register_blueprint(points_sso_bp)

        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.user = UserModel(username='学员', email='student@mail2.sysu.edu.cn')
        self.user.set_password('a' * 32)
        self.outsider = UserModel(username='外校', email='someone@qq.com')
        self.outsider.set_password('a' * 32)
        db.session.add_all([self.user, self.outsider])
        db.session.commit()

        self.client = self.app.test_client()
        # 路由内部经模块全局名 limiter 取限流器，替换为独立内存实例（不连 Redis）
        self._limiter_patcher = patch('blueprints.points_sso.limiter', self.test_limiter)
        self._limiter_patcher.start()
        self.addCleanup(self._limiter_patcher.stop)

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _auth(self, user):
        token = create_access_token(identity=user.email)
        return {'Authorization': f'Bearer {token}'}

    def _post(self, user):
        return self.client.post('/points-sso/ticket', headers=self._auth(user))

    def _verify_school_email(self, user=None):
        """走现有申请、验证码与负责人批准流程，证明普通登录邮箱不需变更。"""
        from models import IdentitySchoolConfigModel, PersonIdentityModel
        from services.identity import person, verification
        from test_identity_verification import _FakeRedis
        user = user or self.outsider
        person.create_provisional(user)
        reviewer = UserModel(username='审核人', email='reviewer@example.com', role='super_admin')
        reviewer.set_password('a' * 32)
        db.session.add(reviewer)
        db.session.flush()
        db.session.add(IdentitySchoolConfigModel(
            school_id='sysu', name='中山大学',
            personal_email_domains=['mail2.sysu.edu.cn', 'mail.sysu.edu.cn'],
            excluded_email_domains=[], email_local_matches_identifier=True,
            reviewer_user_ids=[reviewer.id]))
        db.session.flush()
        application = verification.create_or_update_application(
            user, school_id='sysu', claimed_name='学员', claimed_identifier='verified01',
            contact_email='verified01@mail2.sysu.edu.cn')
        with patch.object(verification, 'redis_client', _FakeRedis()):
            code, _ = verification.issue_challenge(user, application)
            self.assertTrue(verification.verify_challenge(user, application, code))
        verification.submit_application(user, application)
        verification.approve_application(application, reviewer)
        db.session.commit()
        identity = PersonIdentityModel.query.filter_by(person_id=user.person_id).one()
        return application, identity

    def test_qq_registration_then_real_verification_enters_with_verified_email(self):
        application, _ = self._verify_school_email()
        original_email = self.outsider.email
        with patch('points_center_client.ensure_user') as ensure, \
                patch('points_center_client.create_sso_ticket', return_value=('verified-ticket', 90)) as ticket:
            response = self._post(self.outsider)
        self.assertEqual(response.status_code, 200)
        uid = str(self.outsider.id).zfill(7)
        ensure.assert_called_once_with(application.contact_email, uid)
        ticket.assert_called_once_with(uid)
        self.assertEqual(self.outsider.email, original_email)

    def test_eligibility_is_read_only_private_and_separate_from_open_switch(self):
        application, _ = self._verify_school_email()
        self.app.config['POINTS_CENTER_ENABLED'] = False
        with patch('points_center_client.ensure_user') as ensure, \
                patch('points_center_client.create_sso_ticket') as ticket:
            response = self.client.get('/points-sso/eligibility', headers=self._auth(self.outsider))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json['eligible'])
        self.assertFalse(response.json['enabled'])
        self.assertEqual(response.json['source'], 'verified_school_email')
        self.assertNotIn(application.contact_email, response.get_data(as_text=True))
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        ensure.assert_not_called()
        ticket.assert_not_called()
        self.assertEqual(self.client.get('/points-sso/eligibility').status_code, 401)

    def test_unverified_or_untrusted_evidence_never_calls_points_center(self):
        from models import PersonModel
        application, identity = self._verify_school_email()
        person = db.session.get(PersonModel, self.outsider.person_id)
        cases = [
            (person, 'verification_status', 'unverified'),
            (person, 'verification_status', 'pending'),
            (person, 'verification_status', 'disputed'),
            (person, 'verification_status', 'revoked'),
            (person, 'record_status', 'merged'),
            (identity, 'proof_status', 'revoked'),
            (identity, 'issuer', 'external:other'),
            (identity, 'assurance_method', 'manual_review'),
            (identity, 'kind', 'roster_ref'),
            (identity, 'proof_ref', 'application#999999'),
            (identity, 'proof_ref', 'self-claimed'),
            (identity, 'canonical_key', 'someone_else'),
            (application, 'status', 'submitted'),
            (application, 'status', 'rejected'),
            (application, 'challenge_verified_at', None),
            (application, 'reviewed_at', None),
            (application, 'reviewed_by', None),
            (application, 'method', 'manual'),
            (application, 'school_id', 'external:other'),
            (application, 'contact_email', 'fake@mail2.sysu.edu.cn.evil.com'),
            (application, 'applicant_user_id', self.user.id),
        ]
        for obj, field, value in cases:
            with self.subTest(field=field, value=value):
                original = getattr(obj, field)
                setattr(obj, field, value)
                db.session.commit()
                with patch('points_center_client.ensure_user') as ensure, \
                        patch('points_center_client.create_sso_ticket') as ticket:
                    response = self._post(self.outsider)
                    status = self.client.get('/points-sso/eligibility', headers=self._auth(self.outsider))
                self.assertIn(response.status_code, (400, 403))
                self.assertFalse(status.json['eligible'])
                ensure.assert_not_called()
                ticket.assert_not_called()
                setattr(obj, field, original)
                db.session.commit()

    def test_revocation_after_eligibility_is_rechecked_on_ticket(self):
        _, identity = self._verify_school_email()
        self.assertTrue(self.client.get('/points-sso/eligibility', headers=self._auth(self.outsider)).json['eligible'])
        identity.proof_status = 'revoked'
        db.session.commit()
        with patch('points_center_client.ensure_user') as ensure:
            self.assertEqual(self._post(self.outsider).status_code, 400)
        ensure.assert_not_called()

    def test_campus_login_retains_original_binding_email_after_verification(self):
        self._verify_school_email(self.user)
        with patch('points_center_client.ensure_user') as ensure, \
                patch('points_center_client.create_sso_ticket', return_value=('ticket', 90)):
            self.assertEqual(self._post(self.user).status_code, 200)
        ensure.assert_called_once_with(self.user.email, str(self.user.id).zfill(7))

    def test_migrated_identity_proof_follows_current_person_after_account_merge(self):
        from models import PersonModel
        application, identity = self._verify_school_email()
        old_person = db.session.get(PersonModel, self.outsider.person_id)
        surviving = PersonModel(public_id=uuid.uuid4().hex, verification_status='verified', record_status='active')
        db.session.add(surviving)
        db.session.flush()
        self.user.email = 'survivor@qq.com'
        self.user.person_id = surviving.id
        self.outsider.person_id = surviving.id
        self.outsider.lifecycle = 'merged'
        identity.person_id = surviving.id
        old_person.record_status = 'merged'
        old_person.merged_to_person_id = surviving.id
        db.session.commit()
        self.assertNotEqual(application.applicant_person_id, surviving.id)
        with patch('points_center_client.ensure_user') as ensure, \
                patch('points_center_client.create_sso_ticket', return_value=('ticket', 90)):
            self.assertEqual(self._post(self.user).status_code, 200)
        ensure.assert_called_once_with(application.contact_email, str(self.user.id).zfill(7))

    def test_multiple_verified_emails_require_review(self):
        from models import IdentityApplicationModel, PersonIdentityModel
        application, _ = self._verify_school_email()
        other = IdentityApplicationModel(
            school_id='sysu', applicant_user_id=self.outsider.id,
            applicant_person_id=self.outsider.person_id, claimed_name='学员',
            claimed_identifier='another', contact_email='another@mail.sysu.edu.cn',
            method='school_email', status='approved', challenge_verified_at=application.challenge_verified_at,
            reviewed_by=application.reviewed_by, reviewed_at=application.reviewed_at)
        db.session.add(other)
        db.session.flush()
        db.session.add(PersonIdentityModel(
            person_id=self.outsider.person_id, issuer='sysu', kind='netid',
            canonical_key='another', proof_status='verified', assurance_method='school_email',
            proof_ref=f'application#{other.id}'))
        db.session.commit()
        with patch('points_center_client.ensure_user') as ensure:
            response = self._post(self.outsider)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json['error_code'], 'POINTS_IDENTITY_AMBIGUOUS')
        ensure.assert_not_called()

    def test_verified_qq_binding_conflict_does_not_issue_ticket(self):
        self._verify_school_email()
        with patch('points_center_client.ensure_user', side_effect=PointsCenterError('conflict', category='HTTP', status_code=409)), \
                patch('points_center_client.create_sso_ticket') as ticket:
            response = self._post(self.outsider)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json['error_code'], 'POINTS_BINDING_CONFLICT')
        ticket.assert_not_called()

    def test_browser_cannot_supply_verified_identity_or_another_users_email(self):
        with patch('points_center_client.ensure_user') as ensure:
            response = self.client.post('/points-sso/ticket', headers=self._auth(self.outsider), json={
                'email': self.user.email, 'person_id': self.user.person_id,
                'verification_status': 'verified', 'eligible': True})
        self.assertEqual(response.status_code, 400)
        ensure.assert_not_called()

    # ── 登录与账号 ──────────────────────────────────────────────
    def test_missing_token_rejected(self):
        resp = self.client.post('/points-sso/ticket')
        self.assertEqual(resp.status_code, 401)

    def test_unknown_user_401(self):
        ghost = type('U', (), {'email': 'ghost@mail2.sysu.edu.cn'})()
        resp = self._post(ghost)
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.get_json()['error_code'], 'AUTH_USER_MISSING')

    def test_banned_user_401(self):
        # D1 安全地基起 _current_user 收口 services.auth_context.resolve_actor
        # （带账号生命周期校验）：本测试的裸 Flask app 未挂真 app 的入口守卫
        # （enforce_request_access），封禁账号在解析层即得 None → 401 AUTH_USER_MISSING，
        # 走不到蓝图内自带的封禁 403 分支（该分支保留作纵深防御）。生产真 app 中
        # 封禁用户由入口守卫先拦，仍为 403 ACCOUNT_BANNED。无论哪层拒绝，上游不得被调用。
        self.user.status = 'banned'
        db.session.commit()
        with patch('points_center_client.ensure_user') as mock_ensure:
            resp = self._post(self.user)
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.get_json()['error_code'], 'AUTH_USER_MISSING')
        mock_ensure.assert_not_called()

    # ── 开关与邮箱 ──────────────────────────────────────────────
    def test_disabled_returns_503_without_upstream_call(self):
        self.app.config['POINTS_CENTER_ENABLED'] = False
        with patch('points_center_client.ensure_user') as mock_ensure:
            resp = self._post(self.user)
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.get_json()['message'], '积分商城暂未开放')
        mock_ensure.assert_not_called()

    def test_non_campus_email_rejected(self):
        with patch('points_center_client.ensure_user') as mock_ensure:
            resp = self._post(self.outsider)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()['error_code'], 'EMAIL_DOMAIN')
        mock_ensure.assert_not_called()

    def test_lookalike_domain_rejected(self):
        self.outsider.email = 'evil@mail.sysu.edu.cn.example.com'
        db.session.commit()
        with patch('points_center_client.ensure_user') as mock_ensure:
            resp = self._post(self.outsider)
        self.assertEqual(resp.status_code, 400)
        mock_ensure.assert_not_called()

    def test_uppercase_domain_normalized(self):
        self.user.email = 'Student@MAIL2.SYSU.EDU.CN'
        db.session.commit()
        with patch('points_center_client.ensure_user') as mock_ensure, \
             patch('points_center_client.create_sso_ticket',
                   return_value=('t' * 64, 90)):
            resp = self._post(self.user)
        self.assertEqual(resp.status_code, 200)
        mock_ensure.assert_called_once_with('Student@mail2.sysu.edu.cn',
                                            str(self.user.id).zfill(7))

    # ── 业务调用 ────────────────────────────────────────────────
    def test_success_contract_and_order(self):
        with patch('points_center_client.ensure_user') as mock_ensure, \
             patch('points_center_client.create_sso_ticket',
                   return_value=('a1b2c3', 90)) as mock_ticket:
            resp = self._post(self.user)
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data['code'], 200)
        self.assertEqual(data['ticket'], 'a1b2c3')
        self.assertEqual(data['expires_in'], 90)
        self.assertEqual(data['store_callback_url'], VALID_CALLBACK)
        self.assertEqual(resp.headers.get('Cache-Control'), 'no-store')
        mock_ensure.assert_called_once_with('student@mail2.sysu.edu.cn',
                                            str(self.user.id).zfill(7))
        mock_ticket.assert_called_once_with(str(self.user.id).zfill(7))

    def test_ensure_conflict_409_and_no_ticket(self):
        err = PointsCenterError('conflict', category='HTTP', status_code=409)
        with patch('points_center_client.ensure_user', side_effect=err), \
             patch('points_center_client.create_sso_ticket') as mock_ticket:
            resp = self._post(self.user)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.get_json()['error_code'], 'POINTS_BINDING_CONFLICT')
        mock_ticket.assert_not_called()

    def test_ensure_generic_4xx_stops_flow(self):
        err = PointsCenterError('bad', category='HTTP', status_code=400)
        with patch('points_center_client.ensure_user', side_effect=err), \
             patch('points_center_client.create_sso_ticket') as mock_ticket:
            resp = self._post(self.user)
        self.assertEqual(resp.status_code, 502)
        mock_ticket.assert_not_called()

    # ── 异常映射：上游认证错误不得变成 401/403 ──────────────────
    def test_upstream_401_maps_to_502(self):
        err = PointsCenterError('unauthorized', category='HTTP', status_code=401)
        with patch('points_center_client.ensure_user', side_effect=err):
            resp = self._post(self.user)
        self.assertEqual(resp.status_code, 502)
        self.assertEqual(resp.get_json()['error_code'], 'POINTS_INTEGRATION_ERROR')

    def test_upstream_timeout_maps_to_504(self):
        err = PointsCenterError('timeout', category='TIMEOUT')
        with patch('points_center_client.ensure_user'), \
             patch('points_center_client.create_sso_ticket', side_effect=err):
            resp = self._post(self.user)
        self.assertEqual(resp.status_code, 504)

    def test_upstream_network_error_maps_to_502(self):
        err = PointsCenterError('conn', category='NETWORK')
        with patch('points_center_client.ensure_user', side_effect=err):
            resp = self._post(self.user)
        self.assertEqual(resp.status_code, 502)
        self.assertEqual(resp.get_json()['error_code'], 'POINTS_UPSTREAM_ERROR')

    def test_upstream_429_passthrough_with_retry_after(self):
        err = PointsCenterError('busy', category='HTTP', status_code=429, retry_after=17)
        with patch('points_center_client.ensure_user', side_effect=err):
            resp = self._post(self.user)
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(resp.headers.get('Retry-After'), '17')

    def test_upstream_5xx_maps_to_502(self):
        err = PointsCenterError('server', category='HTTP', status_code=500)
        with patch('points_center_client.ensure_user', side_effect=err):
            resp = self._post(self.user)
        self.assertEqual(resp.status_code, 502)
        self.assertEqual(resp.get_json()['error_code'], 'POINTS_UPSTREAM_ERROR')

    def test_upstream_3xx_treated_as_error(self):
        err = PointsCenterError('redirect', category='HTTP', status_code=302)
        with patch('points_center_client.ensure_user', side_effect=err):
            resp = self._post(self.user)
        self.assertEqual(resp.status_code, 502)

    # ── 限流 ────────────────────────────────────────────────────
    def test_user_bucket_10_per_minute(self):
        with patch('points_center_client.ensure_user'), \
             patch('points_center_client.create_sso_ticket', return_value=('t', 90)):
            for _ in range(10):
                self.assertEqual(self._post(self.user).status_code, 200)
            resp = self._post(self.user)
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(resp.get_json()['error_code'], 'RATE_LIMITED')

    def test_same_ip_different_users_independent(self):
        with patch('points_center_client.ensure_user'), \
             patch('points_center_client.create_sso_ticket', return_value=('t', 90)):
            for _ in range(10):
                self.assertEqual(self._post(self.user).status_code, 200)
            # 同 IP 出口的另一用户不受前者用户桶影响
            self.assertEqual(self._post(self.outsider).status_code, 400)  # 邮箱先拦
        # 换成校园邮箱的第二用户应正常
        self.outsider.email = 'second@mail.sysu.edu.cn'
        db.session.commit()
        with patch('points_center_client.ensure_user'), \
             patch('points_center_client.create_sso_ticket', return_value=('t', 90)):
            self.assertEqual(self._post(self.outsider).status_code, 200)

    def test_limiter_storage_failure_503_and_no_upstream(self):
        from limits.errors import StorageError

        class BrokenLimiter:
            def limit(self, *args, **kwargs):
                raise StorageError("storage down")

        with patch('blueprints.points_sso.limiter', BrokenLimiter()), \
             patch('points_center_client.ensure_user') as mock_ensure:
            resp = self._post(self.user)
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.get_json()['error_code'], 'LIMITER_UNAVAILABLE')
        mock_ensure.assert_not_called()

    def test_redis_connection_failure_503_and_no_upstream(self):
        # 生产 RedisStorage 默认不包装异常，需覆盖原始 Redis 连接错误。
        with patch.object(self.test_limiter.storage, 'incr',
                          side_effect=RedisConnectionError('storage down')), \
             patch('points_center_client.ensure_user') as mock_ensure:
            resp = self._post(self.user)
        self.assertEqual(resp.status_code, 503)
        mock_ensure.assert_not_called()

    def test_total_bucket_20_per_second(self):
        headers = self._auth(self.user)
        users = [SimpleNamespace(id=i, email=self.user.email, status='active')
                 for i in range(1, 22)]
        with patch('blueprints.points_sso._current_user', side_effect=users), \
             patch('time.time', return_value=time.time()), \
             patch('points_center_client.ensure_user'), \
             patch('points_center_client.create_sso_ticket', return_value=('t', 90)):
            for _ in range(20):
                self.assertEqual(self.client.post('/points-sso/ticket', headers=headers).status_code, 200)
            self.assertEqual(self.client.post('/points-sso/ticket', headers=headers).status_code, 429)

    def test_ip_bucket_300_per_minute(self):
        headers = self._auth(self.user)
        users = [SimpleNamespace(id=i, email=self.user.email, status='active')
                 for i in range(1, 302)]
        clock = [int(time.time() // 60) * 60 + 1]
        # 每秒最多 10 次，总时长 30 秒，排除用户桶与总量桶先触发的影响。
        with patch('blueprints.points_sso._current_user', side_effect=users), \
             patch('time.time', side_effect=lambda: clock[0]), \
             patch('points_center_client.ensure_user'), \
             patch('points_center_client.create_sso_ticket', return_value=('t', 90)):
            for i in range(300):
                clock[0] = int(clock[0]) + (1 if i and i % 10 == 0 else 0)
                self.assertEqual(self.client.post('/points-sso/ticket', headers=headers).status_code, 200)
            self.assertEqual(self.client.post('/points-sso/ticket', headers=headers).status_code, 429)

    def test_upstream_error_body_not_logged(self):
        sensitive = 'sensitive-ticket-should-never-appear'
        bad = _make_response(status_code=500,
                             payload={'detail': sensitive, 'ticket': sensitive})
        # 本例需走真实客户端异常构造，但请求本身仍由 mock 截获。
        with patch('points_center_client.requests.request', return_value=bad), \
             self.assertLogs(self.app.logger, level='ERROR') as logs:
            resp = self._post(self.user)
        self.assertEqual(resp.status_code, 502)
        self.assertNotIn(sensitive, '\n'.join(logs.output))


class PointsCenterClientTest(unittest.TestCase):
    """签名协议与上游响应校验（mock requests，验证实际字节）。"""

    def setUp(self):
        self.app, self.key = _build_app(enabled=True)
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.public_key = self.key.public_key()

    def tearDown(self):
        self.ctx.pop()

    def _fixed_identity(self):
        return (
            patch('points_center_client.time.time', return_value=1791079918.0),
            patch('points_center_client.uuid.uuid4',
                  return_value=uuid.UUID('a1b2c3d4e5f60718293a4b5c6d7e8f90')),
        )

    def test_canonical_string_and_signature(self):
        payload = {"platform_user_id": "0000123"}
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        t_patch, n_patch = self._fixed_identity()
        with t_patch, n_patch:
            headers = points_center_client._sign_and_build_headers(
                "POST", "/platform-api/v1/sso/tickets", body)
        canonical = (
            f"POINTS-SIGN-V1\n{PLATFORM_ID}\n{KEY_ID}\n1791079918\n"
            f"a1b2c3d4e5f60718293a4b5c6d7e8f90\nPOST\n/platform-api/v1/sso/tickets\n"
            f"{hashlib.sha256(body).hexdigest()}\n"
        )
        # 末尾恰好一个换行：签名可用测试公钥按该明文验签通过
        self.public_key.verify(
            base64.b64decode(headers["X-Signature"]), canonical.encode("utf-8"))
        for h in ("X-Platform-ID", "X-Key-ID", "X-Timestamp", "X-Nonce", "X-Signature"):
            self.assertIn(h, headers)

    def test_canonical_rejects_tampered_body(self):
        body = b'{"platform_user_id":"0000123"}'
        t_patch, n_patch = self._fixed_identity()
        with t_patch, n_patch:
            headers = points_center_client._sign_and_build_headers("POST", "/x", body)
        with self.assertRaises(InvalidSignature):
            self.public_key.verify(
                base64.b64decode(headers["X-Signature"]), b'{"platform_user_id":"9999999"}')

    def test_empty_body_sha256_constant(self):
        self.assertEqual(hashlib.sha256(b"").hexdigest(), EMPTY_BODY_SHA256)

    def test_request_sends_signed_exact_bytes(self):
        captured = {}

        def fake_request(method, url, **kwargs):
            captured.update(kwargs, method=method, url=url)
            return _make_response(payload={"platform_user_id": "0000123",
                                           "email": "student@mail2.sysu.edu.cn",
                                           "status": "active"})

        with patch('points_center_client.requests.request', side_effect=fake_request):
            points_center_client.ensure_user("student@mail2.sysu.edu.cn", "0000123")
        # 同一份 body_bytes 用于哈希/签名/发送：URL、方法与字节完全受控
        self.assertEqual(captured['method'], "POST")
        self.assertEqual(captured['url'],
                         f"{VALID_BASE_URL}/platform-api/v1/users/ensure")
        body = captured['data']
        self.assertEqual(json.loads(body), {
            "email": "student@mail2.sysu.edu.cn",
            "platform_user_id": "0000123",
            "allow_rebind": False,
        })
        canonical = (
            f"POINTS-SIGN-V1\n{PLATFORM_ID}\n{KEY_ID}\n{captured['headers']['X-Timestamp']}\n"
            f"{captured['headers']['X-Nonce']}\nPOST\n/platform-api/v1/users/ensure\n"
            f"{hashlib.sha256(body).hexdigest()}\n"
        )
        self.public_key.verify(
            base64.b64decode(captured['headers']['X-Signature']),
            canonical.encode("utf-8"))

    def test_non_ascii_payload_serialized_once(self):
        captured = {}

        def fake_request(method, url, **kwargs):
            captured.update(kwargs)
            return _make_response(payload={"user_id": 1, "email": "张三@mail2.sysu.edu.cn",
                                           "platform_user_id": "0000123",
                                           "status": "active", "is_new_user": True})

        with patch('points_center_client.requests.request', side_effect=fake_request):
            points_center_client.ensure_user("张三@mail2.sysu.edu.cn", "0000123")
        body = captured['data']
        self.assertIn("张三".encode("utf-8"), body)  # ensure_ascii=False
        self.assertNotIn(b"\\u", body)
        self.assertEqual(json.loads(body)["email"], "张三@mail2.sysu.edu.cn")

    def test_nonce_not_reused_between_calls(self):
        nonces = []

        def fake_request(method, url, **kwargs):
            nonces.append(kwargs['headers']['X-Nonce'])
            if url.endswith('/users/ensure'):
                return _make_response(payload={"platform_user_id": "0000123",
                                               "email": "a@mail2.sysu.edu.cn",
                                               "status": "active"})
            return _make_response(payload={"ticket": "x" * 64, "expires_in": 90})

        with patch('points_center_client.requests.request', side_effect=fake_request):
            points_center_client.ensure_user("a@mail2.sysu.edu.cn", "0000123")
            points_center_client.create_sso_ticket("0000123")
        self.assertEqual(len(nonces), 2)
        self.assertNotEqual(nonces[0], nonces[1])

    def test_upstream_error_status_surfaced(self):
        err_resp = _make_response(status_code=429, payload={"detail": "rate"},
                                  headers={"Retry-After": "5"})
        with patch('points_center_client.requests.request', return_value=err_resp):
            with self.assertRaises(PointsCenterError) as ctx:
                points_center_client.create_sso_ticket("0000123")
        self.assertEqual(ctx.exception.category, "HTTP")
        self.assertEqual(ctx.exception.status_code, 429)
        self.assertEqual(ctx.exception.retry_after, 5)

    def test_non_json_response_rejected(self):
        bad = _make_response(status_code=200, text="<html>gateway</html>")
        with patch('points_center_client.requests.request', return_value=bad):
            with self.assertRaises(PointsCenterError) as ctx:
                points_center_client.create_sso_ticket("0000123")
        self.assertEqual(ctx.exception.category, "RESPONSE")

    def test_redirect_treated_as_error(self):
        redir = _make_response(status_code=302, text="")
        with patch('points_center_client.requests.request', return_value=redir):
            with self.assertRaises(PointsCenterError) as ctx:
                points_center_client.ensure_user("a@mail2.sysu.edu.cn", "0000123")
        self.assertEqual(ctx.exception.category, "HTTP")
        self.assertEqual(ctx.exception.status_code, 302)

    def test_ensure_contract_mismatch_rejected(self):
        wrong = _make_response(payload={"platform_user_id": "9999999",
                                        "email": "a@mail2.sysu.edu.cn",
                                        "status": "active"})
        with patch('points_center_client.requests.request', return_value=wrong):
            with self.assertRaises(PointsCenterError) as ctx:
                points_center_client.ensure_user("a@mail2.sysu.edu.cn", "0000123")
        self.assertEqual(ctx.exception.category, "CONTRACT")

    def test_ensure_inactive_status_rejected(self):
        wrong = _make_response(payload={"platform_user_id": "0000123",
                                        "email": "a@mail2.sysu.edu.cn",
                                        "status": "suspended"})
        with patch('points_center_client.requests.request', return_value=wrong):
            with self.assertRaises(PointsCenterError) as ctx:
                points_center_client.ensure_user("a@mail2.sysu.edu.cn", "0000123")
        self.assertEqual(ctx.exception.category, "CONTRACT")

    def test_ensure_accepts_production_uppercase_active_without_rebinding(self):
        response = _make_response(payload={'platform_user_id': '0000123',
                                           'email': 'a@mail2.sysu.edu.cn', 'status': 'ACTIVE'})
        with patch('points_center_client.requests.request', return_value=response) as request:
            points_center_client.ensure_user('a@mail2.sysu.edu.cn', '0000123')
        self.assertFalse(json.loads(request.call_args.kwargs['data'])['allow_rebind'])

    def test_ticket_expires_in_mismatch_stops(self):
        odd = _make_response(payload={"ticket": "x" * 64, "expires_in": 120})
        with patch('points_center_client.requests.request', return_value=odd):
            with self.assertRaises(PointsCenterError) as ctx:
                points_center_client.create_sso_ticket("0000123")
        self.assertEqual(ctx.exception.category, "CONTRACT")
        self.assertIn("90", str(ctx.exception.message))


class InitValidationTest(unittest.TestCase):
    """init_points_center 配置校验与 app 隔离。"""

    def _app_with(self, **overrides):
        key_b64, key = _generate_key_b64()
        cfg = dict(
            POINTS_CENTER_ENABLED=True,
            POINTS_CENTER_BASE_URL=VALID_BASE_URL,
            POINTS_PLATFORM_ID=PLATFORM_ID,
            POINTS_KEY_ID=KEY_ID,
            POINTS_PRIVATE_KEY_B64=key_b64,
            POINTS_STORE_CALLBACK_URL=VALID_CALLBACK,
        )
        cfg.update(overrides)
        app = Flask(__name__)
        app.config.update(cfg)
        return app, key

    def test_disabled_skips_validation(self):
        app, _ = self._app_with(POINTS_CENTER_ENABLED=False, POINTS_PLATFORM_ID="",
                                POINTS_PRIVATE_KEY_B64="")
        init_points_center(app)  # 关闭时缺配置不报错、不初始化
        self.assertNotIn(points_center_client.EXTENSION_KEY, app.extensions)

    def test_missing_platform_id_rejected(self):
        app, _ = self._app_with(POINTS_PLATFORM_ID="")
        with self.assertRaises(RuntimeError):
            init_points_center(app)

    def test_placeholder_rejected(self):
        app, _ = self._app_with(POINTS_PLATFORM_ID="XXXXXXXXXXX")
        with self.assertRaises(RuntimeError):
            init_points_center(app)

    def test_bad_base64_rejected(self):
        app, _ = self._app_with(POINTS_PRIVATE_KEY_B64="not-base64!!")
        with self.assertRaises(RuntimeError):
            init_points_center(app)

    def test_wrong_key_length_rejected(self):
        short = base64.b64encode(b"\x01" * 31).decode("ascii")
        app, _ = self._app_with(POINTS_PRIVATE_KEY_B64=short)
        with self.assertRaises(RuntimeError):
            init_points_center(app)

    def test_pem_text_rejected(self):
        pem = ("-----BEGIN PRIVATE KEY-----\nMC4CAQAwBQYDK2VwBCIEIKx\n-----END PRIVATE KEY-----\n")
        app, _ = self._app_with(POINTS_PRIVATE_KEY_B64=base64.b64encode(pem.encode()).decode())
        with self.assertRaises(RuntimeError):
            init_points_center(app)

    def test_callback_path_must_be_exact(self):
        app, _ = self._app_with(POINTS_STORE_CALLBACK_URL="https://store.example.edu.cn/other")
        with self.assertRaises(RuntimeError):
            init_points_center(app)

    def test_base_url_with_path_rejected(self):
        app, _ = self._app_with(POINTS_CENTER_BASE_URL="http://172.25.56.83:18081/prefix")
        with self.assertRaises(RuntimeError):
            init_points_center(app)

    def test_two_apps_key_isolation(self):
        app1, key1 = self._app_with()
        app2, key2 = self._app_with()
        init_points_center(app1)
        init_points_center(app2)
        s1 = app1.extensions[points_center_client.EXTENSION_KEY]["private_key"]
        s2 = app2.extensions[points_center_client.EXTENSION_KEY]["private_key"]
        raw1 = s1.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                serialization.NoEncryption())
        raw2 = s2.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                serialization.NoEncryption())
        self.assertNotEqual(raw1, raw2)


if __name__ == '__main__':
    unittest.main()

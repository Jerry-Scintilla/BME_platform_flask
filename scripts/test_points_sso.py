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

    # ── 登录与账号 ──────────────────────────────────────────────
    def test_missing_token_rejected(self):
        resp = self.client.post('/points-sso/ticket')
        self.assertEqual(resp.status_code, 401)

    def test_unknown_user_401(self):
        ghost = type('U', (), {'email': 'ghost@mail2.sysu.edu.cn'})()
        resp = self._post(ghost)
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.get_json()['error_code'], 'AUTH_USER_MISSING')

    def test_banned_user_403(self):
        self.user.status = 'banned'
        db.session.commit()
        with patch('points_center_client.ensure_user') as mock_ensure:
            resp = self._post(self.user)
        self.assertEqual(resp.status_code, 403)
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

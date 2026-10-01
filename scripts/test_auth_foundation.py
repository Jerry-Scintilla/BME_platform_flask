"""D1 身份安全地基的隔离回归；只使用内存 SQLite（assertions 覆盖规格第 6/16 章「会话」行）。

覆盖随批次扩展：
  B2 模型层：user 新列默认值 / auth_session 生命周期 / 摘要唯一约束
  B3 解析层：resolve_actor 矩阵 / request_guard 路径分类 / 审计脱敏
  B4 会话层：轮换与重放 / bump 撤权 / legacy 兑换 / 验证码 purpose
  B6 MFA 层：TOTP 防重放 / 恢复码一次性

用法（项目根）：.venv/bin/python scripts/test_auth_foundation.py
"""
import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from flask import Flask
from flask_jwt_extended import JWTManager
from sqlalchemy import event
from sqlalchemy.dialects.mysql import LONGTEXT, MEDIUMTEXT
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.compiler import compiles

from exts import db
from services.request_guard import enforce_request_access
from models import (
    AuthFactorModel, AuthLegacyRefreshConsumptionModel, AuthRecoveryCodeModel,
    AuthSessionModel, UserModel,
)


@compiles(MEDIUMTEXT, 'sqlite')
@compiles(LONGTEXT, 'sqlite')
def _mysql_text_on_sqlite(_element, _compiler, **_kwargs):
    return 'TEXT'


class AuthFoundationTestBase(unittest.TestCase):
    """共用脚手架：内存 SQLite + JWT；各用例自建用户/会话。"""

    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
            JWT_SECRET_KEY='test-secret-key-with-at-least-32-characters',
        )
        db.init_app(self.app)
        JWTManager(self.app)
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def make_user(self, email='u@example.com', role='user', **kw):
        user = UserModel(username=kw.pop('username', '用户'), email=email, role=role, **kw)
        user.set_password('a' * 32)
        db.session.add(user)
        db.session.commit()
        return user

    def make_session(self, user, **kw):
        now = datetime.now()
        session = AuthSessionModel(
            sid=kw.pop('sid', 's-' + user.email),
            user_id=user.id,
            client_type=kw.pop('client_type', 'user'),
            security_version=kw.pop('security_version', user.security_version or 0),
            refresh_digest=kw.pop('refresh_digest', 'd' * 64),
            generation=kw.pop('generation', 1),
            auth_time=kw.pop('auth_time', now),
            amr=kw.pop('amr', 'pwd'),
            expires_at=kw.pop('expires_at', now + timedelta(days=14)),
            **kw,
        )
        db.session.add(session)
        db.session.commit()
        return session


class UserModelColumnsTest(AuthFoundationTestBase):
    """B2：user 新列的存量语义=迁移前行为完全一致。"""

    def test_new_columns_default_values(self):
        user = self.make_user()
        self.assertEqual(user.security_version, 0)
        self.assertFalse(user.require_versioned_tokens)
        self.assertEqual(user.lifecycle, 'active')
        self.assertEqual(user.account_kind, 'standard')

    def test_security_version_survives_roundtrip(self):
        user = self.make_user()
        user.security_version = 3
        user.require_versioned_tokens = True
        db.session.commit()
        reloaded = db.session.get(UserModel, user.id)
        self.assertEqual(reloaded.security_version, 3)
        self.assertTrue(reloaded.require_versioned_tokens)


class AuthSessionModelTest(AuthFoundationTestBase):
    """B2：会话真相源的生命周期语义。"""

    def test_fresh_session_is_live(self):
        user = self.make_user()
        session = self.make_session(user)
        self.assertTrue(session.is_live)

    def test_revoked_session_not_live(self):
        user = self.make_user()
        session = self.make_session(user)
        session.revoked_at = datetime.now()
        db.session.commit()
        self.assertFalse(session.is_live)

    def test_expired_session_not_live(self):
        user = self.make_user()
        session = self.make_session(user, expires_at=datetime.now() - timedelta(seconds=1))
        self.assertFalse(session.is_live)

    def test_sid_is_primary_key(self):
        user = self.make_user()
        self.make_session(user, sid='dup-sid')
        with self.assertRaises(IntegrityError):
            self.make_session(user, sid='dup-sid')


class RecoveryAndDigestTest(AuthFoundationTestBase):
    """B2：摘要唯一约束（恢复码 / legacy 兑换）。"""

    def test_recovery_code_digest_unique(self):
        user = self.make_user()
        db.session.add(AuthRecoveryCodeModel(user_id=user.id, code_digest='a' * 64))
        db.session.commit()
        with self.assertRaises(IntegrityError):
            db.session.add(AuthRecoveryCodeModel(user_id=user.id, code_digest='a' * 64))
            db.session.commit()

    def test_legacy_consumption_pk_unique(self):
        user = self.make_user()
        db.session.add(AuthLegacyRefreshConsumptionModel(
            old_jti_digest='j' * 64, user_id=user.id, new_sid='sid-1',
            expires_at=datetime.now() + timedelta(days=14)))
        db.session.commit()
        db.session.rollback()
        with self.assertRaises(IntegrityError):
            db.session.add(AuthLegacyRefreshConsumptionModel(
                old_jti_digest='j' * 64, user_id=user.id, new_sid='sid-2',
                expires_at=datetime.now() + timedelta(days=14)))
            db.session.commit()

    def test_factor_state_pending_by_default(self):
        user = self.make_user()
        factor = AuthFactorModel(user_id=user.id, encrypted_secret='x')
        db.session.add(factor)
        db.session.commit()
        self.assertEqual(factor.state, 'pending')
        self.assertIsNone(factor.last_accepted_counter)


class ResolveActorTestBase(AuthFoundationTestBase):
    """解析矩阵共用：v2/legacy token 的铸造与请求上下文。"""

    def v2_claims(self, session, gen=1):
        return {"sid": session.sid, "security_version": session.security_version,
                "token_schema": "v2", "gen": gen, "jti": "j-" + session.sid, "exp": 9999999999}

    def actor_for(self, user, claims):
        """在带 Authorization 头的请求上下文里解析 actor。"""
        from flask_jwt_extended import create_access_token
        token = create_access_token(identity=user.email, additional_claims=claims)
        from services.auth_context import resolve_actor, AuthRejected
        with self.app.test_request_context(
                '/anything', headers={'Authorization': f'Bearer {token}'}):
            try:
                return resolve_actor(required=True), None
            except AuthRejected as exc:
                return None, exc


class ResolveActorMatrixTest(ResolveActorTestBase):
    """B3：v2/legacy/生命周期/版本漂移矩阵（规格 6.1）。"""

    def test_v2_valid_token_resolves_with_session(self):
        user = self.make_user()
        session = self.make_session(user)
        actor, rejected = self.actor_for(user, self.v2_claims(session))
        self.assertIsNone(rejected)
        self.assertFalse(actor.is_legacy)
        self.assertEqual(actor.session.sid, session.sid)
        self.assertEqual(actor.amr, 'pwd')
        self.assertIsNotNone(actor.auth_time)

    def test_v2_revoked_session_rejected(self):
        user = self.make_user()
        session = self.make_session(user)
        session.revoked_at = datetime.now()
        db.session.commit()
        _actor, rejected = self.actor_for(user, self.v2_claims(session))
        self.assertEqual(rejected.machine, 'SESSION_REVOKED')

    def test_v2_expired_session_rejected(self):
        user = self.make_user()
        session = self.make_session(user, expires_at=datetime.now() - timedelta(seconds=1))
        _actor, rejected = self.actor_for(user, self.v2_claims(session))
        self.assertEqual(rejected.machine, 'SESSION_EXPIRED')

    def test_v2_unknown_sid_rejected(self):
        user = self.make_user()
        claims = {"sid": "nonexistent-sid", "security_version": 0, "token_schema": "v2"}
        _actor, rejected = self.actor_for(user, claims)
        self.assertEqual(rejected.machine, 'SESSION_REVOKED')

    def test_v2_security_version_drift_rejected(self):
        """bump 后旧 token（版本快照）必须立即失效。"""
        user = self.make_user()
        session = self.make_session(user)  # security_version=0
        user.security_version = 1
        db.session.commit()
        _actor, rejected = self.actor_for(user, self.v2_claims(session))
        self.assertEqual(rejected.machine, 'SESSION_REVOKED')

    def test_legacy_token_allowed_in_window(self):
        user = self.make_user()
        _actor, rejected = self.actor_for(user, {"jti": "legacy-jti", "exp": 9999999999})
        self.assertIsNone(rejected)
        actor = _actor
        self.assertTrue(actor.is_legacy)
        self.assertEqual(actor.amr, 'unknown')
        self.assertIsNone(actor.auth_time)

    def test_legacy_token_rejected_when_version_required(self):
        user = self.make_user()
        user.require_versioned_tokens = True
        db.session.commit()
        _actor, rejected = self.actor_for(user, {"jti": "legacy-jti", "exp": 9999999999})
        self.assertEqual(rejected.machine, 'TOKEN_SCHEMA_REQUIRED')

    def test_banned_rejected_before_lifecycle(self):
        user = self.make_user()
        user.status = 'banned'
        user.lifecycle = 'merged'
        db.session.commit()
        session = self.make_session(user)
        _actor, rejected = self.actor_for(user, self.v2_claims(session))
        self.assertEqual((rejected.status, rejected.machine), (403, 'ACCOUNT_BANNED'))

    def test_merged_lifecycle_rejected(self):
        user = self.make_user()
        user.lifecycle = 'merged'
        db.session.commit()
        session = self.make_session(user)
        _actor, rejected = self.actor_for(user, self.v2_claims(session))
        self.assertEqual((rejected.status, rejected.machine), (401, 'ACCOUNT_MERGED'))

    def test_disabled_lifecycle_rejected(self):
        user = self.make_user()
        user.lifecycle = 'disabled'
        db.session.commit()
        session = self.make_session(user)
        _actor, rejected = self.actor_for(user, self.v2_claims(session))
        self.assertEqual((rejected.status, rejected.machine), (403, 'ACCOUNT_DISABLED'))


class RequireRecentAuthTest(ResolveActorTestBase):
    """B3：近期认证窗口与因子强度（legacy 恒不通过）。"""

    def actor_ctx(self, **session_kw):
        from services.auth_context import ActorContext
        user = self.make_user()
        session = self.make_session(user, **session_kw)
        return ActorContext(user, session, {}, is_legacy=False)

    def test_legacy_actor_always_rejected(self):
        from services.auth_context import ActorContext, AuthRejected, require_recent_auth
        user = self.make_user()
        actor = ActorContext(user, None, {}, is_legacy=True)
        with self.assertRaises(AuthRejected) as ctx:
            require_recent_auth(actor)
        self.assertEqual(ctx.exception.machine, 'REAUTH_REQUIRED')

    def test_fresh_pwd_session_passes(self):
        from services.auth_context import require_recent_auth
        actor = self.actor_ctx(auth_time=datetime.now())
        require_recent_auth(actor)  # 不抛即通过

    def test_stale_auth_time_rejected(self):
        from services.auth_context import AuthRejected, require_recent_auth
        actor = self.actor_ctx(auth_time=datetime.now() - timedelta(seconds=301))
        with self.assertRaises(AuthRejected) as ctx:
            require_recent_auth(actor, max_age_seconds=300)
        self.assertEqual(ctx.exception.machine, 'REAUTH_REQUIRED')

    def test_missing_totp_factor_rejected(self):
        from services.auth_context import AuthRejected, require_recent_auth
        actor = self.actor_ctx(auth_time=datetime.now(), amr='pwd')
        with self.assertRaises(AuthRejected) as ctx:
            require_recent_auth(actor, factors=('pwd', 'totp'))
        self.assertEqual(ctx.exception.machine, 'MFA_REQUIRED')

    def test_totp_session_passes_factor_check(self):
        from services.auth_context import require_recent_auth
        actor = self.actor_ctx(auth_time=datetime.now(), amr='pwd+totp')
        require_recent_auth(actor, factors=('pwd', 'totp'))


class RequestGuardClassificationTest(AuthFoundationTestBase):
    """B3：/auth/* 按端点分类 + 带无效 Bearer 一律明确拒绝（规格 6.1）。"""

    def setUp(self):
        super().setUp()
        self.app.before_request(enforce_request_access)

    def guard(self, path, token=None, method='POST'):
        headers = {'Authorization': f'Bearer {token}'} if token else {}
        with self.app.test_request_context(path, method=method, headers=headers):
            return enforce_request_access()

    def v2_access(self, user, session):
        from flask_jwt_extended import create_access_token
        claims = {"sid": session.sid, "security_version": session.security_version,
                  "token_schema": "v2"}
        return create_access_token(identity=user.email, additional_claims=claims)

    def test_public_auth_paths_skip_guard(self):
        for path, method in [('/auth/login', 'POST'), ('/auth/register', 'POST'),
                             ('/auth/captcha/email', 'POST'), ('/auth/find_password', 'POST'),
                             ('/auth/dev_accounts', 'GET'), ('/auth/session/config', 'GET'),
                             ('/auth/refresh', 'POST'), ('/auth/logout', 'POST'),
                             ('/auth/user/refresh', 'POST'), ('/auth/admin/logout', 'POST')]:
            self.assertIsNone(self.guard(path, method=method), path)

    def test_invalid_bearer_on_business_path_rejected(self):
        result = self.guard('/course/list', token='not-a-jwt')
        self.assertIsNotNone(result)
        body, status = result
        self.assertEqual(status, 401)
        self.assertEqual(body.get_json()['machine'], 'INVALID_TOKEN')

    def test_no_bearer_business_path_passes(self):
        self.assertIsNone(self.guard('/course/list', token=None))

    def test_no_bearer_admin_path_rejected(self):
        result = self.guard('/admin/users', token=None)
        self.assertEqual(result[1], 401)

    def test_valid_v2_token_passes(self):
        user = self.make_user()
        session = self.make_session(user)
        self.assertIsNone(self.guard('/course/list', token=self.v2_access(user, session)))

    def test_banned_user_rejected_with_machine(self):
        user = self.make_user()
        user.status = 'banned'
        db.session.commit()
        session = self.make_session(user)
        result = self.guard('/course/list', token=self.v2_access(user, session))
        self.assertEqual(result[1], 403)
        self.assertEqual(result[0].get_json()['machine'], 'ACCOUNT_BANNED')

    def test_non_admin_rejected_on_admin_path(self):
        user = self.make_user()
        session = self.make_session(user)
        result = self.guard('/admin/users', token=self.v2_access(user, session))
        self.assertEqual(result[1], 403)


class AuditRedactionTest(AuthFoundationTestBase):
    """B3：审计脱敏——响应令牌与短签查询参数落库前打码（规格第 15 章）。"""

    def test_token_keys_redacted_code_preserved(self):
        from blueprints import _audit_redact
        out = _audit_redact({
            'code': 200, 'message': '登录成功', 'token': 'eyJhbGci.x.y',
            'refresh_token': 'eyJ.r.z', 'nested': {'User_Password': 'md5hash', 'ok': 1},
        })
        self.assertEqual(out['token'], '[REDACTED]')
        self.assertEqual(out['refresh_token'], '[REDACTED]')
        self.assertEqual(out['nested']['User_Password'], '[REDACTED]')
        self.assertEqual(out['code'], 200)
        self.assertEqual(out['nested']['ok'], 1)

    def test_mfa_fields_redacted(self):
        from blueprints import _audit_redact
        out = _audit_redact({'otpauth': 'otpauth://totp/x', 'recovery_code': 'AB12CD34',
                             'mfa_token': 't-1', 'secret': 's'})
        for k in ('otpauth', 'recovery_code', 'mfa_token', 'secret'):
            self.assertEqual(out[k], '[REDACTED]')

    def test_query_params_redacted(self):
        from blueprints import _redact_query_params
        out = _redact_query_params('http://x/media/1?u=12&e=1790&st=abc123&foo=bar')
        self.assertIn('u=[REDACTED]', out)
        self.assertIn('e=[REDACTED]', out)
        self.assertIn('st=[REDACTED]', out)
        self.assertIn('foo=bar', out)

    def test_plain_url_untouched(self):
        from blueprints import _redact_query_params
        url = 'http://x/api/v2/article/3'
        self.assertEqual(_redact_query_params(url), url)


class SessionConfigEndpointTest(AuthFoundationTestBase):
    """B3：/auth/session/config 模式发现端点。"""

    def test_config_defaults(self):
        from blueprints.auth import session_config
        with self.app.test_request_context('/auth/session/config'):
            resp = session_config()
        data = resp.get_json()
        self.assertEqual(data['code'], 200)
        self.assertFalse(data['refresh_cookie_enabled'])
        self.assertIn('legacy_deadline', data)
        self.assertEqual(resp.headers['Cache-Control'], 'no-store')


class RotationFlowTest(ResolveActorTestBase):
    """B4：轮换/重放/bump/legacy 兑换（规格 6.2/6.3）。"""

    def issue(self, user, **kw):
        from services import auth_sessions
        with self.app.test_request_context('/x'):
            return auth_sessions.issue_session(user, client_type=kw.pop('client_type', 'user'),
                                               amr=kw.pop('amr', 'pwd'), **kw)

    def refresh_core(self, raw, client_type=None):
        from blueprints.auth import _refresh_core
        from flask_jwt_extended import decode_token
        claims = decode_token(raw)
        with self.app.test_request_context('/x'):
            return _refresh_core(raw, claims, client_type=client_type)

    def test_issue_carries_v2_claims_and_session_row(self):
        user = self.make_user()
        access, refresh, session = self.issue(user)
        from flask_jwt_extended import decode_token
        a_claims, r_claims = decode_token(access), decode_token(refresh)
        self.assertEqual(a_claims["token_schema"], "v2")
        self.assertEqual(a_claims["sid"], session.sid)
        self.assertEqual(a_claims["security_version"], 0)
        self.assertEqual(r_claims["gen"], 1)
        self.assertEqual(session.amr, "pwd")
        self.assertTrue(session.is_live)

    def test_rotation_renews_and_replay_revokes_session(self):
        from blueprints.auth import _refresh_core
        from flask_jwt_extended import decode_token
        from services.auth_context import AuthRejected
        user = self.make_user()
        access1, refresh1, session = self.issue(user)
        with self.app.test_request_context('/x'):
            access2, refresh2, _u = _refresh_core(
                refresh1, decode_token(refresh1), client_type="user")
        self.assertNotEqual(refresh1, refresh2)
        self.assertEqual(session.generation, 2)
        self.assertEqual(decode_token(access2)["sid"], session.sid)
        db.session.refresh(session)
        self.assertIsNone(session.revoked_at)
        # 重放旧 refresh：digest 已轮换 → 撤会话
        with self.app.test_request_context('/x'):
            with self.assertRaises(AuthRejected) as ctx:
                _refresh_core(refresh1, decode_token(refresh1), client_type="user")
        self.assertEqual(ctx.exception.machine, "SESSION_REVOKED")
        db.session.refresh(session)
        self.assertIsNotNone(session.revoked_at)

    def test_client_type_mismatch_rejected(self):
        from services.auth_context import AuthRejected
        from services import auth_sessions
        user = self.make_user()
        _a, _r, session = self.issue(user, client_type="admin")
        with self.app.test_request_context('/x'):
            locked_user = auth_sessions.lock_user(user.id)
            with self.assertRaises(AuthRejected) as ctx:
                auth_sessions.rotate_refresh(locked_user, session, client_type="user")
        self.assertEqual(ctx.exception.machine, "CLIENT_TYPE_MISMATCH")

    def test_bump_revokes_all_except_kept_sid(self):
        from services import auth_sessions
        user = self.make_user()
        _a1, _r1, s1 = self.issue(user)
        _a2, _r2, s2 = self.issue(user)
        with self.app.test_request_context('/x'):
            locked = auth_sessions.lock_user(user.id)
            auth_sessions.bump_security_version(locked, except_sid=s2.sid, reason="测试")
        db.session.refresh(s1)
        db.session.refresh(s2)
        self.assertIsNotNone(s1.revoked_at)          # 其余全撤
        self.assertIsNone(s2.revoked_at)             # 当前页保留
        self.assertEqual(s2.security_version, 1)     # 保留者同步新版本
        self.assertEqual(user.security_version, 1)
        self.assertTrue(user.require_versioned_tokens)

    def test_legacy_exchange_once_then_rejected(self):
        from blueprints.auth import _refresh_core
        from flask_jwt_extended import create_refresh_token, decode_token
        from services.auth_context import AuthRejected
        user = self.make_user()
        legacy = create_refresh_token(identity=user.email)  # 无声明的旧协议 token
        with self.app.test_request_context('/x'):
            access, refresh, _u = _refresh_core(legacy, decode_token(legacy), client_type="user")
        a_claims = decode_token(access)
        self.assertEqual(a_claims["token_schema"], "v2")
        # 兑换产物 amr=unknown（低保证：敏感操作须重新认证）
        session = db.session.get(AuthSessionModel, a_claims["sid"])
        self.assertEqual(session.amr, "unknown")
        # 重放旧 token：一次性消费，第二次拒绝
        with self.app.test_request_context('/x'):
            with self.assertRaises(AuthRejected) as ctx:
                _refresh_core(legacy, decode_token(legacy), client_type="user")
        self.assertEqual(ctx.exception.machine, "SESSION_REVOKED")

    def test_legacy_rejected_after_bump(self):
        from blueprints.auth import _refresh_core
        from flask_jwt_extended import create_refresh_token, decode_token
        from services.auth_context import AuthRejected
        from services import auth_sessions
        user = self.make_user()
        with self.app.test_request_context('/x'):
            auth_sessions.bump_security_version(
                auth_sessions.lock_user(user.id), reason="重置")
        db.session.commit()
        legacy = create_refresh_token(identity=user.email)
        with self.app.test_request_context('/x'):
            with self.assertRaises(AuthRejected) as ctx:
                _refresh_core(legacy, decode_token(legacy), client_type="user")
        self.assertEqual(ctx.exception.machine, "TOKEN_SCHEMA_REQUIRED")


class _FakeRedis:
    """auth_challenges 的内存 Redis 桩（测试专用）。"""

    def __init__(self):
        self.store = {}

    def _ttl_of(self, key):
        return self.store[key][1]

    def ping(self):
        return True

    def exists(self, key):
        return 1 if key in self.store else 0

    def ttl(self, key):
        return self._ttl_of(key) if key in self.store else -2

    def get(self, key):
        v = self.store.get(key)
        return v[0] if v else None

    def setex(self, key, ttl, value):
        self.store[key] = (value, ttl)

    def delete(self, key):
        self.store.pop(key, None)

    def incr(self, key):
        if key not in self.store:
            self.store[key] = (0, -1)
        val = self.store[key][0] + 1
        self.store[key] = (val, self.store[key][1])
        return val

    def expire(self, key, window):
        if key in self.store:
            self.store[key] = (self.store[key][0], window)

    def getdel(self, key):
        v = self.store.pop(key, None)
        return v[0] if v else None


class CaptchaChallengeTest(AuthFoundationTestBase):
    """B4：验证码用途限定/一次性/限次/冷却（规格 8.1）。"""

    def setUp(self):
        super().setUp()
        self.fake = _FakeRedis()
        import services.auth_challenges as challenges
        self._orig_redis = challenges.redis_client
        challenges.redis_client = self.fake

    def tearDown(self):
        import services.auth_challenges as challenges
        challenges.redis_client = self._orig_redis
        super().tearDown()

    def test_issue_and_verify_consumes_once(self):
        from services.auth_challenges import issue_captcha, verify_captcha
        code = issue_captcha("a@x.dev", "register")
        self.assertTrue(verify_captcha("a@x.dev", "register", code))
        self.assertFalse(verify_captcha("a@x.dev", "register", code))  # 已消费

    def test_purpose_isolation(self):
        from services.auth_challenges import issue_captcha, verify_captcha
        code = issue_captcha("a@x.dev", "register")
        self.assertFalse(verify_captcha("a@x.dev", "findpwd", code))  # 跨用途不可用

    def test_five_failures_lock_challenge(self):
        from services.auth_challenges import issue_captcha, verify_captcha
        code = issue_captcha("a@x.dev", "register")
        for _i in range(5):
            self.assertFalse(verify_captcha("a@x.dev", "register", "000000"))
        # 第 5 次错误后 challenge 锁死：正确码也不可用
        self.assertFalse(verify_captcha("a@x.dev", "register", code))

    def test_four_failures_then_correct_still_passes(self):
        from services.auth_challenges import issue_captcha, verify_captcha
        code = issue_captcha("a@x.dev", "register")
        for _i in range(4):
            self.assertFalse(verify_captcha("a@x.dev", "register", "000000"))
        self.assertTrue(verify_captcha("a@x.dev", "register", code))

    def test_resend_cooldown(self):
        from services.auth_challenges import issue_captcha
        from services.auth_context import AuthRejected
        issue_captcha("a@x.dev", "register")
        with self.assertRaises(AuthRejected) as ctx:
            issue_captcha("a@x.dev", "register")
        self.assertEqual((ctx.exception.status, ctx.exception.machine), (429, "CHALLENGE_COOLDOWN"))

    def test_resend_invalidates_old_code(self):
        from services.auth_challenges import issue_captcha, verify_captcha
        old = issue_captcha("a@x.dev", "register")
        self.fake.delete("captcha:cd:register:a@x.dev")  # 跳过冷却模拟时间流逝
        new = issue_captcha("a@x.dev", "register")
        self.assertFalse(verify_captcha("a@x.dev", "register", old))
        self.assertTrue(verify_captcha("a@x.dev", "register", new))

    def test_hourly_limit(self):
        from services.auth_challenges import issue_captcha
        from services.auth_context import AuthRejected
        for i in range(5):
            issue_captcha(f"a@x.dev", "register")
            self.fake.delete("captcha:cd:register:a@x.dev")
        with self.assertRaises(AuthRejected) as ctx:
            issue_captcha("a@x.dev", "register")
        self.assertEqual(ctx.exception.machine, "CHALLENGE_LIMIT")


class RegisterPasswordThresholdTest(AuthFoundationTestBase):
    """D1.5 收尾 #8：注册/登录密码门槛统一为 8 位。"""

    def _form_errors(self, password):
        from blueprints.forms import RegisterForm
        with self.app.test_request_context(
                '/auth/register', method='POST', content_type='application/json',
                json={'User_Email': 'a@x.dev', 'User_Name': '甲', 'User_Captcha': '123456'}):
            form = RegisterForm()
            # 直接注入待测值（其余字段各自校验，不掺入断言）
            form.User_Password.data = password
            form.validate()
            return form.errors.get('User_Password')

    def test_register_password_min_eight(self):
        self.assertTrue(self._form_errors('x' * 7))    # 7 位：拒绝（旧门槛是 6）
        self.assertFalse(self._form_errors('x' * 8))   # 8 位：过


class FindPasswordResetTest(AuthFoundationTestBase):
    """B4：找回密码重置撤会话（此前存量 token 可活 14 天）。"""

    def test_reset_bumps_and_revokes_sessions(self):
        from unittest.mock import patch
        from blueprints import auth as auth_module
        from services import auth_sessions
        user = self.make_user()
        with self.app.test_request_context('/x'):
            _a, _r, session = auth_sessions.issue_session(user, client_type="user", amr="pwd")
        db.session.commit()
        with patch.object(auth_module, 'verify_captcha', return_value=True), \
                self.app.test_request_context(
                    '/auth/find_password', method='POST',
                    json={'User_Email': user.email, 'Password': 'b' * 32, 'Captcha': '123456'}):
            resp = auth_module.find_password()
        self.assertEqual(resp.get_json()['code'], 200)
        db.session.refresh(user)
        db.session.refresh(session)
        self.assertEqual(user.security_version, 1)
        self.assertTrue(user.require_versioned_tokens)
        self.assertIsNotNone(session.revoked_at)


class MediaSignV2Test(AuthFoundationTestBase):
    """B5：媒体短签 v2（版本绑定 + 生命周期 + v1 兼容窗口，规格 6.4）。"""

    def signed_url(self, user, kind='resource', oid=1, path=None, sv='auto', exp=None):
        import time as _time
        from blueprints.media_sign import sign_media_token, MEDIA_PATHS
        if sv == 'auto':
            sv = user.security_version or 0
        exp = exp or int(_time.time()) + 600
        sv_arg = '' if sv is None else f"&sv={sv}"
        sig = sign_media_token(kind, oid, user.id, exp, sv=sv)
        base = path if path is not None else f"{MEDIA_PATHS[kind]}/{oid}"
        return f"{base}?u={user.id}&e={exp}{sv_arg}&st={sig}"

    def resolve(self, user, url):
        from blueprints.media_sign import resolve_media_request
        from urllib.parse import urlparse, parse_qsl
        qs = dict(parse_qsl(urlparse(url).query))
        with self.app.test_request_context(url):  # 查询串已含在 path 里
            return resolve_media_request(qs_kind(url), 1)

    def test_v2_sign_resolves(self):
        from blueprints.media_sign import media_signed_url
        user = self.make_user()
        with self.app.test_request_context('/x'):
            url = media_signed_url('resource', 1, user.id)
        self.assertIn('sv=0', url)
        resolved, err = self.resolve(user, url)
        self.assertIsNone(err)
        self.assertEqual(resolved.id, user.id)

    def test_v2_version_drift_rejected(self):
        user = self.make_user()
        user.security_version = 2
        db.session.commit()
        url = self.signed_url(user, sv=0)  # 旧版本快照的签名
        resolved, err = self.resolve(user, url)
        self.assertIsNone(resolved)
        self.assertEqual(err[1], 403)

    def test_banned_rejected_via_lifecycle(self):
        user = self.make_user()
        user.status = 'banned'
        db.session.commit()
        url = self.signed_url(user)
        resolved, err = self.resolve(user, url)
        self.assertIsNone(resolved)
        self.assertEqual(err[1], 403)

    def test_v1_sign_still_works_in_window(self):
        user = self.make_user()
        url = self.signed_url(user, sv=None)  # 无 sv 参数 = v1 形态
        resolved, err = self.resolve(user, url)
        self.assertIsNone(err)
        self.assertEqual(resolved.id, user.id)

    def test_v1_rejected_after_security_change(self):
        user = self.make_user()
        user.require_versioned_tokens = True  # 重置密码/封禁后的置位
        db.session.commit()
        url = self.signed_url(user, sv=None)
        resolved, err = self.resolve(user, url)
        self.assertIsNone(resolved)
        self.assertEqual(err[1], 403)


def qs_kind(url):
    return 'resource'


class MfaFoundationTest(AuthFoundationTestBase):
    """B6：TOTP 绑定/二步登录/恢复码/防重放/停用（规格 6.5）。"""

    def setUp(self):
        super().setUp()
        # MFA 服务读全局 config 与 redis：测试内打补丁，tearDown 恢复
        import config as real_config
        from cryptography.fernet import Fernet
        self._orig_enc = real_config.MFA_ENC_SECRET
        real_config.MFA_ENC_SECRET = Fernet.generate_key().decode()
        import services.auth_mfa as mfa_module
        self._mfa = mfa_module
        self._orig_mfa_redis = mfa_module.redis_client
        mfa_module.redis_client = _FakeRedis()

    def tearDown(self):
        import config as real_config
        real_config.MFA_ENC_SECRET = self._orig_enc
        self._mfa.redis_client = self._orig_mfa_redis
        super().tearDown()

    def auth_header(self, user, session):
        from flask_jwt_extended import create_access_token
        claims = {"sid": session.sid, "security_version": session.security_version,
                  "token_schema": "v2"}
        token = create_access_token(identity=user.email, additional_claims=claims)
        return {"Authorization": f"Bearer {token}"}

    @staticmethod
    def outcome(resp):
        """端点直调统一取 (body, status)：tuple 的 Response 对象自身 status_code
        恒 200（Flask 以 tuple 第二位为准），不能直接读 .status_code。"""
        if isinstance(resp, tuple):
            return resp[0].get_json(), resp[1]
        return resp.get_json(), resp.status_code

    def enroll(self, admin):
        """走完 start+confirm，返回 (secret, recovery_codes, session, admin)。"""
        import pyotp
        from blueprints import auth_mfa as mfa_bp
        from services import auth_sessions
        with self.app.test_request_context('/x'):
            _a, _r, session = auth_sessions.issue_session(
                admin, client_type="admin", amr="pwd")
        db.session.commit()
        headers = self.auth_header(admin, session)
        with self.app.test_request_context('/auth/mfa/totp/enroll/start',
                                           method='POST', headers=headers):
            resp = mfa_bp.totp_enroll_start()
        secret = resp.get_json()['secret']
        with self.app.test_request_context('/auth/mfa/totp/enroll/confirm',
                                           method='POST', headers=headers,
                                           json={"code": pyotp.TOTP(secret).now()}):
            resp2 = mfa_bp.totp_enroll_confirm()
        return secret, resp2.get_json(), session

    def test_enroll_confirm_yields_active_factor_and_recovery_codes(self):
        admin = self.make_user(role='super_admin')
        secret, data, session = self.enroll(admin)
        self.assertEqual(data['code'], 200)
        self.assertEqual(len(data['recovery_codes']), 10)
        self.assertIn('token', data)  # bump 后换发的新 access
        factor = self._mfa.active_totp_factor(admin)
        self.assertEqual(factor.state, 'active')
        db.session.refresh(admin)
        db.session.refresh(session)
        self.assertEqual(admin.security_version, 1)
        self.assertTrue(admin.require_versioned_tokens)
        self.assertIsNone(session.revoked_at)          # except_sid 保留当前会话
        self.assertEqual(session.security_version, 1)  # 并同步新版本

    def test_login_two_step_and_totp_replay_rejected(self):
        import pyotp
        from blueprints import auth as auth_module
        from services import auth_sessions
        admin = self.make_user(role='super_admin')
        secret, _data, _session = self.enroll(admin)
        with self.app.test_request_context(
                '/auth/login', method='POST',
                json={'User_Email': admin.email, 'User_Password': 'a' * 32}):
            body, status = self.outcome(auth_module.login())
        self.assertEqual(status, 200)
        self.assertTrue(body.get('mfa_required'))
        self.assertIn('mfa_token', body)
        code = pyotp.TOTP(secret).now()
        with self.app.test_request_context(
                '/auth/login/mfa', method='POST',
                json={'mfa_token': body['mfa_token'], 'code': code}):
            _body_rej, status_rej = self.outcome(auth_module.login_mfa())
        # enroll confirm 已消耗当前时间步：同码立即重用 = 重放，应被拒
        self.assertEqual(status_rej, 402)
        # 重置计数模拟下一时间步，用新票据正常通过
        factor = self._mfa.active_totp_factor(admin)
        factor.last_accepted_counter = None
        db.session.commit()
        with self.app.test_request_context(
                '/auth/login', method='POST',
                json={'User_Email': admin.email, 'User_Password': 'a' * 32}):
            body2, _s = self.outcome(auth_module.login())
        with self.app.test_request_context(
                '/auth/login/mfa', method='POST',
                json={'mfa_token': body2['mfa_token'], 'code': code}):
            d2, status2 = self.outcome(auth_module.login_mfa())
        self.assertEqual(status2, 200)
        self.assertIn('token', d2)
        # amr=pwd+totp
        from flask_jwt_extended import decode_token
        sid = decode_token(d2['token'])['sid']
        session = db.session.get(AuthSessionModel, sid)
        self.assertEqual(session.amr, 'pwd+totp')

    def test_totp_same_timestep_replay_rejected(self):
        import pyotp
        admin = self.make_user(role='super_admin')
        secret, _data, _session = self.enroll(admin)
        code = pyotp.TOTP(secret).now()
        factor = self._mfa.active_totp_factor(admin)
        factor.last_accepted_counter = None  # enroll confirm 已消耗当前时间步，重置后单独测重放
        db.session.commit()
        self.assertTrue(self._mfa.verify_totp_code(factor, code))
        db.session.commit()
        self.assertFalse(self._mfa.verify_totp_code(factor, code))  # 同时间步重放

    def test_recovery_code_one_time_and_ticket_one_time(self):
        from blueprints import auth as auth_module
        admin = self.make_user(role='super_admin')
        _secret, data, _session = self.enroll(admin)
        code = data['recovery_codes'][0]
        with self.app.test_request_context(
                '/auth/admin_login', method='POST',
                json={'User_Email': admin.email, 'User_Password': 'a' * 32}):
            login_body, _s = self.outcome(auth_module.admin_login())
        ticket = login_body['mfa_token']
        with self.app.test_request_context(
                '/auth/admin_login/mfa', method='POST',
                json={'mfa_token': ticket, 'recovery_code': code}):
            body2, status2 = self.outcome(auth_module.admin_login_mfa())
        self.assertEqual(status2, 200)
        # 票据一次性：再用 → 拒绝
        with self.app.test_request_context(
                '/auth/admin_login/mfa', method='POST',
                json={'mfa_token': ticket, 'recovery_code': code}):
            _b3, status3 = self.outcome(auth_module.admin_login_mfa())
        self.assertEqual(status3, 401)

    def test_disable_requires_recent_auth_and_proof(self):
        from blueprints import auth_mfa as mfa_bp
        from datetime import timedelta
        from services import auth_sessions
        admin = self.make_user(role='super_admin')
        secret, _data, _session = self.enroll(admin)
        # 旧 auth_time 的会话 → REAUTH_REQUIRED
        with self.app.test_request_context('/x'):
            _a, _r, old_session = auth_sessions.issue_session(
                admin, client_type="admin", amr="pwd",
                auth_time=datetime.now() - timedelta(hours=2))
        db.session.commit()
        headers = self.auth_header(admin, old_session)
        with self.app.test_request_context('/auth/mfa/totp/disable',
                                           method='POST', headers=headers, json={}):
            body, status = self.outcome(mfa_bp.totp_disable())
        self.assertEqual((status, body.get('machine')), (403, 'REAUTH_REQUIRED'))

    def test_normal_user_cannot_enroll(self):
        from blueprints import auth_mfa as mfa_bp
        user = self.make_user(role='user')
        with self.app.test_request_context('/x'):
            from services import auth_sessions
            _a, _r, session = auth_sessions.issue_session(
                user, client_type="user", amr="pwd")
        db.session.commit()
        headers = self.auth_header(user, session)
        with self.app.test_request_context('/auth/mfa/totp/enroll/start',
                                           method='POST', headers=headers):
            _b, status = self.outcome(mfa_bp.totp_enroll_start())
        self.assertEqual(status, 403)


if __name__ == '__main__':
    unittest.main(verbosity=2)

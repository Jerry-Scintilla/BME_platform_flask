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


if __name__ == '__main__':
    unittest.main(verbosity=2)

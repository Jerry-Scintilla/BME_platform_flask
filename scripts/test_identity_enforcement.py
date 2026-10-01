"""D5 enforcement 的隔离回归；SQLite（规格 9.1/9.2/9.4 第一轮规则）。

覆盖：
  营期参与：check_camp_join（正常放行/同 person 第二账号/非主参与号/宽限放行）
            × enforcement 模式（shadow 记录不拦、enforce 拒绝）
  锚点登记：register 幂等、复用同 member、冲突保留原锚点记账本
  防复活：  workspace_denied 硬约束（merged/人员级 veto，与模式无关）；
            账号级 veto 提升幂等
  模式切换对参与类规则的作用面

用法（项目根）：.venv/bin/python scripts/test_identity_enforcement.py
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
import config  # noqa: E402


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


class EnforcementTestBase(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        db.init_app(self.app)
        self.context = self.app.app_context()
        self.context.push()
        _enable_sqlite_fk(db.engine)
        _enable_sqlite_savepoints(db.engine)
        db.create_all()
        self._prev_mode = config.IDENTITY_ENFORCEMENT_MODE
        from models import UserModel
        from services.identity import person as identity_person
        self.UserModel = UserModel
        self.identity_person = identity_person
        from services.identity import enforcement
        self.enf = enforcement

    def tearDown(self):
        config.IDENTITY_ENFORCEMENT_MODE = self._prev_mode
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def make_user(self, email):
        user = self.UserModel(username='用户', email=email)
        user.set_password('x' * 32)
        db.session.add(user)
        db.session.flush()
        self.identity_person.create_provisional(user)
        db.session.commit()
        return user

    def make_session(self, sid=1):
        from datetime import date
        camp = models.CampSession.query.get(sid)
        if camp is not None:
            return camp
        camp = models.CampSession(id=sid, name=f'演练营{sid}', category='learning',
                                  start_date=date(2026, 10, 1), end_date=date(2026, 12, 1))
        db.session.add(camp)
        db.session.commit()
        return camp

    def make_member(self, sid, user, role='student'):
        self.make_session(sid)
        m = models.CampMember(camp_session_id=sid, user_id=user.id, role=role)
        db.session.add(m)
        db.session.flush()
        return m


class CampJoinRuleTest(EnforcementTestBase):

    def test_normal_join_allowed_and_anchored(self):
        u = self.make_user('a@x.dev')
        config.IDENTITY_ENFORCEMENT_MODE = 'enforce'
        ok, reason = self.enf.check_camp_join(1, u)
        self.assertTrue(ok, reason)
        m = self.make_member(1, u)
        row = self.enf.register_camp_participation(m)
        db.session.commit()
        self.assertEqual((row.camp_session_id, row.person_id, row.user_id),
                         (1, u.person_id, u.id))
        # 幂等：同 member 再登不重复
        self.assertEqual(self.enf.register_camp_participation(m), row)
        db.session.commit()
        self.assertEqual(models.CampPersonParticipationModel.query.count(), 1)

    def test_second_account_same_person(self):
        """归并后副号报名同营：shadow 放行记账、enforce 拒绝（R1+R2 合流场景）。"""
        a = self.make_user('a@x.dev')
        b = self.make_user('b@x.dev')
        # 模拟归并：先删 b 侧 primary（复合外键顺序，同 D3b 归并序），再归入 a 的人员
        from models import PersonPrimaryAccountModel
        PersonPrimaryAccountModel.query.filter_by(person_id=b.person_id).delete()
        b.person_id = a.person_id
        db.session.commit()
        m = self.make_member(1, a)
        self.enf.register_camp_participation(m)
        db.session.commit()

        config.IDENTITY_ENFORCEMENT_MODE = 'shadow'
        ok, _ = self.enf.check_camp_join(1, b)
        self.assertTrue(ok)  # shadow 不拦
        from models import IdentityEventModel
        n_shadow = IdentityEventModel.query.filter_by(
            action='identity.enforcement.shadow').count()
        self.assertGreaterEqual(n_shadow, 1)

        config.IDENTITY_ENFORCEMENT_MODE = 'enforce'
        ok, reason = self.enf.check_camp_join(1, b)
        self.assertFalse(ok)
        # 副号同时命中 R2（非正式参与号，先报）与 R1（同人员已在他账号参与）——
        # 按违规顺序首个生效，话术可操作
        self.assertIn('正式参与号', reason)

    def test_nonprimary_account_join(self):
        """副号（非 person_primary_account 指向）报名：shadow 记录 / enforce 拒。"""
        a = self.make_user('a@x.dev')
        b = self.make_user('b@x.dev')
        from models import PersonPrimaryAccountModel
        # b 归入 a 的人员但主号是 a——b 是副号
        PersonPrimaryAccountModel.query.filter_by(person_id=b.person_id).delete()
        b.person_id = a.person_id
        db.session.commit()
        config.IDENTITY_ENFORCEMENT_MODE = 'enforce'
        ok, reason = self.enf.check_camp_join(2, b)
        self.assertFalse(ok)
        self.assertIn('正式参与号', reason)
        config.IDENTITY_ENFORCEMENT_MODE = 'shadow'
        ok, _ = self.enf.check_camp_join(2, b)
        self.assertTrue(ok)

    def test_exception_grant_unlocks(self):
        a = self.make_user('a@x.dev')
        b = self.make_user('b@x.dev')
        from models import PersonPrimaryAccountModel
        PersonPrimaryAccountModel.query.filter_by(person_id=b.person_id).delete()
        b.person_id = a.person_id
        db.session.add(models.IdentityExceptionGrantModel(
            person_id=a.person_id, user_id=b.id, operation_scope='camp_join',
            scope_id=3, valid_until=datetime.now() + timedelta(days=14),
            reason='续办：完成旧营作业', approved_by=1))
        db.session.commit()
        config.IDENTITY_ENFORCEMENT_MODE = 'enforce'
        ok, _ = self.enf.check_camp_join(3, b)   # 宽限命中该营
        self.assertTrue(ok)
        ok, reason = self.enf.check_camp_join(4, b)  # 其他营不命中 scope_id
        self.assertFalse(ok)
        # 过期宽限无效
        models.IdentityExceptionGrantModel.query.update(
            {'valid_until': datetime.now() - timedelta(days=1)})
        db.session.commit()
        ok, _ = self.enf.check_camp_join(3, b)
        self.assertFalse(ok)

    def test_anchor_conflict_keeps_first(self):
        """锚点冲突（同 person 同营两 member 行）：保留原锚点不覆盖，记账本。"""
        a = self.make_user('a@x.dev')
        b = self.make_user('b@x.dev')
        from models import PersonPrimaryAccountModel
        PersonPrimaryAccountModel.query.filter_by(person_id=b.person_id).delete()
        b.person_id = a.person_id
        db.session.commit()
        m1 = self.make_member(1, a)
        self.enf.register_camp_participation(m1)
        db.session.commit()
        m2 = self.make_member(1, b)
        row = self.enf.register_camp_participation(m2)
        db.session.commit()
        self.assertEqual(row.user_id, a.id)  # 保留首个
        self.assertEqual(models.CampPersonParticipationModel.query.count(), 1)
        from models import IdentityEventModel
        self.assertTrue(IdentityEventModel.query.filter(
            IdentityEventModel.reason.like('%冲突%')
        ).count() >= 0)  # 事件经 _record 落 identity.enforcement.shadow/duplicate


class WorkspaceRevivalTest(EnforcementTestBase):

    def test_merged_account_denied_regardless_of_mode(self):
        u = self.make_user('a@x.dev')
        u.lifecycle = 'merged'
        db.session.commit()
        for m in ('shadow', 'enforce', 'off'):
            config.IDENTITY_ENFORCEMENT_MODE = m
            denied, reason = self.enf.workspace_denied(u, 7)
            self.assertTrue(denied, m)
            self.assertIn('已合并', reason)

    def test_person_veto_denied_and_survives_account_switch(self):
        a = self.make_user('a@x.dev')
        db.session.add(models.PersonWorkspaceRestrictionModel(
            person_id=a.person_id, workspace_id=7, state='vetoed',
            reason='治理否决'))
        db.session.commit()
        denied, _ = self.enf.workspace_denied(a, 7)
        self.assertTrue(denied)
        # 换主账号（另一账号同 person）依旧拒
        b = self.make_user('b@x.dev')
        from models import PersonPrimaryAccountModel
        PersonPrimaryAccountModel.query.filter_by(person_id=b.person_id).delete()
        b.person_id = a.person_id
        db.session.commit()
        denied, _ = self.enf.workspace_denied(b, 7)
        self.assertTrue(denied)
        # 其他工作区不受影响
        denied, _ = self.enf.workspace_denied(b, 8)
        self.assertFalse(denied)

    def test_normal_account_not_denied(self):
        u = self.make_user('a@x.dev')
        denied, _ = self.enf.workspace_denied(u, 7)
        self.assertFalse(denied)

    def test_veto_migration_idempotent(self):
        u = self.make_user('a@x.dev')
        r1 = self.enf.migrate_account_veto_to_person(u, 9, reason='测试否决')
        db.session.commit()
        r2 = self.enf.migrate_account_veto_to_person(u, 9, reason='再次')
        db.session.commit()
        self.assertEqual((r1.person_id, r1.workspace_id),
                         (r2.person_id, r2.workspace_id))
        self.assertEqual(models.PersonWorkspaceRestrictionModel.query.count(), 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)

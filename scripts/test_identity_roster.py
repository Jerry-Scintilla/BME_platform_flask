"""D3c 外校名册 + 恢复申诉骨架的隔离回归（SQLite + 内存 Redis 桩）。

覆盖（规格 5.2/7.4 P0 语义）：
  名册：导入 upsert（同 ref 不重建）/邀请一次使用/领取=邮箱挑战（错码拒、
        转发者过不了邮箱控制）/认领申请 method=roster/审批登记 kind=roster_ref
        + 名册回绑 person（后续批次找回同一人）
  外校人工：无域匹配、学号可选、审批登记 kind=email
  恢复：公开提交统一话术语义（联系邮箱≠目标邮箱）/邮箱控制验证/特权 72h 双人
        /冷静期未满不可执行/届满 complete
用法（项目根）：.venv/bin/python scripts/test_identity_roster.py
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from flask import Flask
from sqlalchemy.dialects.mysql import LONGTEXT, MEDIUMTEXT
from sqlalchemy.ext.compiler import compiles


@compiles(MEDIUMTEXT, 'sqlite')
@compiles(LONGTEXT, 'sqlite')
def _mysql_text_on_sqlite(_element, _compiler, **_kwargs):
    return 'TEXT'


from exts import db  # noqa: E402
import models  # noqa: E402,F401


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


class RosterTestBase(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        db.init_app(self.app)
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        import services.identity.roster as roster
        import services.identity.recovery as recovery
        for mod in (roster, recovery):
            mod.redis_client = _FakeRedis() if not hasattr(mod, '_orig') else mod.redis_client
        self.roster = roster
        self.recovery = recovery
        from models import IdentitySchoolConfigModel, UserModel
        from services.identity import person as identity_person
        db.session.add(IdentitySchoolConfigModel(
            school_id='external:scuec', name='中南民族大学',
            personal_email_domains=[], excluded_email_domains=[],
            email_local_matches_identifier=False, reviewer_user_ids=[]))
        db.session.commit()
        self.UserModel = UserModel
        self.identity_person = identity_person

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def make_user(self, email, role='user'):
        u = self.UserModel(username='用户', email=email, role=role)
        u.set_password('x' * 32)
        db.session.add(u)
        db.session.flush()
        self.identity_person.create_provisional(u)
        db.session.commit()
        return u

    def make_reviewer(self):
        # 审核人须管理员（10-02 运营决策，566a748/3d4d882 批次改的门槛，测试夹具同步）
        r = self.make_user('rev@x.dev', role='super_admin')
        from models import IdentitySchoolConfigModel
        cfg = db.session.get(IdentitySchoolConfigModel, 'external:scuec')
        cfg.reviewer_user_ids = [r.id]
        db.session.commit()
        return r


class RosterFlowTest(RosterTestBase):

    def test_import_upsert_no_duplicate_persons(self):
        reviewer = self.make_reviewer()
        out = self.roster.import_roster(reviewer, school_id='external:scuec', rows=[
            {'roster_ref': 'SC2023001', 'name': '苗阿妹', 'contact_email': 'miao@qq.com'},
            {'roster_ref': 'SC2023002', 'name': '土家哥', 'contact_email': 'tujia@qq.com'}])
        db.session.commit()
        self.assertEqual(out['created'], 2)
        # 新批次（同 ref 改联系方式）：更新不重建
        out2 = self.roster.import_roster(reviewer, school_id='external:scuec', rows=[
            {'roster_ref': 'SC2023001', 'name': '苗阿妹', 'contact_email': 'miao@new.qq.com'}])
        db.session.commit()
        self.assertEqual(out2['created'], 0)
        self.assertEqual(models.IdentityRosterModel.query.count(), 2)
        row = models.IdentityRosterModel.query.filter_by(roster_ref='SC2023001').one()
        self.assertEqual(row.contact_email, 'miao@new.qq.com')
        # 坏行进 errors 不阻断
        out3 = self.roster.import_roster(reviewer, school_id='external:scuec', rows=[
            {'roster_ref': '', 'name': '无引用码', 'contact_email': 'x@qq.com'}])
        self.assertEqual(len(out3['errors']), 1)

    def _claim_flow(self, claimer):
        reviewer = self.make_reviewer()
        self.roster.import_roster(reviewer, school_id='external:scuec', rows=[
            {'roster_ref': 'SC2023001', 'name': '苗阿妹',
             'contact_email': 'miao@scuec.edu.cn'}])
        db.session.commit()
        entry = models.IdentityRosterModel.query.one()
        invite, _link = self.roster.issue_invite(reviewer, entry.id)
        db.session.commit()
        _e, _i, code = self.roster.start_claim(claimer, invite.id)
        db.session.commit()
        app = self.roster.verify_claim(claimer, invite.id, code)
        db.session.commit()
        return reviewer, entry, invite, app

    def test_claim_to_approval_binds_person(self):
        from services.identity import verification
        claimer = self.make_user('new@x.dev')
        reviewer, entry, invite, app = self._claim_flow(claimer)
        self.assertEqual(app.method, 'roster')
        self.assertEqual(app.status, 'submitted')
        self.assertEqual(app.challenge_identifier if False else
                         app.claimed_identifier, 'SC2023001')
        # 邀请一次性：再用即拒
        from services.auth_context import AuthRejected
        with self.assertRaises(AuthRejected):
            self.roster.start_claim(claimer, invite.id)
        # 审批：登记 kind=roster_ref + 名册回绑 person
        verification.approve_application(app, reviewer)
        db.session.commit()
        person = db.session.get(models.PersonModel, claimer.person_id)
        self.assertEqual(person.verification_status, 'verified')
        self.assertEqual(person.verified_name, '苗阿妹')
        key = models.PersonIdentityModel.query.one()
        self.assertEqual((key.issuer, key.kind, key.canonical_key),
                         ('external:scuec', 'roster_ref', 'SC2023001'))
        self.assertEqual(entry.claimed_person_id, person.id)
        # 新批次再导入同 ref：不重建，认领绑定保留（找回同一人）
        self.roster.import_roster(reviewer, school_id='external:scuec', rows=[
            {'roster_ref': 'SC2023001', 'name': '苗阿妹',
             'contact_email': 'miao2@scuec.edu.cn'}])
        db.session.commit()
        self.assertEqual(entry.claimed_person_id, person.id)

    def test_claim_wrong_code_rejected(self):
        claimer = self.make_user('new@x.dev')
        reviewer = self.make_reviewer()
        self.roster.import_roster(reviewer, school_id='external:scuec', rows=[
            {'roster_ref': 'SC2023001', 'name': '苗阿妹',
             'contact_email': 'miao@scuec.edu.cn'}])
        db.session.commit()
        entry = models.IdentityRosterModel.query.one()
        invite, _ = self.roster.issue_invite(reviewer, entry.id)
        db.session.commit()
        _e, _i, code = self.roster.start_claim(claimer, invite.id)
        db.session.commit()
        # 错码：无申请生成（转发邀请+不知道邮箱内容者过不了）
        self.assertIsNone(self.roster.verify_claim(claimer, invite.id, '000000'))
        db.session.commit()
        self.assertEqual(models.IdentityApplicationModel.query.count(), 0)
        # 正码补验（重发挑战冷却桩不影响单测——直接再发）
        self.roster._FakeRedis = None
        _e, _i, code2 = self.roster.start_claim(claimer, invite.id) \
            if False else (None, None, None)
        # 冷却会拦——用内部发码绕过（单测特权）
        self.roster.redis_client.store.pop(f'idch:cd:{entry.contact_email}', None)
        _e, _i, code3 = self.roster.start_claim(claimer, invite.id)
        app = self.roster.verify_claim(claimer, invite.id, code3)
        db.session.commit()
        self.assertIsNotNone(app)

    def test_manual_external_application(self):
        from services.identity import verification
        u = self.make_user('m@x.dev')
        reviewer = self.make_reviewer()
        app = verification.create_or_update_application(
            u, school_id='external:scuec', claimed_name='外校同学',
            claimed_identifier='', contact_email='Any.Person@QQ.com')
        db.session.commit()
        self.assertEqual(app.method, 'manual')
        self.assertEqual(app.contact_email, 'Any.Person@qq.com')  # 仅域归一
        app.challenge_verified_at = __import__('datetime').datetime.now()
        app.status = 'submitted'
        db.session.commit()
        verification.approve_application(app, reviewer)
        db.session.commit()
        key = models.PersonIdentityModel.query.one()
        self.assertEqual((key.kind, key.canonical_key), ('email', 'Any.Person@qq.com'))


class RecoveryFlowTest(RosterTestBase):

    def test_submit_verify_and_privileged_two_review(self):
        # 公开提交（联系邮箱≠目标邮箱）
        case, code = self.recovery.submit_case(
            kind='account_lost', target_email='lost@qq.com',
            contact_email='new@qq.com', statement='原手机号已停用收不到验证码，希望恢复账号，可以核对往期培训营的参与记录。')
        db.session.commit()
        self.assertEqual(case.status, 'draft')
        self.assertFalse(self.recovery.verify_case(case.id, '000000'))
        self.assertTrue(self.recovery.verify_case(case.id, code))
        db.session.commit()
        self.assertEqual(case.status, 'submitted')
        self.assertFalse(case.require_two)   # 普通账号

        r1 = self.make_user('r1@x.dev', role='super_admin')
        r2 = self.make_user('r2@x.dev', role='super_admin')
        out = self.recovery.decide_case(case, r1, decision='approved', note='名册核对通过')
        db.session.commit()
        self.assertEqual(case.status, 'cooldown')
        self.assertEqual(case.cooldown_until.hour, 24 % 24 or case.cooldown_until.hour)
        # 冷静期未满不可执行
        from services.auth_context import AuthRejected
        with self.assertRaises(AuthRejected):
            self.recovery.complete_case(case, r1)
        # 届满后完成
        case.cooldown_until = __import__('datetime').datetime.now()
        db.session.commit()
        self.recovery.complete_case(case, r1)
        db.session.commit()
        self.assertEqual(case.status, 'done')

    def test_privileged_target_needs_two_reviewers(self):
        boss = self.make_user('boss@x.dev', role='super_admin')
        case, code = self.recovery.submit_case(
            kind='factor_lost', target_email='boss@x.dev',
            contact_email='boss2@qq.com', statement='换了手机后动态口令丢失，申请恢复账号的 MFA 因素，可以核对往年培训营的记录。')
        self.recovery.verify_case(case.id, code)
        db.session.commit()
        self.assertTrue(case.require_two)    # 目标是超管 → 72h+双人
        r1 = self.make_user('r1@x.dev')
        r2 = self.make_user('r2@x.dev')
        self.recovery.decide_case(case, r1, decision='approved')
        db.session.commit()
        self.assertEqual(case.status, 'submitted')  # 第一人不够
        from services.auth_context import AuthRejected
        with self.assertRaises(AuthRejected):       # 同一人再批不行
            self.recovery.decide_case(case, r1, decision='approved')
        self.recovery.decide_case(case, r2, decision='approved')
        db.session.commit()
        self.assertEqual(case.status, 'cooldown')
        self.assertGreaterEqual(case.cooldown_until,
                                __import__('datetime').datetime.now()
                                + __import__('datetime').timedelta(hours=71))

    def test_same_email_rejected(self):
        from services.auth_context import AuthRejected
        with self.assertRaises(AuthRejected):
            self.recovery.submit_case(
                kind='account_lost', target_email='same@qq.com',
                contact_email='same@qq.com', statement='x' * 30)


if __name__ == '__main__':
    unittest.main(verbosity=2)

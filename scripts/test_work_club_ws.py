# -*- coding: utf-8 -*-
"""X3 社团工作区（scope=club）单测（跨组方案 §4.4）。

覆盖：
  开通：scope=club 唯一（重复 409）；组区查重不受 club 行影响
  派生：在任干事（任意组）→ member；组长类 → coordinator；卸任/过期 → 撤；
        普通组员（无任职）不进社团区
  回填：backfill_workspace 对 club 全社在任干事批量授
  联动：sync_after_officer 任职变更同步社团区（新任授/卸任撤）
用法（项目根）：.venv/bin/python scripts/test_work_club_ws.py
"""
import os
import sys
import unittest
from datetime import date, datetime, timedelta

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


class ClubWsTestBase(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        db.init_app(self.app)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        from services.work import access as work_access, provisioning
        self.access = work_access
        self.prov = provisioning
        from models import (ClubGroup, ClubMembership, ClubOfficer, ClubPosition,
                            UserModel, WorkWorkspace)
        self.UserModel = UserModel
        self.ClubGroup = ClubGroup
        self.ClubMembership = ClubMembership
        self.ClubOfficer = ClubOfficer
        self.ClubPosition = ClubPosition
        self.WorkWorkspace = WorkWorkspace
        # 两个组 + 职位（组长类 rank>=10 / 干事类 rank<10）
        self.g1 = ClubGroup(name='技术组', status='active')
        self.g2 = ClubGroup(name='运营组', status='active')
        db.session.add_all([self.g1, self.g2])
        db.session.flush()
        self.pos_leader = ClubPosition(name='组长', sort_rank=10)
        self.pos_member = ClubPosition(name='干事', sort_rank=1)
        db.session.add_all([self.pos_leader, self.pos_member])
        db.session.flush()
        # 一个组工作区（对照组）+ 社团工作区
        self.ws_group = WorkWorkspace(club_group_id=self.g1.id, scope='group')
        self.ws_club = WorkWorkspace(club_group_id=None, scope='club')
        db.session.add_all([self.ws_group, self.ws_club])
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def make_user(self, email):
        u = self.UserModel(username=email.split('@')[0], email=email)
        u.set_password('x' * 32)
        db.session.add(u)
        db.session.flush()
        return u

    def make_officer(self, user, group, position, active=True):
        today = date.today()
        o = self.ClubOfficer(
            user_id=user.id, group_id=group.id,
            title_id=position.id, title=position.name,
            term_start=today - timedelta(days=10),
            term_end=None if active else today - timedelta(days=1),
            status='active' if active else 'ended')
        db.session.add(o)
        db.session.flush()
        return o


class ClubDesiredTest(ClubWsTestBase):

    def test_officer_any_group_gets_member(self):
        u = self.make_user('off@x.dev')
        self.make_officer(u, self.g2, self.pos_member)
        db.session.commit()
        got = self.prov._desired(u.id, self.ws_club)
        self.assertEqual(got[0], 'member')

    def test_leader_rank_gets_coordinator(self):
        u = self.make_user('lead@x.dev')
        self.make_officer(u, self.g1, self.pos_leader)
        db.session.commit()
        got = self.prov._desired(u.id, self.ws_club)
        self.assertEqual(got[0], 'coordinator')

    def test_plain_member_not_in_club_ws(self):
        u = self.make_user('plain@x.dev')
        db.session.add(self.ClubMembership(user_id=u.id, group_id=self.g1.id,
                                   slot='primary', joined_at=date.today()))
        db.session.commit()
        self.assertIsNone(self.prov._desired(u.id, self.ws_club))

    def test_expired_officer_not_in_club_ws(self):
        u = self.make_user('old@x.dev')
        self.make_officer(u, self.g1, self.pos_member, active=False)
        db.session.commit()
        self.assertIsNone(self.prov._desired(u.id, self.ws_club))

    def test_sync_grants_and_revokes_on_term_change(self):
        from models import WorkAccessGrant
        u = self.make_user('off@x.dev')
        o = self.make_officer(u, self.g1, self.pos_member)
        db.session.commit()
        action = self.prov.sync_user_workspace(u.id, self.ws_club.id)
        db.session.commit()
        self.assertEqual(action, 'granted')
        self.assertIsNotNone(self.access.workspace_access(u).get(self.ws_club.id))
        # 卸任（任职过期）→ 撤
        o.term_end = date.today() - timedelta(days=1)
        o.status = 'ended'
        db.session.commit()
        action2 = self.prov.sync_user_workspace(u.id, self.ws_club.id)
        db.session.commit()
        self.assertEqual(action2, 'revoked')
        self.assertIsNone(self.access.workspace_access(u).get(self.ws_club.id))


class ClubBackfillSyncTest(ClubWsTestBase):

    def test_backfill_covers_all_groups_officers(self):
        from models import WorkAccessGrant
        u1 = self.make_user('a@x.dev'); u2 = self.make_user('b@x.dev')
        u3 = self.make_user('c@x.dev')  # 普通组员，不应进
        self.make_officer(u1, self.g1, self.pos_member)
        self.make_officer(u2, self.g2, self.pos_leader)
        db.session.add(self.ClubMembership(user_id=u3.id, group_id=self.g1.id,
                                   slot='primary', joined_at=date.today()))
        db.session.commit()
        counts = self.prov.backfill_workspace(self.ws_club)
        db.session.commit()
        active = {g.user_id for g in WorkAccessGrant.query.filter_by(
            workspace_id=self.ws_club.id, status='active').all()}
        self.assertEqual(active, {u1.id, u2.id})
        roles = {g.user_id: g.role for g in WorkAccessGrant.query.filter_by(
            workspace_id=self.ws_club.id, status='active').all()}
        self.assertEqual(roles[u2.id], 'coordinator')
        self.assertEqual(roles[u1.id], 'member')

    def test_officer_change_hook_syncs_club_ws(self):
        from models import WorkAccessGrant
        u = self.make_user('hook@x.dev')
        db.session.commit()
        # 未任职：社团区无授权
        self.prov.sync_after_officer(u.id, None, None)
        db.session.commit()
        self.assertFalse(WorkAccessGrant.query.filter_by(
            workspace_id=self.ws_club.id, user_id=u.id).all())
        # 新任干事：经钩子（组区+社团区）自动授
        self.make_officer(u, self.g2, self.pos_member)
        db.session.commit()
        self.prov.sync_after_officer(u.id, None, self.g2.id, event='任职变更')
        db.session.commit()
        self.assertIsNotNone(WorkAccessGrant.query.filter_by(
            workspace_id=self.ws_club.id, user_id=u.id, status='active').first())
        # 卸任：社团区撤
        from models import ClubOfficer
        ClubOfficer.query.filter_by(user_id=u.id).update(
            {'term_end': date.today() - timedelta(days=1), 'status': 'ended'})
        db.session.commit()
        self.prov.sync_after_officer(u.id, self.g2.id, None, event='卸任')
        db.session.commit()
        row = WorkAccessGrant.query.filter_by(
            workspace_id=self.ws_club.id, user_id=u.id).first()
        self.assertEqual(row.status, 'revoked')


class DualSlotAppointmentTest(ClubWsTestBase):
    """2026-10-04 组长解耦：club/group 两槽跨槽可兼任（组长+社团职务并存），
    同槽互斥（不可双组长/双社团职务）。校验器 + 授权派生双覆盖。"""

    def setUp(self):
        super().setUp()
        from blueprints.officers import _validate_appointment
        self.validate = _validate_appointment

    def test_leader_can_hold_club_position(self):
        u = self.make_user('dual@x.dev')
        self.make_officer(u, self.g1, self.pos_leader)      # 组长（group 槽）
        db.session.commit()
        # 再任社团职务（club 槽，挂另一组）——放行
        self.assertIsNone(self.validate(u.id, self.pos_member, self.g2))

    def test_club_position_then_leader_allowed(self):
        u = self.make_user('dual2@x.dev')
        self.make_officer(u, self.g1, self.pos_member)      # 干事（club 槽）
        db.session.commit()
        # 再任组长（group 槽，同组）——放行
        self.assertIsNone(self.validate(u.id, self.pos_leader, self.g1))

    def test_two_leaderships_blocked(self):
        u = self.make_user('dup@x.dev')
        self.make_officer(u, self.g1, self.pos_leader)
        db.session.commit()
        code, msg = self.validate(u.id, self.pos_leader, self.g2)
        self.assertEqual(code, 409)
        self.assertIn('同级', msg)

    def test_two_club_positions_blocked(self):
        u = self.make_user('dup2@x.dev')
        self.make_officer(u, self.g1, self.pos_member)
        self.pos_vp = self.ClubPosition(name='副社长', sort_rank=2)
        db.session.add(self.pos_vp)
        db.session.flush()
        db.session.commit()
        code, _ = self.validate(u.id, self.pos_vp, self.g1)
        self.assertEqual(code, 409)

    def test_edit_self_row_exempt(self):
        u = self.make_user('renew@x.dev')
        row = self.make_officer(u, self.g1, self.pos_leader)
        db.session.commit()
        # 连任/编辑自身行：exclude_ids 豁免
        self.assertIsNone(self.validate(u.id, self.pos_leader, self.g1,
                                        exclude_ids=[row.id]))

    def test_dual_slot_role_derivation(self):
        """组长+干事并存：社团区=coordinator（组长优先），组长组=coordinator，
        干事挂组=member（无归属回落任职来源）。"""
        u = self.make_user('dual3@x.dev')
        self.make_officer(u, self.g1, self.pos_leader)
        self.make_officer(u, self.g2, self.pos_member)
        db.session.commit()
        self.assertEqual(self.prov._desired(u.id, self.ws_club)[0], 'coordinator')
        self.assertEqual(self.prov._desired(u.id, self.ws_group)[0], 'coordinator')
        ws_g2 = self.WorkWorkspace(club_group_id=self.g2.id, scope='group')
        db.session.add(ws_g2)
        db.session.commit()
        self.assertEqual(self.prov._desired(u.id, ws_g2)[0], 'member')

if __name__ == '__main__':
    unittest.main(verbosity=2)

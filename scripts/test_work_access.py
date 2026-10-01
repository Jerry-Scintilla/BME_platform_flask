"""内部工作台·授权与治理的隔离回归（M1）；只使用内存 SQLite。

覆盖设计方案 §15 用例：A01（未开通者探测/治理端点 403）、A05（任期到期/
撤销授权即时失效）、A06（归属行原地改组授权失效）、D04 核心（工作区停用关闸）、
§5.2 治理不能造治理、组织变更事件挂钩（club_admin/officers 同事务留痕）。
对象级可见性（can_read_item 草稿/工作区/参与人三支）先行直测服务层，M2 补列表过滤。

用法（项目根）：.venv/bin/python scripts/test_work_access.py
"""
import io
import os
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import unittest
from datetime import date, datetime, timedelta

from flask import Flask
from flask_jwt_extended import JWTManager, create_access_token
from sqlalchemy.dialects.mysql import LONGTEXT, MEDIUMTEXT
from sqlalchemy.ext.compiler import compiles

from exts import db
from models import (UserModel, ClubGroup, ClubPosition, ClubOfficer, ClubMembership,
                    WorkAccessGrant, WorkItem, WorkItemParticipant, WorkTask, WorkWorkspace,
                    ClubOrgEvent)
from blueprints import work as work_module, work_files as work_files_module, \
    club_admin as club_admin_module, officers as officers_module
from services.work import access


@compiles(MEDIUMTEXT, 'sqlite')
@compiles(LONGTEXT, 'sqlite')
def _mysql_text_on_sqlite(_element, _compiler, **_kwargs):
    return 'TEXT'


class WorkAccessTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
            JWT_SECRET_KEY='test-secret-key-with-at-least-32-characters',
        )
        db.init_app(self.app)
        JWTManager(self.app)
        self.app.register_blueprint(work_module.bp)
        self.app.register_blueprint(work_files_module.bp)
        self.app.register_blueprint(club_admin_module.bp)
        self.app.register_blueprint(officers_module.bp)
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        self.client = self.app.test_client()

        # 人员：超管 / 干事（组长挂软件组）/ 组员（归属软件组）/ 普通学员 / 治理人员
        self.super_admin = UserModel(username='超管', email='super@t.dev', role='super_admin')
        self.super_admin.set_password('a' * 32)
        self.officer_user = UserModel(username='干事甲', email='officer@t.dev', role='user')
        self.officer_user.set_password('a' * 32)
        self.member_user = UserModel(username='组员乙', email='member@t.dev', role='user')
        self.member_user.set_password('a' * 32)
        self.plain_user = UserModel(username='学员丙', email='plain@t.dev', role='user')
        self.plain_user.set_password('a' * 32)
        self.gov_user = UserModel(username='治理丁', email='gov@t.dev', role='user')
        self.gov_user.set_password('a' * 32)
        db.session.add_all([self.super_admin, self.officer_user, self.member_user,
                            self.plain_user, self.gov_user])
        db.session.flush()

        # 组织：软件组/硬件组 + 组长职位 + 干事任职（在任）+ 组员归属（软件组）
        self.g_soft = ClubGroup(name='软件组')
        self.g_hard = ClubGroup(name='硬件组')
        db.session.add_all([self.g_soft, self.g_hard])
        db.session.flush()
        self.position = ClubPosition(name='组长', group_rule='optional')
        db.session.add(self.position)
        db.session.flush()
        self.officer = ClubOfficer(
            user_id=self.officer_user.id, title_id=self.position.id, title='组长',
            group_id=self.g_soft.id, department='软件组',
            term_start=date.today() - timedelta(days=30), status='active')
        self.membership = ClubMembership(
            user_id=self.member_user.id, group_id=self.g_soft.id,
            slot='primary', joined_at=date.today())
        db.session.add_all([self.officer, self.membership])
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    # ── 助手 ──────────────────────────────────────────────

    def _auth(self, user):
        return {'Authorization': f'Bearer {create_access_token(identity=user.email)}'}

    def _open_workspace(self, group):
        resp = self.client.post('/work/governance/workspaces',
                                headers=self._auth(self.super_admin),
                                json={'club_group_id': group.id})
        assert resp.status_code == 200, resp.get_json()
        return WorkWorkspace.query.filter_by(club_group_id=group.id).first()

    def _grant(self, actor, user, ws, role, source_type, source_row,
               valid_until=None, expect=200):
        payload = {'user_id': user.id, 'role': role, 'source_type': source_type,
                   'grant_reason': '冒烟授权'}
        if ws is not None:
            payload['workspace_id'] = ws.id
        if source_row is not None:
            payload['source_id'] = source_row.id
        if valid_until:
            payload['valid_until'] = valid_until
        resp = self.client.post('/work/governance/grants', headers=self._auth(actor),
                                json=payload)
        assert resp.status_code == expect, (resp.status_code, resp.get_json())
        return resp

    def _me(self, user):
        resp = self.client.get('/work/me', headers=self._auth(user))
        assert resp.status_code == 200
        return resp.get_json()['data']

    # ── A01：未开通者探测与治理端点 ───────────────────────

    def test_a01_probe_returns_empty_shape_for_plain_user(self):
        data = self._me(self.plain_user)
        self.assertIsNone(data['eligibility'])
        self.assertFalse(data['is_governance'])
        self.assertEqual(data['workspaces'], [])
        self.assertEqual(data['todo']['pending_responses'], 0)

    def test_a01_plain_user_rejected_on_governance(self):
        resp = self.client.get('/work/governance/grants', headers=self._auth(self.plain_user))
        self.assertEqual(resp.status_code, 403)
        # 无权与不存在统一形态：工作区管理同样 403
        resp = self.client.post('/work/governance/workspaces',
                                headers=self._auth(self.plain_user),
                                json={'club_group_id': self.g_soft.id})
        self.assertEqual(resp.status_code, 403)

    def test_a01_workspace_access_not_leaked_to_plain_member(self):
        """普通组员（有归属但无授权）不因组织身份获得任何工作区（§3.3）。"""
        ws = self._open_workspace(self.g_soft)
        self._grant(self.super_admin, self.officer_user, ws, 'coordinator',
                    'officer', self.officer)
        data = self._me(self.member_user)          # 有 membership、无 grant
        self.assertEqual(data['workspaces'], [])
        self.assertIsNone(data['eligibility'])     # 组员资格需「归属∧已开通」同时成立

    # ── 授权开通与生效 ────────────────────────────────────

    def test_officer_source_grant_grants_workspace_role(self):
        ws = self._open_workspace(self.g_soft)
        self._grant(self.super_admin, self.officer_user, ws, 'coordinator',
                    'officer', self.officer)
        data = self._me(self.officer_user)
        self.assertEqual(len(data['workspaces']), 1)
        self.assertEqual(data['workspaces'][0]['role'], 'coordinator')
        self.assertEqual(data['workspaces'][0]['group_name'], '软件组')
        self.assertEqual(data['eligibility']['kind'], 'officer')

    def test_membership_source_grant_for_member(self):
        ws = self._open_workspace(self.g_soft)
        self._grant(self.super_admin, self.member_user, ws, 'member',
                    'membership', self.membership)
        data = self._me(self.member_user)
        self.assertEqual(len(data['workspaces']), 1)
        self.assertEqual(data['workspaces'][0]['role'], 'member')
        self.assertEqual(data['eligibility']['kind'], 'member')

    def test_duplicate_grant_conflict(self):
        ws = self._open_workspace(self.g_soft)
        self._grant(self.super_admin, self.member_user, ws, 'member',
                    'membership', self.membership)
        self._grant(self.super_admin, self.member_user, ws, 'member',
                    'membership', self.membership, expect=409)

    def test_governance_cannot_create_governance(self):
        """§5.2：治理人员不能造治理——governance 岗位仅超管可授。"""
        db.session.add(WorkAccessGrant(
            user_id=self.gov_user.id, role='governance', workspace_id=None,
            source_type='direct', granted_by=self.super_admin.id,
            grant_reason='初始化', status='active'))
        db.session.commit()
        ws = self._open_workspace(self.g_soft)
        # 治理人员可开通普通岗位
        self._grant(self.gov_user, self.member_user, ws, 'member',
                    'membership', self.membership)
        # 但不能授 governance
        resp = self.client.post('/work/governance/grants', headers=self._auth(self.gov_user),
                                json={'user_id': self.plain_user.id, 'role': 'governance',
                                      'source_type': 'direct', 'grant_reason': '越权'})
        self.assertEqual(resp.status_code, 403)

    def test_bootstrap_only_super_admin_and_no_dup(self):
        resp = self.client.post('/work/governance/bootstrap',
                                headers=self._auth(self.member_user),
                                json={'user_id': self.gov_user.id, 'grant_reason': 'x'})
        self.assertEqual(resp.status_code, 403)
        resp = self.client.post('/work/governance/bootstrap',
                                headers=self._auth(self.super_admin),
                                json={'user_id': self.gov_user.id, 'grant_reason': '首次初始化'})
        self.assertEqual(resp.status_code, 200)
        resp = self.client.post('/work/governance/bootstrap',
                                headers=self._auth(self.super_admin),
                                json={'user_id': self.gov_user.id, 'grant_reason': '再来'})
        self.assertEqual(resp.status_code, 409)
        self.assertTrue(access.is_governance(self.gov_user))

    # ── A05：任期到期/撤销即时失效 ─────────────────────────

    def test_a05_officer_term_expiry_invalidates_grant(self):
        ws = self._open_workspace(self.g_soft)
        self._grant(self.super_admin, self.officer_user, ws, 'coordinator',
                    'officer', self.officer)
        self.assertEqual(len(self._me(self.officer_user)['workspaces']), 1)
        # 任期昨日结束（后台未执行卸任）：授权立即失效，无需重新登录
        self.officer.term_end = date.today() - timedelta(days=1)
        db.session.commit()
        data = self._me(self.officer_user)
        self.assertEqual(data['workspaces'], [])

    def test_a05_revoke_takes_effect_immediately(self):
        ws = self._open_workspace(self.g_soft)
        self._grant(self.super_admin, self.member_user, ws, 'member',
                    'membership', self.membership)
        self.assertEqual(len(self._me(self.member_user)['workspaces']), 1)
        grant = WorkAccessGrant.query.filter_by(user_id=self.member_user.id).one()
        resp = self.client.post(f"/work/governance/grants/{grant.id}/revoke",
                                headers=self._auth(self.super_admin),
                                json={'reason': '试点结束'})
        self.assertEqual(resp.status_code, 200)
        data = self._me(self.member_user)
        self.assertEqual(data['workspaces'], [])
        # 撤销需要原因
        grant2 = WorkAccessGrant(user_id=self.member_user.id, role='member',
                                 workspace_id=ws.id, source_type='membership',
                                 source_id=self.membership.id,
                                 group_id_snapshot=self.g_soft.id,
                                 granted_by=self.super_admin.id, grant_reason='再开')
        db.session.add(grant2)
        db.session.commit()
        resp = self.client.post(f"/work/governance/grants/{grant2.id}/revoke",
                                headers=self._auth(self.super_admin), json={})
        self.assertEqual(resp.status_code, 400)

    # ── #7：授权来源组与工作区组绑定校验（收紧拍板） ──────

    def test_grant_membership_source_must_match_workspace_group(self):
        """A 组 membership 授 B 组工作区 → 400（跨组协作走事项参与者邀请）。"""
        ws_soft = self._open_workspace(self.g_soft)
        ws_hard = self._open_workspace(self.g_hard)
        resp = self.client.post('/work/governance/grants', headers=self._auth(self.super_admin),
                                json={'user_id': self.member_user.id, 'role': 'member',
                                      'workspace_id': ws_hard.id,
                                      'source_type': 'membership',
                                      'source_id': self.membership.id,
                                      'grant_reason': '跨组越界'})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('归属来源与工作区组不一致', resp.get_json()['message'])
        # 同组开通不受影响
        self._grant(self.super_admin, self.member_user, ws_soft, 'member',
                    'membership', self.membership)

    def test_grant_officer_group_mismatch_rejected_but_club_level_allowed(self):
        """挂组任职授他组工作区 → 400；社团级职位（group_id 空）放行任意组。"""
        ws_soft = self._open_workspace(self.g_soft)
        ws_hard = self._open_workspace(self.g_hard)
        # 干事甲挂软件组 → 授硬件组工作区被拒
        resp = self.client.post('/work/governance/grants', headers=self._auth(self.super_admin),
                                json={'user_id': self.officer_user.id, 'role': 'coordinator',
                                      'workspace_id': ws_hard.id,
                                      'source_type': 'officer',
                                      'source_id': self.officer.id,
                                      'grant_reason': '挂组越界'})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('任职来源挂靠组与工作区组不一致', resp.get_json()['message'])
        # 社团级职位（如社长，group_id=None）可授任意组工作区
        club_officer = ClubOfficer(
            user_id=self.plain_user.id, title_id=self.position.id, title='社长',
            group_id=None, term_start=date.today() - timedelta(days=10), status='active')
        db.session.add(club_officer)
        db.session.commit()
        self._grant(self.super_admin, self.plain_user, ws_hard, 'member',
                    'officer', club_officer)
        data = self._me(self.plain_user)
        self.assertEqual(len(data['workspaces']), 1)
        self.assertEqual(data['workspaces'][0]['id'], ws_hard.id)

    def test_candidates_gate_is_eligibility_based(self):
        """#43：候选人口径为资格判定——纯在任干事（无任何授权）可调取
        （可被邀为参与者/需要选回应人）；无资格者仍 403。"""
        r = self.client.get('/work/candidates', headers=self._auth(self.officer_user))
        self.assertEqual(r.status_code, 200)
        uids = [c['user_id'] for c in r.get_json()['data']['candidates']]
        self.assertIn(self.officer_user.id, uids)       # 在任干事即入选
        self.assertNotIn(self.member_user.id, uids)     # 未开通授权的组员不在列
        r = self.client.get('/work/candidates', headers=self._auth(self.plain_user))
        self.assertEqual(r.status_code, 403)

    # ── A06：归属行原地改组，授权不跟随漂移 ────────────────

    def test_a06_membership_group_drift_invalidates_grant(self):
        ws = self._open_workspace(self.g_soft)
        self._grant(self.super_admin, self.member_user, ws, 'member',
                    'membership', self.membership)
        self.assertEqual(len(self._me(self.member_user)['workspaces']), 1)
        # 管理端 PUT membership 原地覆盖：归属行改指硬件组 → 快照不匹配 → 失效
        self.membership.group_id = self.g_hard.id
        db.session.commit()
        self.assertEqual(self._me(self.member_user)['workspaces'], [])
        grant = WorkAccessGrant.query.filter_by(user_id=self.member_user.id).one()
        self.assertEqual(access.grant_status_reason(grant),
                         (False, '组归属已调整（授权绑定原组）'))

    # ── D04 核心：工作区停用关闸 ──────────────────────────

    def test_d04_disabled_workspace_blocks_access(self):
        ws = self._open_workspace(self.g_soft)
        self._grant(self.super_admin, self.member_user, ws, 'member',
                    'membership', self.membership)
        self.assertEqual(len(self._me(self.member_user)['workspaces']), 1)
        resp = self.client.post(f"/work/governance/workspaces/{ws.id}/status",
                                headers=self._auth(self.super_admin),
                                json={'status': 'disabled'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._me(self.member_user)['workspaces'], [])
        # 数据保留：工作区与授权记录仍在
        self.assertIsNotNone(WorkWorkspace.query.get(ws.id))
        self.assertEqual(WorkAccessGrant.query.filter_by(user_id=self.member_user.id).count(), 1)
        resp = self.client.post(f"/work/governance/workspaces/{ws.id}/status",
                                headers=self._auth(self.super_admin),
                                json={'status': 'active'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(self._me(self.member_user)['workspaces']), 1)

    def test_workspace_create_guards(self):
        self._open_workspace(self.g_soft)
        resp = self.client.post('/work/governance/workspaces',
                                headers=self._auth(self.super_admin),
                                json={'club_group_id': self.g_soft.id})
        self.assertEqual(resp.status_code, 409)      # 一组最多一个工作区
        resp = self.client.post('/work/governance/workspaces',
                                headers=self._auth(self.super_admin),
                                json={'club_group_id': 99999})
        self.assertEqual(resp.status_code, 404)

    # ── 对象级可见性（服务层直测，M2 列表过滤的前置）────────

    def _make_item(self, ws, visibility='workspace', status='open', creator=None):
        item = WorkItem(workspace_id=ws.id, kind='topic', title='t', visibility=visibility,
                        status=status, created_by=(creator or self.officer_user).id)
        db.session.add(item)
        db.session.commit()
        return item

    def test_can_read_item_three_branches(self):
        ws = self._open_workspace(self.g_soft)
        self._grant(self.super_admin, self.officer_user, ws, 'coordinator',
                    'officer', self.officer)
        self._grant(self.super_admin, self.member_user, ws, 'member',
                    'membership', self.membership)

        draft = self._make_item(ws, status='draft', creator=self.officer_user)
        ws_item = self._make_item(ws, visibility='workspace', creator=self.officer_user)
        pt_item = self._make_item(ws, visibility='participants', creator=self.officer_user)
        db.session.add(WorkItemParticipant(item_id=pt_item.id, user_id=self.member_user.id))
        db.session.commit()

        # 草稿仅作者；工作区档=有效授权（member 可读）；参与档=作者∨参与行
        self.assertIsNotNone(access.can_read_item(self.officer_user, draft))
        self.assertIsNone(access.can_read_item(self.member_user, draft))
        acc = access.can_read_item(self.member_user, ws_item)
        self.assertEqual((acc.via, acc.workspace_role), ('workspace', 'member'))
        self.assertIsNone(access.can_read_item(self.plain_user, ws_item))
        self.assertIsNotNone(access.can_read_item(self.member_user, pt_item))
        self.assertEqual(access.can_read_item(self.member_user, pt_item).via, 'participant')
        # 参与档不因工作区授权放行：member 对未参与的受限事项无权（协调员亦然，§5.3）
        pt2 = self._make_item(ws, visibility='participants', creator=self.member_user)
        self.assertIsNone(access.can_read_item(self.officer_user, pt2))

    # ── 组织变更事件挂钩（club_admin/officers 同事务留痕）──

    def test_membership_change_records_org_event(self):
        resp = self.client.put(f"/admin/club/membership/{self.member_user.id}",
                               headers=self._auth(self.super_admin),
                               json={'primary': self.g_hard.id})
        self.assertEqual(resp.status_code, 200, resp.get_json())
        ev = ClubOrgEvent.query.filter_by(kind='membership_set').one()
        self.assertEqual(ev.user_id, self.member_user.id)
        change = __import__('json').loads(ev.detail_json)['changes'][0]
        self.assertEqual(change['from_group_id'], self.g_soft.id)
        self.assertEqual(change['to_group_id'], self.g_hard.id)
        # 挂钩同时真实改了归属行（A06 的数据面）
        self.assertEqual(self.membership.group_id, self.g_hard.id)

    def test_officer_appoint_records_org_event(self):
        resp = self.client.post('/admin/officers', headers=self._auth(self.super_admin),
                                json={'user_id': self.member_user.id, 'title': '组长',
                                      'department': '硬件组'})
        self.assertEqual(resp.status_code, 200, resp.get_json())
        ev = ClubOrgEvent.query.filter_by(kind='officer_appointed').one()
        self.assertEqual(ev.user_id, self.member_user.id)
        self.assertEqual(ev.to_group_id, self.g_hard.id)


class _WorkItemsBase(unittest.TestCase):
    """共享脚手架：双组双工作区 + 协调员/组员/跨组员/局外人四类身份（M2/M3 共用）。"""

    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
            JWT_SECRET_KEY='test-secret-key-with-atleast-32-characters',
        )
        db.init_app(self.app)
        JWTManager(self.app)
        self.app.register_blueprint(work_module.bp)
        self.app.register_blueprint(work_files_module.bp)
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        self.client = self.app.test_client()

        self.coord = UserModel(username='软件组长', email='coord@t.dev', role='user')
        self.coord.set_password('a' * 32)
        self.member1 = UserModel(username='软件组员', email='m1@t.dev', role='user')
        self.member1.set_password('a' * 32)
        self.member2 = UserModel(username='硬件组员', email='m2@t.dev', role='user')
        self.member2.set_password('a' * 32)
        self.plain = UserModel(username='局外人', email='plain@t.dev', role='user')
        self.plain.set_password('a' * 32)
        db.session.add_all([self.coord, self.member1, self.member2, self.plain])
        db.session.flush()

        self.g_soft = ClubGroup(name='软件组')
        self.g_hard = ClubGroup(name='硬件组')
        db.session.add_all([self.g_soft, self.g_hard])
        db.session.flush()
        self.ws_soft = WorkWorkspace(club_group_id=self.g_soft.id, status='active')
        self.ws_hard = WorkWorkspace(club_group_id=self.g_hard.id, status='active')
        db.session.add_all([self.ws_soft, self.ws_hard])
        db.session.flush()

        m0 = ClubMembership(user_id=self.coord.id, group_id=self.g_soft.id,
                            slot='primary', joined_at=date.today())
        m1 = ClubMembership(user_id=self.member1.id, group_id=self.g_soft.id,
                            slot='primary', joined_at=date.today())
        m2 = ClubMembership(user_id=self.member2.id, group_id=self.g_hard.id,
                            slot='primary', joined_at=date.today())
        db.session.add_all([m0, m1, m2])
        db.session.flush()
        for grant in (
            WorkAccessGrant(user_id=self.coord.id, role='coordinator',
                            workspace_id=self.ws_soft.id, source_type='membership',
                            source_id=m0.id, group_id_snapshot=self.g_soft.id,
                            granted_by=self.coord.id, grant_reason='t'),
            WorkAccessGrant(user_id=self.member1.id, role='member',
                            workspace_id=self.ws_soft.id, source_type='membership',
                            source_id=m1.id, group_id_snapshot=self.g_soft.id,
                            granted_by=self.coord.id, grant_reason='t'),
            WorkAccessGrant(user_id=self.member2.id, role='member',
                            workspace_id=self.ws_hard.id, source_type='membership',
                            source_id=m2.id, group_id_snapshot=self.g_hard.id,
                            granted_by=self.coord.id, grant_reason='t'),
        ):
            db.session.add(grant)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def _auth(self, user):
        return {'Authorization': f'Bearer {create_access_token(identity=user.email)}'}

    def _create_item(self, user, ws_id, visibility='workspace', kind='topic',
                     title='测试话题', status_publish=True, body=None):
        r = self.client.post('/work/items', headers=self._auth(user), json={
            'workspace_id': ws_id, 'kind': kind, 'title': title,
            'visibility': visibility, 'body': body or '正文', 'idempotency_key': None})
        assert r.status_code == 200, r.get_json()
        item_id = r.get_json()['data']['id']
        if status_publish:
            r = self.client.post(f'/work/items/{item_id}/commands', headers=self._auth(user),
                                 json={'command': 'publish', 'expected_version': 1})
            assert r.status_code == 200, r.get_json()
        return item_id

    def _reply(self, user, item_id, body='回复内容', crid=None, **extra):
        payload = {'body': body, 'client_request_id': crid or f'cr-{time.time()}-{id(body)}'}
        payload.update(extra)
        return self.client.post(f'/work/items/{item_id}/replies',
                                headers=self._auth(user), json=payload)


    def _create_task(self, creator, assignee, ws_id=None, reviewer=None, due_offset_days=7):
        """建任务草稿并发布（返回 item_id + 发布后版本）。"""
        ws_id = ws_id or self.ws_soft.id
        payload = {
            'workspace_id': ws_id, 'kind': 'task', 'title': '冒烟任务',
            'visibility': 'workspace',
            'task': {'assignee_user_id': assignee.id,
                     'due_at': (date.today() + timedelta(days=due_offset_days)).isoformat()},
            'idempotency_key': f'task-{time.time()}',
        }
        if reviewer is not None:
            payload['task']['reviewer_user_id'] = reviewer.id
        r = self.client.post('/work/items', headers=self._auth(creator), json=payload)
        assert r.status_code == 200, r.get_json()
        item_id = r.get_json()['data']['id']
        r = self.client.post(f'/work/items/{item_id}/commands', headers=self._auth(creator),
                             json={'command': 'publish', 'expected_version': 1})
        assert r.status_code == 200, r.get_json()
        return item_id, 2          # 发布后 version=2

    def _cmd(self, user, item_id, command, version, expect=200, **params):
        payload = {'command': command, 'expected_version': version, **params}
        r = self.client.post(f'/work/items/{item_id}/commands',
                             headers=self._auth(user), json=payload)
        assert r.status_code == expect, (command, r.status_code, r.get_json())
        return r

class WorkItemsTest(_WorkItemsBase):
    """M2 事项/回复/通知/已读的隔离回归（A02/A03/A04/B01 等）。"""

    # ── A02：跨组隔离（URL/列表/搜索/计数） ─────────────────

    def test_a02_cross_group_isolation(self):
        item = self._create_item(self.coord, self.ws_soft.id, title='软件组内部排期')
        r = self.client.get(f'/work/items/{item}', headers=self._auth(self.member2))
        self.assertEqual(r.status_code, 404)
        r = self.client.get('/work/items', headers=self._auth(self.member2))
        data = r.get_json()['data']
        ids = [i['id'] for i in data['items']]
        self.assertNotIn(item, ids)
        self.assertEqual(data['total'], 0)
        r = self.client.get('/work/items?q=排期', headers=self._auth(self.member2))
        self.assertEqual(r.get_json()['data']['total'], 0)     # 搜索不泄露
        # 本组成员可见
        r = self.client.get(f'/work/items/{item}', headers=self._auth(self.member1))
        self.assertEqual(r.status_code, 200)

    def test_a03_coordinator_no_native_access_to_restricted(self):
        """§5.3：participants 档协调员无天然阅读权。"""
        item = self._create_item(self.member1, self.ws_soft.id,
                                 visibility='participants', title='受限话题')
        r = self.client.get(f'/work/items/{item}', headers=self._auth(self.coord))
        self.assertEqual(r.status_code, 404)

    def test_a04_invited_member_sees_only_that_item(self):
        item = self._create_item(self.member1, self.ws_soft.id,
                                 visibility='participants', title='跨组协作事项')
        other = self._create_item(self.member1, self.ws_soft.id,
                                  visibility='participants', title='另一个受限事项')
        # 邀请硬件组员参与 item
        r = self.client.post(f'/work/items/{item}/participants', headers=self._auth(self.member1),
                             json={'user_id': self.member2.id})
        self.assertEqual(r.status_code, 200, r.get_json())
        # 被邀者可见该事项
        r = self.client.get(f'/work/items/{item}', headers=self._auth(self.member2))
        self.assertEqual(r.status_code, 200)
        # 同组其他受限事项仍 404；列表只见被邀事项，total=1（计数不泄露）
        r = self.client.get(f'/work/items/{other}', headers=self._auth(self.member2))
        self.assertEqual(r.status_code, 404)
        r = self.client.get('/work/items', headers=self._auth(self.member2))
        data = r.get_json()['data']
        self.assertEqual([i['id'] for i in data['items']], [item])
        self.assertEqual(data['total'], 1)

    def test_a04_invite_requires_eligibility(self):
        item = self._create_item(self.member1, self.ws_soft.id)
        r = self.client.post(f'/work/items/{item}/participants',
                             headers=self._auth(self.member1),
                             json={'user_id': self.plain.id})
        self.assertEqual(r.status_code, 400)      # 无资格不能被邀请

    # ── B01：幂等（回复/创建） ─────────────────────────────

    def test_b01_reply_idempotent(self):
        item = self._create_item(self.coord, self.ws_soft.id)
        r1 = self._reply(self.member1, item, body='第一次', crid='fixed-cr-1')
        r2 = self._reply(self.member1, item, body='第一次重试', crid='fixed-cr-1')
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r1.get_json()['data']['seq'], r2.get_json()['data']['seq'])
        from models import WorkReply, WorkNotificationReceipt
        self.assertEqual(WorkReply.query.filter_by(item_id=item).count(), 1)
        # 通知只发一次（协调员=另一成员收到一条回复通知）
        self.assertEqual(WorkNotificationReceipt.query.filter_by(
            notify_type='reply_to_me').count(), 1)

    def test_b01_create_idempotent(self):
        r1 = self.client.post('/work/items', headers=self._auth(self.coord), json={
            'workspace_id': self.ws_soft.id, 'kind': 'topic', 'title': '幂等话题',
            'idempotency_key': 'idem-1'})
        r2 = self.client.post('/work/items', headers=self._auth(self.coord), json={
            'workspace_id': self.ws_soft.id, 'kind': 'topic', 'title': '幂等话题重发',
            'idempotency_key': 'idem-1'})
        self.assertEqual(r1.get_json()['data']['id'], r2.get_json()['data']['id'])
        self.assertEqual(WorkItem.query.filter_by(title='幂等话题').count(), 1)

    def test_b01_idem_key_scoped_per_user(self):
        """#13：幂等键按创建者隔离——跨用户撞 key 各自成文，不 500 不占位。"""
        r1 = self.client.post('/work/items', headers=self._auth(self.coord), json={
            'workspace_id': self.ws_soft.id, 'kind': 'topic', 'title': '甲的话题',
            'idempotency_key': 'shared-key'})
        r2 = self.client.post('/work/items', headers=self._auth(self.member1), json={
            'workspace_id': self.ws_soft.id, 'kind': 'topic', 'title': '乙的话题',
            'idempotency_key': 'shared-key'})
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        self.assertNotEqual(r1.get_json()['data']['id'], r2.get_json()['data']['id'])
        # 甲方重试同 key 仍回自己的原事项
        r1b = self.client.post('/work/items', headers=self._auth(self.coord), json={
            'workspace_id': self.ws_soft.id, 'kind': 'topic', 'title': '甲的重试',
            'idempotency_key': 'shared-key'})
        self.assertEqual(r1b.get_json()['data']['id'], r1.get_json()['data']['id'])

    def test_b01_reply_request_id_scoped_per_item(self):
        """#14：client_request_id 限定本事项——撞其他事项的同名 key 明确 409。"""
        item_a = self._create_item(self.coord, self.ws_soft.id, title='事项甲')
        item_b = self._create_item(self.coord, self.ws_soft.id, title='事项乙')
        r1 = self._reply(self.member1, item_a, body='甲的回复', crid='same-crid')
        self.assertEqual(r1.status_code, 200)
        # 同一作者在事项乙复用该 key：不返回甲的回复当成功，明确冲突
        r2 = self._reply(self.member1, item_b, body='乙的回复', crid='same-crid')
        self.assertEqual(r2.status_code, 409)
        self.assertIn('其他事项', r2.get_json()['message'])
        from models import WorkReply
        self.assertEqual(WorkReply.query.filter_by(client_request_id='same-crid').count(), 1)
        # 事项甲内重试同 key 仍幂等返回原回复
        r3 = self._reply(self.member1, item_a, body='甲的重试', crid='same-crid')
        self.assertEqual(r3.status_code, 200)
        self.assertEqual(r3.get_json()['data']['seq'], r1.get_json()['data']['seq'])

    def test_b01_receipt_links_notification_row(self):
        """#3：回执 notification_id 真实回填（回执→通知链路可用）。"""
        from models import WorkNotificationReceipt
        item = self._create_item(self.coord, self.ws_soft.id)
        self._reply(self.member1, item, body='产生通知', crid='cr-nid')
        receipts = WorkNotificationReceipt.query.all()
        self.assertTrue(receipts)
        self.assertTrue(all(r.notification_id is not None for r in receipts))

    # ── 流转与编辑 ─────────────────────────────────────────

    def test_publish_close_reopen_with_notifications(self):
        item = self._create_item(self.coord, self.ws_soft.id, status_publish=False)
        # 草稿仅作者可见
        r = self.client.get(f'/work/items/{item}', headers=self._auth(self.member1))
        self.assertEqual(r.status_code, 404)
        # 发布 → 工作区可见 + 发布通知（member1 一条）
        from models import WorkNotificationReceipt, NotificationModel
        r = self.client.post(f'/work/items/{item}/commands', headers=self._auth(self.coord),
                             json={'command': 'publish', 'expected_version': 1})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(NotificationModel.query.filter_by(
            user_id=self.member1.id, category='work').count(), 1)
        r = self.client.get(f'/work/items/{item}', headers=self._auth(self.member1))
        self.assertEqual(r.status_code, 200)
        # 已关闭话题停止普通回复（§7.1）
        r = self.client.post(f'/work/items/{item}/commands', headers=self._auth(self.coord),
                             json={'command': 'close', 'expected_version': 2, 'reason': '结论已成'})
        self.assertEqual(r.status_code, 200)
        r = self._reply(self.member1, item, body='还想说')
        self.assertEqual(r.status_code, 409)
        # 重新打开后可回复
        self.client.post(f'/work/items/{item}/commands', headers=self._auth(self.coord),
                         json={'command': 'reopen', 'expected_version': 3})
        r = self._reply(self.member1, item, body='补充')
        self.assertEqual(r.status_code, 200)

    def test_patch_version_conflict(self):
        item = self._create_item(self.coord, self.ws_soft.id)
        # 发布后 version=2（创建=1，发布+1）
        r = self.client.patch(f'/work/items/{item}', headers=self._auth(self.coord),
                              json={'title': '新标题', 'expected_version': 99})
        self.assertEqual(r.status_code, 409)
        r = self.client.patch(f'/work/items/{item}', headers=self._auth(self.coord),
                              json={'title': '新标题', 'expected_version': 2})
        self.assertEqual(r.status_code, 200)
        # 普通成员不能编辑他人事项
        r = self.client.patch(f'/work/items/{item}', headers=self._auth(self.member1),
                              json={'title': '越权', 'expected_version': 3})
        self.assertEqual(r.status_code, 403)

    def test_patch_concurrent_edit_stale_version_rejected(self):
        """#1：并发 PATCH 模拟——双方基于同一 version 编辑，后写者必须 409
        并刷新后重试，先写者的修改不被静默覆盖（patch 路径行锁 + 乐观校验）。"""
        item = self._create_item(self.member1, self.ws_soft.id, title='初版')  # v2
        # 协调员与作者同时读到 v2；作者先提交（v2→v3）
        r_author = self.client.patch(f'/work/items/{item}', headers=self._auth(self.member1),
                                     json={'title': '作者的修改', 'expected_version': 2})
        self.assertEqual(r_author.status_code, 200)
        self.assertEqual(r_author.get_json()['data']['version'], 3)
        # 协调员持过期 v2 提交 → 409（不是覆盖成功的 200）
        r_coord = self.client.patch(f'/work/items/{item}', headers=self._auth(self.coord),
                                    json={'title': '协调员的修改', 'expected_version': 2})
        self.assertEqual(r_coord.status_code, 409)
        # 先写者的标题未被覆盖；协调员刷新后按新版本重试成功
        self.assertEqual(WorkItem.query.get(item).title, '作者的修改')
        r_coord2 = self.client.patch(f'/work/items/{item}', headers=self._auth(self.coord),
                                     json={'title': '协调员的修改', 'expected_version': 3})
        self.assertEqual(r_coord2.status_code, 200)
        self.assertEqual(WorkItem.query.get(item).title, '协调员的修改')
        self.assertEqual(WorkItem.query.get(item).version, 4)

    def test_int_query_params_rejected_400(self):
        """#5：非法 int 参数统一 400，不再 ValueError→500。"""
        item = self._create_item(self.coord, self.ws_soft.id)
        r = self.client.get('/work/items?page=abc', headers=self._auth(self.member1))
        self.assertEqual(r.status_code, 400)
        r = self.client.get('/work/items?page_size=abc', headers=self._auth(self.member1))
        self.assertEqual(r.status_code, 400)
        r = self.client.get(f'/work/items/{item}/replies?after_seq=abc',
                            headers=self._auth(self.member1))
        self.assertEqual(r.status_code, 400)
        r = self.client.get(f'/work/items/{item}/replies?limit=abc',
                            headers=self._auth(self.member1))
        self.assertEqual(r.status_code, 400)
        r = self.client.post(f'/work/items/{item}/read', headers=self._auth(self.member1),
                             json={'last_read_seq': 'abc'})
        self.assertEqual(r.status_code, 400)

    def test_search_wildcards_escaped(self):
        """#38：检索词中的 %/_ 按字面匹配（不再当通配符）。"""
        self._create_item(self.coord, self.ws_soft.id, title='进度100%')
        self._create_item(self.coord, self.ws_soft.id, title='进度100分')
        r = self.client.get('/work/items?q=100%25', headers=self._auth(self.member1))
        data = r.get_json()['data']
        self.assertEqual(data['total'], 1)
        self.assertEqual(data['items'][0]['title'], '进度100%')
        # 下划线同理：只命中字面
        self._create_item(self.coord, self.ws_soft.id, title='A_b')
        self._create_item(self.coord, self.ws_soft.id, title='Axb')
        r = self.client.get('/work/items?q=A_b', headers=self._auth(self.member1))
        titles = [i['title'] for i in r.get_json()['data']['items']]
        self.assertIn('A_b', titles)
        self.assertNotIn('Axb', titles)

    def test_clean_body_truncates_without_partial_tag(self):
        """#37：先截原始再清洗——超长正文不会切出残缺 HTML 标记。"""
        import re
        from services.work import items as items_service
        raw = '<p>安全段落</p>' * 6000                # 远超 20000 上限
        out = items_service.clean_body(raw)
        self.assertIsNotNone(out)
        self.assertLessEqual(len(out), items_service.MAX_BODY_LEN)
        self.assertIsNone(re.search(r'<[^>]*$', out))   # 尾部无未闭合片段
        # 脚本内容剥离（正文文本保留）；短正文原样保留
        self.assertEqual(items_service.clean_body('<script>alert(1)</script>你好'), '你好')
        self.assertEqual(items_service.clean_body('普通正文'), '普通正文')

    # ── 已读游标 ───────────────────────────────────────────

    def test_read_cursor_and_unread_flag(self):
        item = self._create_item(self.coord, self.ws_soft.id)
        self._reply(self.coord, item, body='r1', crid='c1')
        self._reply(self.coord, item, body='r2', crid='c2')
        r = self.client.get('/work/items', headers=self._auth(self.member1))
        self.assertTrue(r.get_json()['data']['items'][0]['unread'])
        # 游标不能越过最新（seq=2）
        r = self.client.post(f'/work/items/{item}/read', headers=self._auth(self.member1),
                             json={'last_read_seq': 3})
        self.assertEqual(r.status_code, 409)
        r = self.client.post(f'/work/items/{item}/read', headers=self._auth(self.member1),
                             json={'last_read_seq': 2})
        self.assertEqual(r.status_code, 200)
        r = self.client.get('/work/items', headers=self._auth(self.member1))
        self.assertFalse(r.get_json()['data']['items'][0]['unread'])

    def test_read_cursor_never_regresses(self):
        """#8：已读游标只进不退——低值重放不回退（条件更新语义）。"""
        item = self._create_item(self.coord, self.ws_soft.id)
        self._reply(self.coord, item, body='r1', crid='c-1')
        self._reply(self.coord, item, body='r2', crid='c-2')
        r = self.client.post(f'/work/items/{item}/read', headers=self._auth(self.member1),
                             json={'last_read_seq': 2})
        self.assertEqual(r.status_code, 200)
        # 并发/重试场景：旧值（1）后到，游标保持 2
        r = self.client.post(f'/work/items/{item}/read', headers=self._auth(self.member1),
                             json={'last_read_seq': 1})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()['data']['last_read_seq'], 2)

    # ── 待回应请求（§7.2） ─────────────────────────────────

    def test_response_request_flow(self):
        item = self._create_item(self.coord, self.ws_soft.id)
        r = self._reply(self.coord, item, body='请组员确认', crid='cr-rr',
                        response={'user_id': self.member1.id})
        self.assertEqual(r.status_code, 200, r.get_json())
        # 待回复桶真实
        r = self.client.get('/work/me', headers=self._auth(self.member1))
        self.assertEqual(r.get_json()['data']['todo']['pending_responses'], 1)
        r = self.client.get('/work/me/todos', headers=self._auth(self.member1))
        bucket = r.get_json()['data']['pending_responses']
        self.assertEqual(len(bucket), 1)
        self.assertEqual(bucket[0]['item_id'], item)
        # 组员回复（带 response_to_request_id）→ 请求完结
        req_id = bucket[0]['request_id']
        r = self._reply(self.member1, item, body='收到，确认', crid='cr-rr-2',
                        response_to_request_id=req_id)
        self.assertEqual(r.status_code, 200, r.get_json())
        r = self.client.get('/work/me', headers=self._auth(self.member1))
        self.assertEqual(r.get_json()['data']['todo']['pending_responses'], 0)
        # 无权者不能被指定回应（须先邀请）
        r = self._reply(self.coord, item, body='请局外人回应', crid='cr-rr-3',
                        response={'user_id': self.plain.id})
        self.assertEqual(r.status_code, 400)

    def test_response_due_reminder_lifecycle(self):
        """#40：带时限的回应请求生成 response_due 提醒，回应完结即取消。"""
        from models import WorkReminder
        item = self._create_item(self.coord, self.ws_soft.id)
        due = (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M')
        r = self._reply(self.coord, item, body='请明天前确认', crid='cr-rd-1',
                        response={'user_id': self.member1.id, 'due_at': due})
        self.assertEqual(r.status_code, 200, r.get_json())
        row = WorkReminder.query.filter_by(slot='response_due', status='pending').one()
        self.assertEqual(row.user_id, self.member1.id)
        self.assertEqual(row.item_id, item)
        req_id = row.object_version              # object_version = 回应请求 id
        # 回应 → 提醒取消
        r = self._reply(self.member1, item, body='已确认', crid='cr-rd-2',
                        response_to_request_id=req_id)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(WorkReminder.query.get(row.id).status, 'cancelled')
        # 无时限请求不生成提醒
        r = self._reply(self.coord, item, body='请尽快看一眼', crid='cr-rd-3',
                        response={'user_id': self.member1.id})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(WorkReminder.query.filter_by(
            slot='response_due', status='pending').count(), 0)

    def test_publish_topic_command_rejects_task_kind(self):
        """#35：服务层兜底——任务经 command_topic.publish 发布被拒（任务发布
        走 tasks._cmd_publish 的补齐前置检查）。"""
        from services.work import items as items_service
        from services.work.access import WorkApiError
        r = self.client.post('/work/items', headers=self._auth(self.coord), json={
            'workspace_id': self.ws_soft.id, 'kind': 'task', 'title': '草稿任务',
            'task': {'due_at': (date.today() + timedelta(days=3)).isoformat()},
            'idempotency_key': 'pub-guard-1'})
        task_id = r.get_json()['data']['id']
        with self.assertRaises(WorkApiError) as ctx:
            items_service.command_topic(self.coord, task_id, 'publish',
                                        {'expected_version': 1})
        self.assertEqual(ctx.exception.code, 409)
        self.assertEqual(WorkItem.query.get(task_id).status, 'draft')

    def test_due_at_date_only_unified_end_of_day(self):
        """#36：仅日期的 due_at 统一 23:59:59（草稿创建与改期同口径）。"""
        from models import WorkTask
        item_id, v = self._create_task(self.coord, self.member1, due_offset_days=5)
        task = WorkTask.query.filter_by(item_id=item_id).one()
        self.assertEqual((task.due_at.hour, task.due_at.minute, task.due_at.second),
                         (23, 59, 59))
        # 改期同样口径
        self._cmd(self.coord, item_id, 'reschedule', v, reason='统一口径',
                  due_at=(date.today() + timedelta(days=6)).isoformat())
        task = WorkTask.query.filter_by(item_id=item_id).one()
        self.assertEqual((task.due_at.hour, task.due_at.minute, task.due_at.second),
                         (23, 59, 59))

    def test_events_timeline_and_reply_reference(self):
        item = self._create_item(self.coord, self.ws_soft.id)
        r1 = self._reply(self.coord, item, body='第一条', crid='e1')
        seq1 = r1.get_json()['data']['seq']
        r2 = self._reply(self.member1, item, body='引用第一条', crid='e2',
                         reply_to_id=r1.get_json()['data']['id'])
        self.assertEqual(r2.status_code, 200)
        r = self.client.get(f'/work/items/{item}/events', headers=self._auth(self.member1))
        types = [e['event_type'] for e in r.get_json()['data']['events']]
        self.assertIn('created', types)
        self.assertIn('published', types)
        self.assertIn('replied', types)
        r = self.client.get(f'/work/items/{item}/replies?after_seq={seq1 - 1}',
                            headers=self._auth(self.member1))
        replies = r.get_json()['data']['replies']
        self.assertEqual(len(replies), 2)
        self.assertEqual(replies[1]['reply_to_id'], replies[0]['id'])


class WorkTasksTest(_WorkItemsBase):
    """M3 任务闭环/转交/提醒的隔离回归（B02/B03/B04/B06/C04/D01 标记）。"""

    def _task_row(self, item_id):
        return WorkTask.query.filter_by(item_id=item_id).first()

    # ── B04：话题转任务幂等 ────────────────────────────────

    def test_b04_promote_idempotent_keeps_id(self):
        topic = self._create_item(self.coord, self.ws_soft.id)
        r = self.client.post(f'/work/items/{topic}/commands', headers=self._auth(self.coord),
                             json={'command': 'promote', 'expected_version': 2,
                                   'assignee_id': self.member1.id,
                                   'due_at': (date.today() + timedelta(days=3)).isoformat()})
        assert r.status_code == 200, r.get_json()
        self.assertEqual(r.get_json()['data']['kind'], 'task')
        # 重复 promote（重试/重复点击）→ 幂等返回同一任务，无副本
        r2 = self.client.post(f'/work/items/{topic}/commands', headers=self._auth(self.coord),
                              json={'command': 'promote', 'expected_version': 99,
                                    'assignee_id': self.member1.id,
                                    'due_at': (date.today() + timedelta(days=3)).isoformat()})
        assert r2.status_code == 200, r2.get_json()
        self.assertIn('幂等', r2.get_json()['message'])
        self.assertEqual(WorkTask.query.filter_by(item_id=topic).count(), 1)
        item = WorkItem.query.get(topic)
        self.assertEqual((item.kind, item.status), ('task', 'todo'))
        self.assertIsNotNone(item.promoted_at)

    # ── 完整执行链：start→block→unblock→submit→review_accept ──

    def test_full_flow_with_review_and_reminders(self):
        from models import WorkReminder, WorkSubmission
        item_id, v = self._create_task(self.coord, self.member1, reviewer=self.coord)
        self._cmd(self.member1, item_id, 'start', v); v += 1
        self._cmd(self.member1, item_id, 'block', v,
                  blocker_reason='等设备到位', follow_up_at=(
                      datetime.now() + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M')); v += 1
        # 受阻跟进提醒已生成
        self.assertIsNotNone(WorkReminder.query.filter_by(
            item_id=item_id, slot='follow_up', status='pending').first())
        self._cmd(self.member1, item_id, 'unblock', v); v += 1
        self.assertIsNone(WorkReminder.query.filter_by(
            item_id=item_id, slot='follow_up', status='pending').first())
        self._cmd(self.member1, item_id, 'submit', v, result_note='初版完成'); v += 1
        self.assertEqual(WorkItem.query.get(item_id).status, 'review')
        sub = WorkSubmission.query.filter_by(item_id=item_id).one()
        self.assertIsNone(sub.decision)
        self._cmd(self.coord, item_id, 'review_accept', v, note='通过'); v += 1
        item = WorkItem.query.get(item_id)
        self.assertEqual(item.status, 'done')
        self.assertEqual(WorkSubmission.query.get(sub.id).decision, 'accepted')
        self.assertEqual(self._task_row(item_id).last_accepted_submission_id, sub.id)
        # 终态后提醒全取消
        self.assertEqual(WorkReminder.query.filter_by(item_id=item_id,
                                                      status='pending').count(), 0)

    def test_complete_without_reviewer(self):
        item_id, v = self._create_task(self.coord, self.member1, reviewer=None)
        self._cmd(self.member1, item_id, 'start', v); v += 1
        self._cmd(self.member1, item_id, 'complete', v, completion_note='事务性工作已完成'); v += 1
        self.assertEqual(WorkItem.query.get(item_id).status, 'done')

    def test_review_return_requires_note_then_reopen(self):
        item_id, v = self._create_task(self.coord, self.member1, reviewer=self.coord)
        self._cmd(self.member1, item_id, 'start', v); v += 1
        self._cmd(self.member1, item_id, 'submit', v, result_note='待审'); v += 1
        r = self._cmd(self.coord, item_id, 'review_return', v, expect=400); v += 0
        self._cmd(self.coord, item_id, 'review_return', v,
                  decision_note='第三章缺修订'); v += 1
        self.assertEqual(WorkItem.query.get(item_id).status, 'in_progress')
        # 已完成任务的重新打开（done→in_progress）
        item_id2, v2 = self._create_task(self.coord, self.member1, reviewer=None)
        self._cmd(self.member1, item_id2, 'start', v2); v2 += 1
        self._cmd(self.member1, item_id2, 'complete', v2, completion_note='ok'); v2 += 1
        self._cmd(self.coord, item_id2, 'reopen', v2, reason='发现遗漏'); v2 += 1
        self.assertEqual(WorkItem.query.get(item_id2).status, 'in_progress')

    # ── B02：并发版本冲突 ──────────────────────────────────

    def test_b02_version_conflict_on_command(self):
        item_id, v = self._create_task(self.coord, self.member1)
        # 版本冲突在状态变更前拦截（无脏状态遗留）
        self._cmd(self.member1, item_id, 'start', v + 99, expect=409)
        self._cmd(self.member1, item_id, 'start', v)
        self.assertEqual(WorkItem.query.get(item_id).status, 'in_progress')

    # ── B06：改期后旧提醒失效、新版本重建 ──────────────────

    def test_b06_reschedule_invalidates_old_reminders(self):
        from models import WorkReminder
        item_id, v = self._create_task(self.coord, self.member1,
                                       due_offset_days=10)
        old = WorkReminder.query.filter_by(item_id=item_id, status='pending').all()
        self.assertTrue(old)
        self._cmd(self.coord, item_id, 'reschedule', v, reason='设备延期',
                  due_at=(date.today() + timedelta(days=20)).isoformat()); v += 1
        for r in old:
            self.assertEqual(WorkReminder.query.get(r.id).status, 'cancelled')
        new = WorkReminder.query.filter_by(item_id=item_id, status='pending').all()
        self.assertTrue(new)
        self.assertTrue(all(r.object_version == v for r in new))
        # 非协调员不能改期
        self._cmd(self.member1, item_id, 'reschedule', v, reason='x',
                  due_at=(date.today() + timedelta(days=5)).isoformat(), expect=403)

    def test_b06_follow_up_survives_reschedule(self):
        """#11（B06 变体）：受阻任务改期后 follow_up 提醒存活（不再被版本
        bump 静默丢弃）。"""
        from models import WorkReminder
        item_id, v = self._create_task(self.coord, self.member1, due_offset_days=10)
        self._cmd(self.member1, item_id, 'start', v); v += 1
        follow_at = datetime.now() + timedelta(days=2)
        self._cmd(self.member1, item_id, 'block', v, blocker_reason='等外协到位',
                  follow_up_at=follow_at.strftime('%Y-%m-%dT%H:%M')); v += 1
        follow = WorkReminder.query.filter_by(
            item_id=item_id, slot='follow_up', status='pending').one()
        # 改期（版本 bump）后：follow_up 保留，due 提醒按新版本重建
        self._cmd(self.coord, item_id, 'reschedule', v, reason='整体延期',
                  due_at=(date.today() + timedelta(days=20)).isoformat()); v += 1
        after = WorkReminder.query.get(follow.id)
        self.assertEqual(after.status, 'pending')
        self.assertEqual(after.trigger_at, follow.trigger_at)   # 触发点不被重建改动
        self.assertTrue(WorkReminder.query.filter_by(
            item_id=item_id, slot='due_soon', status='pending',
            object_version=v).first())
        # unblock 仍正常取消 follow_up（生命周期归 block/unblock 管）
        self._cmd(self.member1, item_id, 'unblock', v)
        self.assertEqual(WorkReminder.query.get(follow.id).status, 'cancelled')

    # ── 组内直派 vs 跨组转交（§8.3/§8.4） ──────────────────

    def test_reassign_scope_guard(self):
        item_id, v = self._create_task(self.coord, self.member1)
        # 跨组目标（member2 无本工作区权限）→ 引导走转交确认
        self._cmd(self.coord, item_id, 'reassign', v, reason='x',
                  assignee_id=self.member2.id, expect=400)
        # 组内有权限目标（coord 自己）→ 直派成功
        self._cmd(self.coord, item_id, 'reassign', v, reason='组内调整',
                  assignee_id=self.coord.id); v += 1
        self.assertEqual(self._task_row(item_id).assignee_user_id, self.coord.id)

    def test_b03_transfer_flow(self):
        item_id, v = self._create_task(self.coord, self.member1)
        # 邀请 member2 参与（跨组参与单个事项，A04 口径），获得读权后可被转交
        self.client.post(f'/work/items/{item_id}/participants', headers=self._auth(self.coord),
                         json={'user_id': self.member2.id})
        # 发起转交
        r = self.client.post(f'/work/items/{item_id}/transfers', headers=self._auth(self.coord),
                             json={'to_user_id': self.member2.id, 'reason': '出差交接'})
        assert r.status_code == 200, r.get_json()
        tid = r.get_json()['data']['transfer_id']
        # 同事项第二个待确认 → 409
        r = self.client.post(f'/work/items/{item_id}/transfers', headers=self._auth(self.coord),
                             json={'to_user_id': self.member1.id})
        self.assertEqual(r.status_code, 409)
        # 待接手桶（member2）
        r = self.client.get('/work/me/todos', headers=self._auth(self.member2))
        self.assertEqual(len(r.get_json()['data']['pending_transfers']), 1)
        # 非目标本人不能接受
        r = self.client.post(f'/work/transfers/{tid}/accept', headers=self._auth(self.member1))
        self.assertEqual(r.status_code, 403)
        # 接受：原子替换负责人
        r = self.client.post(f'/work/transfers/{tid}/accept', headers=self._auth(self.member2))
        assert r.status_code == 200, r.get_json()
        self.assertEqual(self._task_row(item_id).assignee_user_id, self.member2.id)

    def test_b03_transfer_reject_and_expire_keep_assignee(self):
        from services.work import tasks as tasks_service
        item_id, v = self._create_task(self.coord, self.member1)
        self.client.post(f'/work/items/{item_id}/participants', headers=self._auth(self.coord),
                         json={'user_id': self.member2.id})
        r = self.client.post(f'/work/items/{item_id}/transfers', headers=self._auth(self.coord),
                             json={'to_user_id': self.member2.id,
                                   'expires_at': (datetime.now() - timedelta(hours=1)
                                                  ).strftime('%Y-%m-%dT%H:%M')})
        tid = r.get_json()['data']['transfer_id']
        # 过期后接受 → 409，负责人保持
        r = self.client.post(f'/work/transfers/{tid}/accept', headers=self._auth(self.member2))
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self._task_row(item_id).assignee_user_id, self.member1.id)
        # 调度器过期扫描 → expired，发起人被通知
        with self.app.test_request_context():
            from exts import db as _db
            n = tasks_service.expire_transfers()
            _db.session.commit()
        self.assertEqual(n, 1)
        # 拒绝路径
        item_id2, _v2 = self._create_task(self.coord, self.member1)
        self.client.post(f'/work/items/{item_id2}/participants', headers=self._auth(self.coord),
                         json={'user_id': self.member2.id})
        r = self.client.post(f'/work/items/{item_id2}/transfers', headers=self._auth(self.coord),
                             json={'to_user_id': self.member2.id})
        tid2 = r.get_json()['data']['transfer_id']
        r = self.client.post(f'/work/transfers/{tid2}/reject', headers=self._auth(self.member2))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._task_row(item_id2).assignee_user_id, self.member1.id)

    def test_b03_expire_never_overwrites_decided_transfer(self):
        """#12：调度器持旧快照时已决转交不被覆写——行锁下复查 status。"""
        from models import WorkTransferRequest
        from services.work import tasks as tasks_service
        item_id, _v = self._create_task(self.coord, self.member1)
        self.client.post(f'/work/items/{item_id}/participants', headers=self._auth(self.coord),
                         json={'user_id': self.member2.id})
        r = self.client.post(f'/work/items/{item_id}/transfers', headers=self._auth(self.coord),
                             json={'to_user_id': self.member2.id})
        tid = r.get_json()['data']['transfer_id']
        # 对方接受（负责人已换）
        r = self.client.post(f'/work/transfers/{tid}/accept', headers=self._auth(self.member2))
        self.assertEqual(r.status_code, 200)
        # 模拟扫描器读到 accept 前的旧快照：expires_at 已过、扫描窗口内提交完成
        WorkTransferRequest.query.filter_by(id=tid).update(
            {'expires_at': datetime.now() - timedelta(hours=1)})
        db.session.commit()
        with self.app.test_request_context():
            n = tasks_service.expire_transfers()
            db.session.commit()
        self.assertEqual(n, 0)                    # 已决（accepted）不复写
        self.assertEqual(WorkTransferRequest.query.get(tid).status, 'accepted')
        self.assertEqual(self._task_row(item_id).assignee_user_id, self.member2.id)

    # ── C04：提醒认领与恢复 ────────────────────────────────

    def test_c04_claim_send_and_stale_recovery(self):
        from models import NotificationModel, WorkReminder
        from services.work import reminders as reminders_service
        item_id, v = self._create_task(self.coord, self.member1, due_offset_days=2)
        # 提前触发：把 due_soon 拉到过去
        WorkReminder.query.filter_by(item_id=item_id).update(
            {'trigger_at': datetime.now() - timedelta(minutes=5)})
        db.session.commit()
        claimed = reminders_service.claim_due()
        self.assertEqual(len(claimed), 2)          # due_soon + overdue
        db.session.commit()
        db.session.expire_all()                    # claim 走条件 UPDATE，刷新内存态
        self.assertTrue(all(r.status == 'claimed' for r in
                            WorkReminder.query.filter_by(item_id=item_id).all()))
        # 发送 → sent + work 类通知（发布链路已有基线通知，此处断言增量）
        baseline = NotificationModel.query.filter_by(
            user_id=self.member1.id, category='work').count()
        for r in claimed:
            reminders_service.send_one(r)
        db.session.commit()
        after = NotificationModel.query.filter_by(
            user_id=self.member1.id, category='work').count()
        self.assertEqual(after - baseline, 2)
        # 认领超时回收：手工造一条 stale claimed
        stale = WorkReminder(item_id=item_id, user_id=self.member1.id, slot='follow_up',
                             object_version=v,
                             trigger_at=datetime.now() - timedelta(hours=1),
                             status='claimed',
                             claimed_at=datetime.now() - timedelta(hours=1))
        db.session.add(stale)
        db.session.commit()
        reminders_service.recover_stale_claims()
        db.session.commit()
        self.assertEqual(WorkReminder.query.get(stale.id).status, 'pending')

    # ── D01（标记段）：负责人失资格 → 需接管 ────────────────

    def test_d01_takeover_marking_on_revocation(self):
        from models import WorkAccessGrant
        from services.work import tasks as tasks_service
        item_id, _v = self._create_task(self.coord, self.member1)
        # 撤销 member1 授权 → 活跃任务进入需接管，历史操作者不变
        WorkAccessGrant.query.filter_by(user_id=self.member1.id) \
            .update({'status': 'revoked'})
        db.session.commit()
        rows = tasks_service.takeover_candidates()
        self.assertEqual([r['item_id'] for r in rows], [item_id])
        # 恢复授权避免影响后续
        WorkAccessGrant.query.filter_by(user_id=self.member1.id) \
            .update({'status': 'active'})
        db.session.commit()


class WorkFilesTest(_WorkItemsBase):
    """M4 私有文件链路的隔离回归（A07/B05/C01/C02/C03；内存假存储）。"""

    def setUp(self):
        super().setUp()
        import io as _io
        from unittest.mock import patch
        import storage as storage_module

        class _Stat:
            def __init__(self, size):
                self.size = size

        class _FakeStorage:
            def __init__(self):
                self.objects = {}

            def put_object(self, key, stream, length=None, content_type=None):
                self.objects[key] = stream.read()

            def stat_object(self, key):
                if key not in self.objects:
                    raise KeyError(key)
                return _Stat(len(self.objects[key]))

            def get_object(self, key, offset=None, length=None):
                data = self.objects[key]
                if offset is not None:
                    data = data[offset:offset + (length or len(data))]
                return _io.BytesIO(data)

            def remove_object(self, key):
                self.objects.pop(key, None)

        self.fake_storage = _FakeStorage()
        self._patcher = patch.object(storage_module, 'storage', self.fake_storage)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        super().tearDown()

    def _upload(self, user, item_id, filename, data, file_id=None, expect=200):
        from werkzeug.datastructures import FileStorage
        fs = FileStorage(stream=io.BytesIO(data), filename=filename,
                         content_type='text/plain' if filename.endswith(('.txt', '.md'))
                         else 'application/pdf' if filename.endswith('.pdf')
                         else 'image/png')
        r = self.client.post(f'/work/items/{item_id}/files', headers=self._auth(user),
                             data={'file': fs, **({'file_id': str(file_id)} if file_id else {})},
                             content_type='multipart/form-data')
        assert r.status_code == expect, (r.status_code, r.get_json())
        return r

    def _item_link(self, file_id):
        from models import WorkFileLink
        return WorkFileLink.query.filter_by(file_id=file_id, target_type='item').first()

    def test_upload_download_and_a07_isolation(self):
        from models import WorkFile
        item = self._create_item(self.coord, self.ws_soft.id, title='文件事项')
        self._upload(self.member1, item, '说明.txt', '文件内容 abc'.encode())
        wf = WorkFile.query.filter_by(item_id=item).one()
        self.assertEqual(wf.status, 'active')
        # 详情聚合附件
        r = self.client.get(f'/work/items/{item}', headers=self._auth(self.member1))
        self.assertEqual(len(r.get_json()['data']['files']), 1)
        # 鉴权下载：本人 200；无权者 404（A07：换账号即拒，与链接无关）
        link = self._item_link(wf.id)
        r = self.client.get(f'/work/files/{wf.id}/download?link_id={link.id}',
                            headers=self._auth(self.member1))
        self.assertEqual(r.status_code, 200)
        self.assertIn('文件内容', r.get_data(as_text=True))
        r = self.client.get(f'/work/files/{wf.id}/download?link_id={link.id}',
                            headers=self._auth(self.member2))
        self.assertEqual(r.status_code, 404)
        r = self.client.get(f'/work/files/{wf.id}/download?link_id=99999',
                            headers=self._auth(self.member1))
        self.assertEqual(r.status_code, 404)

    def test_c02_fake_extension_rejected_no_trace(self):
        from models import WorkFile
        item = self._create_item(self.coord, self.ws_soft.id)
        # 伪 txt 实为 PNG 二进制 → 415；不留行不留对象（无可下载半成品）
        r = self._upload(self.member1, item, '伪装.txt', b'\x89PNG\r\n\x1a\n' + b'x' * 32,
                         expect=415)
        self.assertTrue(r.get_json()['message'])     # 拒绝原因可见（内容与类型不符类）
        self.assertEqual(WorkFile.query.filter_by(item_id=item).count(), 0)
        self.assertEqual(len(self.fake_storage.objects), 0)
        # 白名单外扩展 → 415
        self._upload(self.member1, item, '工具.exe', b'MZ...', expect=415)

    def test_c01_size_and_quota_guards(self):
        from services.work import files as files_service
        item = self._create_item(self.coord, self.ws_soft.id)
        original = files_service.MAX_FILE_MB
        files_service.MAX_FILE_MB = 1
        try:
            # 真实大小超单文件上限（stat 复核拦截；留隔离记录、对象已清）
            self._upload(self.member1, item, '超大.txt', b'a' * (1024 * 1024 + 10), expect=413)
            self.assertEqual(len(self.fake_storage.objects), 0)
        finally:
            files_service.MAX_FILE_MB = original
        # 配额：单文件上限 1MB、事项配额 1MB，两份 600KB 相加超配 → 第二份被拒
        original_q = files_service.ITEM_QUOTA_MB
        files_service.ITEM_QUOTA_MB = 1
        try:
            files_service.MAX_FILE_MB = 1
            self._upload(self.member1, item, '第一份.txt', b'a' * (600 * 1024))
            self._upload(self.member1, item, '第二份.txt', b'b' * (600 * 1024), expect=413)
        finally:
            files_service.MAX_FILE_MB = original
            files_service.ITEM_QUOTA_MB = original_q

    def test_b05_submission_binds_fixed_version(self):
        from models import WorkFileLink, WorkFileVersion
        item_id, v = self._create_task(self.coord, self.member1, reviewer=self.coord)
        self._cmd(self.member1, item_id, 'start', v); v += 1
        r = self._upload(self.member1, item_id, '交付.txt', 'v1 内容'.encode())
        version1 = WorkFileVersion.query.get(r.get_json()['data']['version_id'])
        # 提交绑定 v1 固定版本
        r = self._cmd(self.member1, item_id, 'submit', v, result_note='首版交付',
                      file_version_ids=[version1.id]); v += 1
        submission_links = WorkFileLink.query.filter_by(
            target_type='submission', version_id=version1.id).all()
        self.assertEqual(len(submission_links), 1)
        # 上传 v2 后：提交关联仍是 v1（B05）
        self._upload(self.member1, item_id, '交付.txt', 'v2 内容'.encode(), file_id=r and version1.file_id)
        self.assertEqual(WorkFileLink.query.filter_by(
            target_type='submission').first().version_id, version1.id)
        # 验收通过不影响绑定
        self._cmd(self.coord, item_id, 'review_accept', v, note='通过')

    def test_c03_link_removal_keeps_version(self):
        from models import WorkFileLink, WorkFileVersion
        item = self._create_item(self.coord, self.ws_soft.id)
        self._upload(self.member1, item, '共享.txt', 'data'.encode())
        from models import WorkFile
        wf = WorkFile.query.filter_by(item_id=item).one()
        version = WorkFileVersion.query.get(wf.current_version_id)
        # 两个关联（事项附件 + 一个交付引用）指向同一版本；删其一版本仍在
        extra = WorkFileLink(file_id=wf.id, version_id=version.id,
                             target_type='submission', target_id=888,
                             purpose='submission_result', created_by=self.coord.id)
        db.session.add(extra)
        db.session.commit()
        db.session.delete(self._item_link(wf.id))
        db.session.commit()
        self.assertIsNotNone(WorkFileVersion.query.get(version.id))
        self.assertIn(version.object_key, self.fake_storage.objects)

    def test_files_search_index_access_filtered(self):
        item = self._create_item(self.coord, self.ws_soft.id, title='资料事项')
        self._upload(self.member1, item, '排期表.txt', 'x'.encode())
        r = self.client.get('/work/files?q=排期', headers=self._auth(self.member1))
        self.assertEqual(r.get_json()['data']['total'], 1)
        # 跨组检索零计数不泄露
        r = self.client.get('/work/files?q=排期', headers=self._auth(self.member2))
        self.assertEqual(r.get_json()['data']['total'], 0)

    def test_emergency_access_allows_file_download_logged(self):
        """#6：紧急介入者可下载该事项附件（下载同样留痕），未介入仍 404。"""
        from models import WorkEvent, WorkFile
        super_admin = UserModel(username='介入超管', email='em-super@t.dev', role='super_admin')
        super_admin.set_password('a' * 32)
        db.session.add(super_admin)
        db.session.commit()
        item = self._create_item(self.member1, self.ws_soft.id,
                                 visibility='participants', title='介入下载事项')
        self._upload(self.member1, item, '证据.txt', '介入内容'.encode())
        wf = WorkFile.query.filter_by(item_id=item).one()
        link = self._item_link(wf.id)
        # 未介入：治理身份也拿不到（404 统一形态）
        r = self.client.get(f'/work/files/{wf.id}/download?link_id={link.id}',
                            headers=self._auth(super_admin))
        self.assertEqual(r.status_code, 404)
        # 介入后：可下载 + 下载再留一条 emergency_access 事件
        r = self.client.post('/work/governance/emergency-access',
                             headers=self._auth(super_admin),
                             json={'item_id': item, 'reason': '审计核查'})
        self.assertEqual(r.status_code, 200, r.get_json())
        r = self.client.get(f'/work/files/{wf.id}/download?link_id={link.id}',
                            headers=self._auth(super_admin))
        self.assertEqual(r.status_code, 200)
        self.assertIn('介入内容', r.get_data(as_text=True))
        em_events = WorkEvent.query.filter_by(
            item_id=item, event_type='emergency_access',
            actor_user_id=super_admin.id).all()
        self.assertEqual(len(em_events), 2)      # 介入读取 + 介入下载各一条
        # 非治理用户即使有历史介入者也无效（member2 无介入记录）
        r = self.client.get(f'/work/files/{wf.id}/download?link_id={link.id}',
                            headers=self._auth(self.member2))
        self.assertEqual(r.status_code, 404)

    def test_quarantined_rejection_persisted_for_audit(self):
        """#39：超限拒绝先落隔离记录再报错（DB 留审计痕迹，对象已清）。"""
        from models import WorkFile
        from services.work import files as files_service
        item = self._create_item(self.coord, self.ws_soft.id)
        original = files_service.MAX_FILE_MB
        files_service.MAX_FILE_MB = 1
        try:
            self._upload(self.member1, item, '超大.txt', b'a' * (1024 * 1024 + 10), expect=413)
        finally:
            files_service.MAX_FILE_MB = original
        row = WorkFile.query.filter_by(item_id=item).one()
        self.assertEqual(row.status, 'quarantined')      # 隔离记录已提交
        self.assertEqual(len(self.fake_storage.objects), 0)
        # 隔离文件不进附件列表
        r = self.client.get(f'/work/items/{item}', headers=self._auth(self.member1))
        self.assertEqual(r.get_json()['data']['files'], [])

    def test_quarantine_bad_new_version_keeps_old_current(self):
        """#39 细化：既有文件的坏新版本只隔离自身——文件保持 active、
        旧版本仍可下载，同时留版本行审计痕迹。"""
        from models import WorkFile, WorkFileVersion
        from services.work import files as files_service
        item = self._create_item(self.coord, self.ws_soft.id)
        r = self._upload(self.member1, item, '迭代.txt', 'v1'.encode())
        file_id = r.get_json()['data']['file_id']
        v1_id = r.get_json()['data']['version_id']
        link = self._item_link(file_id)
        original = files_service.MAX_FILE_MB
        files_service.MAX_FILE_MB = 1
        try:
            self._upload(self.member1, item, '迭代.txt', b'x' * (1024 * 1024 + 10),
                         file_id=file_id, expect=413)
        finally:
            files_service.MAX_FILE_MB = original
        wf = WorkFile.query.get(file_id)
        self.assertEqual(wf.status, 'active')              # 文件未被毁
        self.assertEqual(wf.current_version_id, v1_id)     # current 仍指 v1
        failed = WorkFileVersion.query.filter_by(file_id=file_id,
                                                 format_check='failed').one()
        self.assertEqual(failed.version_no, 2)             # 坏版本留痕
        self.assertNotIn(failed.object_key, self.fake_storage.objects)
        r = self.client.get(f'/work/files/{file_id}/download?link_id={link.id}',
                            headers=self._auth(self.member1))
        self.assertEqual(r.status_code, 200)
        self.assertIn('v1', r.get_data(as_text=True))

    def test_quota_counts_current_version_only(self):
        """#4：配额按各文件 current 版本口径——同文件传新版本不双计旧版。"""
        from models import WorkFile
        from services.work import files as files_service
        item = self._create_item(self.coord, self.ws_soft.id)
        originals = (files_service.MAX_FILE_MB, files_service.ITEM_QUOTA_MB)
        files_service.MAX_FILE_MB = 1
        files_service.ITEM_QUOTA_MB = 1
        try:
            r = self._upload(self.member1, item, '大文件.txt', b'a' * (700 * 1024))
            file_id = r.get_json()['data']['file_id']
            # 同文件 v2（500KB）：旧 current 700KB 不再计入 → 700+500 旧口径会拒，
            # current 口径只算替换后的占用 → 通过
            self._upload(self.member1, item, '大文件.txt', b'b' * (500 * 1024),
                         file_id=file_id)
            self.assertEqual(WorkFile.query.get(file_id).status, 'active')
            # 另一新文件 600KB：current 合计 500+600 > 1MB → 拒
            self._upload(self.member1, item, '再来.txt', b'c' * (600 * 1024), expect=413)
        finally:
            files_service.MAX_FILE_MB, files_service.ITEM_QUOTA_MB = originals


class WorkGovernanceTest(_WorkItemsBase):
    """M5 业务关联投影/交接清单/紧急介入/归档门禁的隔离回归。"""

    def setUp(self):
        super().setUp()
        self.app.register_blueprint(club_admin_module.bp)
        self.super_admin = UserModel(username='超管', email='super@t.dev', role='super_admin')
        self.super_admin.set_password('a' * 32)
        db.session.add(self.super_admin)
        from models import CourseModel
        self.course = CourseModel(title='解剖学入门', introduction='课程介绍',
                                  status=CourseModel.STATUS_NORMAL)
        db.session.add(self.course)
        from models import CampSession
        self.session = CampSession(name='秋季培训营', category='learning', status='running',
                                   start_date=date.today() - timedelta(days=1),
                                   end_date=date.today() + timedelta(days=30))
        db.session.add(self.session)
        db.session.commit()

    def test_d03_business_link_whitelist_and_projection(self):
        item = self._create_item(self.coord, self.ws_soft.id, title='关联事项')
        # 白名单外 / 不存在 / 重复
        r = self.client.post(f'/work/items/{item}/links', headers=self._auth(self.coord),
                             json={'source_type': 'arbitrary_table', 'source_id': 1})
        self.assertEqual(r.status_code, 400)
        r = self.client.post(f'/work/items/{item}/links', headers=self._auth(self.coord),
                             json={'source_type': 'course', 'source_id': 99999})
        self.assertEqual(r.status_code, 404)
        # 建立课程 + 营期关联
        self.client.post(f'/work/items/{item}/links', headers=self._auth(self.coord),
                         json={'source_type': 'course', 'source_id': self.course.id})
        self.client.post(f'/work/items/{item}/links', headers=self._auth(self.coord),
                         json={'source_type': 'camp_session', 'source_id': self.session.id})
        r = self.client.post(f'/work/items/{item}/links', headers=self._auth(self.coord),
                             json={'source_type': 'course', 'source_id': self.course.id})
        self.assertEqual(r.status_code, 409)
        # 投影：课程标题对成员可见；营期对非成员隐藏（不泄露存在性语义）
        r = self.client.get(f'/work/items/{item}', headers=self._auth(self.member1))
        links = r.get_json()['data']['business_links']
        by_type = {l['source_type']: l for l in links}
        # X2 新投影结构：label + fields 字典（盲渲染）；不可访问无 fields
        self.assertEqual(by_type['course']['fields']['title'], '解剖学入门')
        self.assertEqual(by_type['course']['label'], '课程')
        self.assertTrue(by_type['course']['accessible'])
        self.assertFalse(by_type['camp_session']['accessible'])
        self.assertIsNone(by_type['camp_session']['fields'])
        # 营期成员可见标题
        from models import CampMember
        db.session.add(CampMember(camp_session_id=self.session.id,
                                  user_id=self.member1.id, role='student'))
        db.session.commit()
        r = self.client.get(f'/work/items/{item}', headers=self._auth(self.member1))
        by_type = {l['source_type']: l for l in r.get_json()['data']['business_links']}
        self.assertTrue(by_type['camp_session']['accessible'])
        self.assertIn('title', by_type['camp_session']['fields'])
        # 普通成员不能建关联
        r = self.client.post(f'/work/items/{item}/links', headers=self._auth(self.member1),
                             json={'source_type': 'course', 'source_id': self.course.id})
        self.assertEqual(r.status_code, 403)

    def test_handover_overview(self):
        item_id, _v = self._create_task(self.coord, self.member1, reviewer=self.coord)
        topic = self._create_item(self.member1, self.ws_soft.id, title='待回复话题')
        self._reply(self.coord, topic, body='请组员确认', crid='ho-1',
                    response={'user_id': self.member1.id})
        r = self.client.get(f'/work/governance/handover?user_id={self.member1.id}',
                            headers=self._auth(self.super_admin))
        data = r.get_json()['data']
        self.assertEqual(data['counts']['unfinished'], 1)      # member1 负责的活跃任务
        self.assertEqual(data['counts']['to_review'], 0)       # coord 是验收人不是 member1
        self.assertEqual(data['counts']['pending_responses'], 1)
        self.assertEqual(len(data['grants']), 1)               # member1 的授权行
        # 非治理人员 403
        r = self.client.get(f'/work/governance/handover?user_id={self.member1.id}',
                            headers=self._auth(self.member1))
        self.assertEqual(r.status_code, 403)

    def test_governance_int_params_rejected_400(self):
        """#5：治理接口非法 int 参数统一 400（不再 ValueError→500）。"""
        r = self.client.get('/work/governance/grants?user_id=abc',
                            headers=self._auth(self.super_admin))
        self.assertEqual(r.status_code, 400)
        r = self.client.get('/work/governance/grants?workspace_id=abc',
                            headers=self._auth(self.super_admin))
        self.assertEqual(r.status_code, 400)
        r = self.client.get('/work/governance/grants?page=abc',
                            headers=self._auth(self.super_admin))
        self.assertEqual(r.status_code, 400)
        r = self.client.get('/work/governance/handover?user_id=abc',
                            headers=self._auth(self.super_admin))
        self.assertEqual(r.status_code, 400)

    def test_todo_counts_align_with_todos_after_revocation(self):
        """#34：撤权后摘要计数与待办列表同口径——徽标数不大于列表条数。"""
        from models import WorkAccessGrant
        item = self._create_item(self.coord, self.ws_soft.id, title='待回复口径')
        self._reply(self.coord, item, body='请确认', crid='align-1',
                    response={'user_id': self.member1.id})
        r = self.client.get('/work/me', headers=self._auth(self.member1))
        self.assertEqual(r.get_json()['data']['todo']['pending_responses'], 1)
        r = self.client.get('/work/me/todos', headers=self._auth(self.member1))
        self.assertEqual(len(r.get_json()['data']['pending_responses']), 1)
        # 撤权：计数与列表同时归零（旧口径计数仍为 1 → 口径不一致）
        WorkAccessGrant.query.filter_by(user_id=self.member1.id) \
            .update({'status': 'revoked'})
        db.session.commit()
        r = self.client.get('/work/me', headers=self._auth(self.member1))
        self.assertEqual(r.get_json()['data']['todo']['pending_responses'], 0)
        r = self.client.get('/work/me/todos', headers=self._auth(self.member1))
        self.assertEqual(r.get_json()['data']['pending_responses'], [])

    def test_a03_emergency_access_logged(self):
        """A03 介入段：协调员读受限话题 404 → 治理紧急介入可读且留痕。"""
        restricted = self._create_item(self.member1, self.ws_soft.id,
                                       visibility='participants', title='受限测试')
        r = self.client.get(f'/work/items/{restricted}', headers=self._auth(self.coord))
        self.assertEqual(r.status_code, 404)
        # 无理由被拒
        r = self.client.post('/work/governance/emergency-access',
                             headers=self._auth(self.super_admin),
                             json={'item_id': restricted, 'reason': ''})
        self.assertEqual(r.status_code, 400)
        # 介入成功：可读正文 + emergency_access 事件进时间线
        r = self.client.post('/work/governance/emergency-access',
                             headers=self._auth(self.super_admin),
                             json={'item_id': restricted, 'reason': '投诉核查'})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()['data']['title'], '受限测试')
        # 介入事件属治理类：普通参与者时间线不可见，协调员/治理可见（§13 按可见范围过滤）
        r = self.client.get(f'/work/items/{restricted}/events', headers=self._auth(self.member1))
        self.assertNotIn('emergency_access',
                         [e['event_type'] for e in r.get_json()['data']['events']])
        r = self.client.get(f'/work/items/{restricted}/events',
                            headers=self._auth(self.super_admin))
        self.assertIn('emergency_access',
                      [e['event_type'] for e in r.get_json()['data']['events']])

    def test_d04_archive_guarded_by_open_items(self):
        g2 = ClubGroup(name='独立测试组')
        db.session.add(g2)
        db.session.flush()
        from models import WorkWorkspace
        ws2 = WorkWorkspace(club_group_id=g2.id, status='active')
        db.session.add(ws2)
        db.session.flush()
        # member1 次要槽归属 g2 + 授权 → 建并发布事项
        from models import ClubMembership as _CM
        m1b = _CM(user_id=self.member1.id, group_id=g2.id, slot='secondary',
                  joined_at=date.today())
        db.session.add(m1b)
        db.session.flush()
        r = self.client.post('/work/governance/grants', headers=self._auth(self.super_admin),
                             json={'user_id': self.member1.id, 'role': 'member',
                                   'workspace_id': ws2.id, 'source_type': 'membership',
                                   'source_id': m1b.id, 'grant_reason': '归档门禁测试'})
        assert r.status_code == 200, r.get_json()
        r = self.client.post('/work/items', headers=self._auth(self.member1), json={
            'workspace_id': ws2.id, 'kind': 'topic', 'title': '未完结事项',
            'idempotency_key': 'ag-1'})
        assert r.status_code == 200, r.get_json()
        item_id = r.get_json()['data']['id']
        self.client.post(f'/work/items/{item_id}/commands', headers=self._auth(self.member1),
                         json={'command': 'publish', 'expected_version': 1})
        db.session.commit()
        # 清空归属/授权（满足归档的人员前置），事项仍活跃
        _CM.query.filter_by(id=m1b.id).delete()
        from models import WorkAccessGrant as _WAG
        _WAG.query.filter_by(workspace_id=ws2.id).delete()
        db.session.commit()
        # 有未完成事项 → 归档被「未完结工作事项」拦截
        r = self.client.post(f'/admin/club/groups/{g2.id}/archive',
                             headers=self._auth(self.super_admin))
        self.assertEqual(r.status_code, 409, r.get_json())
        self.assertIn('未完成', r.get_json()['message'])
        # 事项终态后可归档（门禁只看状态集合）
        WorkItem.query.filter_by(id=item_id).update({'status': 'closed'})
        db.session.commit()
        r = self.client.post(f'/admin/club/groups/{g2.id}/archive',
                             headers=self._auth(self.super_admin))
        self.assertEqual(r.status_code, 200, r.get_json())



class WorkCrossGroupTest(_WorkItemsBase):
    """X1 跨组协作：摘要层（subtree）与交付闭环。

    fixture 在基类之上加一层父子组：父组（培训组）> 软件组（子），
    硬件组仍为独立组（跨顶级交付对象）。协调员在父组持 subtree 授权。
    """

    def setUp(self):
        super().setUp()
        # 假存储（交付包文件测试用；照 WorkFilesTest）
        import io as _io
        from unittest.mock import patch
        import storage as storage_module

        class _Stat:
            def __init__(self, size):
                self.size = size

        class _FakeStorage:
            def __init__(self):
                self.objects = {}

            def put_object(self, key, stream, length=None, content_type=None):
                self.objects[key] = stream.read()

            def stat_object(self, key):
                if key not in self.objects:
                    raise KeyError(key)
                return _Stat(len(self.objects[key]))

            def get_object(self, key, offset=None, length=None):
                data = self.objects[key]
                if offset is not None:
                    data = data[offset:offset + (length or len(data))]
                return _io.BytesIO(data)

            def remove_object(self, key):
                self.objects.pop(key, None)

        self.fake_storage = _FakeStorage()
        self._patcher = patch.object(storage_module, 'storage', self.fake_storage)
        self._patcher.start()
        # 父组（培训组）+ 工作区；软件组挂为其子组
        self.g_parent = ClubGroup(name='培训组', parent_id=None)
        db.session.add(self.g_parent)
        db.session.flush()
        self.g_hard.parent_id = self.g_parent.id     # 硬件组挂为培训组子组：
        # coord 对硬件组无直接授权——正好验证「摘要可见、正文仍拒」
        self.ws_parent = WorkWorkspace(club_group_id=self.g_parent.id, status='active')
        db.session.add(self.ws_parent)
        db.session.flush()
        # 父组协调员授权（membership 来源）+ subtree
        # coord 基类 fixture 已占 primary（软件组），父组走 secondary 槽
        m_parent = ClubMembership(user_id=self.coord.id, group_id=self.g_parent.id,
                                  slot='secondary', joined_at=date.today())
        db.session.add(m_parent)
        db.session.flush()
        self.subtree_grant = WorkAccessGrant(
            user_id=self.coord.id, role='coordinator', workspace_id=self.ws_parent.id,
            source_type='membership', source_id=m_parent.id,
            group_id_snapshot=self.g_parent.id, subtree=True,
            granted_by=self.super_admin().id, grant_reason='t')
        db.session.add(self.subtree_grant)
        db.session.commit()

    def tearDown(self):
        self._patcher.stop()
        super().tearDown()

    def super_admin(self):
        u = UserModel.query.filter_by(role='super_admin').first()
        if not u:
            u = UserModel(username='超管', email='cg-super@t.dev', role='super_admin')
            u.set_password('a' * 32)
            db.session.add(u)
            db.session.commit()
        return u

    # ── 摘要层 ────────────────────────────────────────────

    def test_summary_tier_visible_but_detail_blocked(self):
        # 硬件组（子组，coord 无直接授权）建活跃任务；摘要可见、详情 404
        item_id, _v = self._create_task(self.member2, self.member2, ws_id=self.ws_hard.id)
        r = self.client.get('/work/items?rollup=subtree', headers=self._auth(self.coord))
        self.assertEqual(r.status_code, 200, r.get_json())
        data = r.get_json()['data']
        self.assertGreaterEqual(data['total'], 1)
        row = next(x for x in data['items'] if x['id'] == item_id)
        self.assertEqual(row['group_name'], '硬件组')
        self.assertIn('title', row)                     # 摘要有标题
        r = self.client.get(f'/work/items/{item_id}', headers=self._auth(self.coord))
        self.assertEqual(r.status_code, 404)            # 正文仍拒

    def test_summary_restricted_placeholder_no_title_leak(self):
        # 受限事项在摘要层只显示占位，不泄露标题
        item = self._create_item(self.member2, self.ws_hard.id,
                                 visibility='participants', title='绝密排期')
        r = self.client.get('/work/items?rollup=subtree', headers=self._auth(self.coord))
        rows = {x['id']: x for x in r.get_json()['data']['items']}
        self.assertIn(item, rows)
        self.assertTrue(rows[item]['restricted'])
        self.assertNotIn('title', rows[item])

    def test_summary_requires_subtree_grant(self):
        r = self.client.get('/work/items?rollup=subtree', headers=self._auth(self.member1))
        self.assertEqual(r.status_code, 403)

    # ── 交付闭环 ──────────────────────────────────────────

    def _offer(self, actor, item_id, to_ws, kind='上架', **extra):
        payload = {'to_workspace_id': to_ws.id, 'kind': kind,
                   'note': '请上架该课程', **extra}
        return self.client.post(f'/work/items/{item_id}/handoffs',
                                headers=self._auth(actor), json=payload)

    def test_handoff_full_cycle_with_backfill(self):
        from models import WorkBusinessLink, WorkHandoff, NotificationModel
        item_id, _v = self._create_task(self.member1, self.member1)
        # 关联课程上下文（接单后应复制到目标任务）
        db.session.add(WorkBusinessLink(item_id=item_id, source_type='course',
                                        source_id=12, created_by=self.member1.id))
        db.session.commit()
        # 局外人不能发起；作者（member1 建的任务本人即作者）与协调员都可以
        r = self._offer(self.plain, item_id, self.ws_hard)
        self.assertEqual(r.status_code, 404)          # 无读取权统一 404
        r = self._offer(self.member1, item_id, self.ws_hard)
        self.assertEqual(r.status_code, 200)
        hid_author = r.get_json()['data']['handoff_id']
        self.client.post(f'/work/handoffs/{hid_author}/withdraw',
                         headers=self._auth(self.member1))
        r = self._offer(self.coord, item_id, self.ws_hard)
        self.assertEqual(r.status_code, 200, r.get_json())
        hid = r.get_json()['data']['handoff_id']

        # 在途唯一：同用途再发 409；不同用途可并行
        r = self._offer(self.coord, item_id, self.ws_hard, kind='上架')
        self.assertEqual(r.status_code, 409)
        r = self._offer(self.coord, item_id, self.ws_hard, kind='审核')
        self.assertEqual(r.status_code, 200)

        # 硬件组协调员（member2 是 member——用治理直接给 member2 升协调员）
        WorkAccessGrant.query.filter_by(user_id=self.member2.id).update(
            {'role': 'coordinator'})
        db.session.commit()
        # 待接单桶
        r = self.client.get('/work/me/todos', headers=self._auth(self.member2))
        bucket = r.get_json()['data']['handoffs']
        self.assertEqual(len(bucket), 2)
        # 非目标组协调员不能接单（无目标工作区权限 → 统一 404 防探测）
        r = self.client.post(f'/work/handoffs/{hid}/accept', headers=self._auth(self.coord))
        self.assertEqual(r.status_code, 404)
        # 接单：目标组生成关联任务 + 业务关联复制
        r = self.client.post(f'/work/handoffs/{hid}/accept', headers=self._auth(self.member2))
        self.assertEqual(r.status_code, 200, r.get_json())
        target_id = r.get_json()['data']['accepted_item_id']
        self.assertIsNotNone(target_id)
        self.assertIsNotNone(WorkBusinessLink.query.filter_by(
            item_id=target_id, source_type='course', source_id=12).first())
        # 目标任务开始并完成 → 回填源事项
        db.session.expire_all()
        v = WorkItem.query.get(target_id).version
        self._cmd(self.member2, target_id, 'start', v)
        db.session.expire_all()
        r = self._cmd(self.member2, target_id, 'complete', WorkItem.query.get(target_id).version,
                      completion_note='已上架 course#12')
        self.assertEqual(r.status_code, 200, r.get_json())
        db.session.expire_all()
        self.assertEqual(WorkHandoff.query.get(hid).status, 'done')
        self.assertIn('已上架', WorkHandoff.query.get(hid).result_note)
        # 源事项有 handoff_completed 事件
        r = self.client.get(f'/work/items/{item_id}/events', headers=self._auth(self.member1))
        types = [e['event_type'] for e in r.get_json()['data']['events']]
        self.assertIn('handoff_completed', types)

    def test_handoff_decline_and_expiry(self):
        from models import WorkHandoff
        item_id, _v = self._create_task(self.member1, self.member1)
        r = self._offer(self.coord, item_id, self.ws_hard)
        hid = r.get_json()['data']['handoff_id']
        # member2 升协调员后拒绝（原因必填）
        WorkAccessGrant.query.filter_by(user_id=self.member2.id).update(
            {'role': 'coordinator'})
        db.session.commit()
        r = self.client.post(f'/work/handoffs/{hid}/decline',
                             headers=self._auth(self.member2), json={})
        self.assertEqual(r.status_code, 400)
        r = self.client.post(f'/work/handoffs/{hid}/decline',
                             headers=self._auth(self.member2),
                             json={'reason': '本周发版窗口已满'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(WorkHandoff.query.get(hid).status, 'declined')
        # 拒绝后可重新发起（终态不占在途唯一）
        r = self._offer(self.coord, item_id, self.ws_hard)
        self.assertEqual(r.status_code, 200)
        hid2 = r.get_json()['data']['handoff_id']
        # 过期扫描
        WorkHandoff.query.filter_by(id=hid2).update(
            {'deadline': datetime.now() - timedelta(hours=1)})
        db.session.commit()
        from services.work import handoffs as handoffs_service
        with self.app.test_request_context():
            n = handoffs_service.expire_handoffs()
            db.session.commit()
        self.assertEqual(n, 1)
        self.assertEqual(WorkHandoff.query.get(hid2).status, 'expired')

    def test_handoff_payload_file_target_group_download(self):
        from models import WorkFileLink
        from services.work import files as files_service
        from werkzeug.datastructures import FileStorage
        item_id, v = self._create_task(self.member1, self.member1)
        # 上传附件（member1 是软件组成员可传）
        fs = FileStorage(stream=io.BytesIO('课程包内容'.encode()), filename='课程包.txt',
                         content_type='text/plain')
        r = self.client.post(f'/work/items/{item_id}/files', headers=self._auth(self.member1),
                             data={'file': fs}, content_type='multipart/form-data')
        self.assertEqual(r.status_code, 200, r.get_json())
        version_id = r.get_json()['data']['version_id']
        # 协调员交付并绑定该版本
        r = self._offer(self.coord, item_id, self.ws_hard,
                        file_version_ids=[version_id])
        self.assertEqual(r.status_code, 200, r.get_json())
        link = WorkFileLink.query.filter_by(target_type='handoff').first()
        self.assertIsNotNone(link)
        # offered 未接单：目标组成员尚不可下载
        file_id = link.file_id
        r = self.client.get(f'/work/files/{file_id}/download?link_id={link.id}',
                            headers=self._auth(self.member2))
        self.assertEqual(r.status_code, 404)
        # 接单后可下载
        WorkAccessGrant.query.filter_by(user_id=self.member2.id).update(
            {'role': 'coordinator'})
        db.session.commit()
        hid = link.target_id
        self.client.post(f'/work/handoffs/{hid}/accept', headers=self._auth(self.member2))
        r = self.client.get(f'/work/files/{file_id}/download?link_id={link.id}',
                            headers=self._auth(self.member2))
        self.assertEqual(r.status_code, 200)
        self.assertIn('课程包内容', r.get_data(as_text=True))
        # 无关人员（局外 plain）不可下载
        r = self.client.get(f'/work/files/{file_id}/download?link_id={link.id}',
                            headers=self._auth(self.plain))
        self.assertEqual(r.status_code, 404)

    def test_candidates_carry_identity(self):
        r = self.client.get('/work/candidates', headers=self._auth(self.member1))
        rows = r.get_json()['data']['candidates']
        self.assertTrue(rows)
        coord_row = next(x for x in rows if x['user_id'] == self.coord.id)
        self.assertEqual(coord_row['group_name'], '软件组')  # primary 槽优先
        # title 仅在任干事有（coord 走 membership 授权，无任职行 → None 合法）
        self.assertIn('title', coord_row)


class WorkProjectionTest(_WorkItemsBase):
    """X2 通用投影架构：注册表 / 适配器口径 / 看板聚合 / 认领。"""

    def setUp(self):
        super().setUp()
        from models import CourseModel, CampSession, CampMember
        from services.work import projections
        import services.work.adapters  # noqa: F401  组装
        self.projections = projections
        self.course = CourseModel(title='解剖学入门', introduction='x',
                                  status=CourseModel.STATUS_NORMAL)
        self.course_deleted = CourseModel(title='已删课程', introduction='x',
                                          status=CourseModel.STATUS_DELETED)
        db.session.add_all([self.course, self.course_deleted])
        self.session = CampSession(name='投影测试营', category='learning', status='running',
                                   start_date=date.today() - timedelta(days=1),
                                   end_date=date.today() + timedelta(days=30))
        db.session.add(self.session)
        db.session.commit()

    def test_registry_and_duplicate(self):
        from services.work.projections import WorkProjectionProvider, register_projection
        self.assertIn('course', self.projections.registered_types())
        class Dup(WorkProjectionProvider):
            source_type = 'course'
            label = '冲突'
            def exists(self, sid): return True
            def summarize(self, user, sid): return None
        with self.assertRaises(ValueError):
            register_projection(Dup())

    def test_adapters_permission_matrix(self):
        course_p = self.projections.get_provider('course')
        camp_p = self.projections.get_provider('camp_session')
        # 课程：公开目录（登录可见）；删除课程不可访问
        self.assertTrue(course_p.stewardable)
        self.assertEqual(course_p.summarize(self.plain, self.course.id)['title'], '解剖学入门')
        self.assertFalse(course_p.exists(self.course_deleted.id))
        self.assertIsNone(course_p.summarize(self.plain, self.course_deleted.id))
        # 营期：非成员不可访问；成员可见
        self.assertFalse(camp_p.stewardable)
        self.assertIsNone(camp_p.summarize(self.plain, self.session.id))
        from models import CampMember
        db.session.add(CampMember(camp_session_id=self.session.id,
                                  user_id=self.plain.id, role='student'))
        db.session.commit()
        fields = camp_p.summarize(self.plain, self.session.id)
        self.assertEqual(fields['title'], '投影测试营')

    def test_link_whitelist_follows_registry(self):
        item = self._create_item(self.coord, self.ws_soft.id)
        r = self.client.post(f'/work/items/{item}/links', headers=self._auth(self.coord),
                             json={'source_type': 'nonexistent_domain', 'source_id': 1})
        self.assertEqual(r.status_code, 400)
        r = self.client.post(f'/work/items/{item}/links', headers=self._auth(self.coord),
                             json={'source_type': 'course', 'source_id': 99999})
        self.assertEqual(r.status_code, 404)

    def test_board_aggregation_and_claim(self):
        item_id, _v = self._create_task(self.member1, self.member1,
                                        due_offset_days=-1)      # 已逾期
        self._cmd(self.member1, item_id, 'start', 2)
        db.session.commit()
        r = self.client.post(f'/work/items/{item_id}/links', headers=self._auth(self.coord),
                             json={'source_type': 'course', 'source_id': self.course.id})
        self.assertEqual(r.status_code, 200)
        # 看板：未认领行 + 态势（active=1, overdue=1）
        r = self.client.get(f'/work/objects/board?ws={self.ws_soft.id}',
                            headers=self._auth(self.member1))
        board = r.get_json()['data']['objects']
        row = next(x for x in board if x['source_id'] == self.course.id)
        self.assertFalse(row['claimed'])
        self.assertEqual(row['active_items'], 1)
        self.assertEqual(row['overdue_items'], 1)
        self.assertEqual(row['fields']['title'], '解剖学入门')
        # 普通成员不能认领；协调员认领幂等
        r = self.client.post('/work/objects/claim', headers=self._auth(self.member1),
                             json={'source_type': 'course', 'source_id': self.course.id,
                                   'ws_id': self.ws_soft.id, 'action': 'claim'})
        self.assertEqual(r.status_code, 403)
        for expect_changed in (True, False):
            r = self.client.post('/work/objects/claim', headers=self._auth(self.coord),
                                 json={'source_type': 'course', 'source_id': self.course.id,
                                       'ws_id': self.ws_soft.id, 'action': 'claim'})
            self.assertEqual(r.status_code, 200)
        r = self.client.get(f'/work/objects/board?ws={self.ws_soft.id}',
                            headers=self._auth(self.member1))
        row = next(x for x in r.get_json()['data']['objects'] if x['source_id'] == self.course.id)
        self.assertTrue(row['claimed'])
        # 非 stewardable 类型（营期）不能认领
        r = self.client.post('/work/objects/claim', headers=self._auth(self.coord),
                             json={'source_type': 'camp_session', 'source_id': self.session.id,
                                   'ws_id': self.ws_soft.id, 'action': 'claim'})
        self.assertEqual(r.status_code, 400)
        # 事项完成后态势归零
        db.session.expire_all()
        self._cmd(self.member1, item_id, 'complete',
                  WorkItem.query.get(item_id).version, completion_note='完成')
        db.session.expire_all()
        r = self.client.get(f'/work/objects/board?ws={self.ws_soft.id}',
                            headers=self._auth(self.member1))
        row = next(x for x in r.get_json()['data']['objects'] if x['source_id'] == self.course.id)
        self.assertEqual(row['active_items'], 0)
        # 取消认领
        r = self.client.post('/work/objects/claim', headers=self._auth(self.coord),
                             json={'source_type': 'course', 'source_id': self.course.id,
                                   'ws_id': self.ws_soft.id, 'action': 'unclaim'})
        self.assertEqual(r.status_code, 200)

if __name__ == '__main__':
    unittest.main(verbosity=2)

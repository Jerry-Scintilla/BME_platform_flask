"""Isolated real JWT/session and PKCE exchange tests. No production connection."""
import base64
import hashlib
import secrets
import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from flask import Flask
from flask_jwt_extended import JWTManager, create_access_token
from sqlalchemy.dialects.mysql import LONGTEXT, MEDIUMTEXT
from sqlalchemy.ext.compiler import compiles
from exts import db, limiter
from models import UserModel, PersonModel, AuthSessionModel, ClubGroup, ClubMembership
from workbench_sso_models import WorkbenchSSOCode
from blueprints.workbench_sso import bp

@compiles(MEDIUMTEXT, 'sqlite')
@compiles(LONGTEXT, 'sqlite')
def _text(element, compiler, **kw): return 'TEXT'


class WorkbenchSSOTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
            JWT_SECRET_KEY=secrets.token_urlsafe(48), RATELIMIT_ENABLED=False,
            WORKBENCH_SSO_ENABLED='true', WORKBENCH_CLIENT_ID='workbench',
            WORKBENCH_CLIENT_SECRET=secrets.token_urlsafe(48),
            WORKBENCH_REDIRECT_URI='http://172.25.56.83:8080/workbench/api/auth/callback',
            WORKBENCH_ISSUER='bme-training')
        db.init_app(self.app); JWTManager(self.app); limiter.init_app(self.app)
        self.app.register_blueprint(bp)
        from blueprints.club_admin import bp as club_bp
        self.app.register_blueprint(club_bp)
        self.context = self.app.app_context(); self.context.push(); db.create_all()
        person = PersonModel(public_id='sso-test-person', record_status='active')
        db.session.add(person); db.session.flush()
        self.user = UserModel(email='test@example.test', username='测试成员', person_id=person.id,
                              lifecycle='active', status='active', account_kind='standard', security_version=1)
        self.user.set_password('not-a-real-password')
        db.session.add(self.user); db.session.flush()
        group = ClubGroup(name='测试组'); db.session.add(group); db.session.flush()
        self.member = ClubMembership(user_id=self.user.id, group_id=group.id, slot='primary', joined_at=date.today())
        db.session.add(self.member)
        self.session = AuthSessionModel(sid='sso-test-session', user_id=self.user.id,
            security_version=1, refresh_digest='f'*64, auth_time=datetime.now(), expires_at=datetime.now()+timedelta(hours=1))
        db.session.add(self.session); db.session.commit()
        self.token = create_access_token(identity=self.user.email, additional_claims={
            'token_schema':'v2', 'sid':self.session.sid, 'security_version':1})
        self.client = self.app.test_client()
        self.verifier = secrets.token_urlsafe(32)
        self.body = {'client_id':'workbench', 'response_type':'code', 'scope':'workbench:access',
            'redirect_uri':self.app.config['WORKBENCH_REDIRECT_URI'], 'state':secrets.token_urlsafe(32),
            'code_challenge_method':'S256', 'code_challenge':base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode()).digest()).decode().rstrip('=')}
        self.basic = {'Authorization':'Basic '+base64.b64encode(('workbench:'+self.app.config['WORKBENCH_CLIENT_SECRET']).encode()).decode()}

    def tearDown(self):
        db.session.remove();db.drop_all();self.context.pop()

    def authorize(self):
        return self.client.post('/workbench-sso/authorize', json=self.body, headers={'Authorization':'Bearer '+self.token})

    def exchange(self, code, verifier=None):
        return self.client.post('/workbench-sso/exchange',headers=self.basic,json={
            'audience':'workbench','grant_type':'authorization_code','code':code,
            'code_verifier':verifier or self.verifier,'redirect_uri':self.body['redirect_uri']})

    def code(self):
        r=self.authorize();self.assertEqual(r.status_code,200,r.json)
        return parse_qs(urlsplit(r.json['redirect_url']).query)['code'][0]

    def test_real_session_pkce_exchange_and_replay(self):
        code=self.code();r=self.exchange(code)
        self.assertEqual(r.status_code,200,r.json);self.assertTrue(r.json['community_active'])
        self.assertIn('directory',r.json);self.assertNotIn('password',str(r.json))
        self.assertEqual(self.exchange(code).status_code,400)

    def test_wrong_pkce_then_valid(self):
        code=self.code();self.assertEqual(self.exchange(code,secrets.token_urlsafe(32)).status_code,400)
        self.assertEqual(self.exchange(code).status_code,200)

    def test_callback_and_audience_are_fixed(self):
        self.body['redirect_uri']='http://foreign.test/callback';self.assertEqual(self.authorize().status_code,400)
        r=self.client.post('/workbench-sso/status',headers=self.basic,json={'audience':'mall','issuer':'bme-training','subject':str(self.user.id)})
        self.assertEqual(r.status_code,400)

    def test_qualification_revocation_between_authorization_and_exchange(self):
        code=self.code();db.session.delete(self.member);db.session.commit()
        self.assertEqual(self.exchange(code).status_code,400)
        r=self.client.post('/workbench-sso/status',headers=self.basic,json={'audience':'workbench','issuer':'bme-training','subject':str(self.user.id)})
        self.assertEqual(r.status_code,200);self.assertFalse(r.json['community_active'])

    def test_expired_code_and_revoked_session(self):
        code=self.code();row=WorkbenchSSOCode.query.one();row.expires_at=datetime.now()-timedelta(seconds=1);db.session.commit()
        self.assertEqual(self.exchange(code).status_code,400)
        code=self.code();self.session.revoked_at=datetime.now();db.session.commit()
        self.assertEqual(self.exchange(code).status_code,400)

    def test_basic_auth_and_ordinary_account_are_denied(self):
        self.assertEqual(self.client.post('/workbench-sso/status',json={'audience':'workbench'}).status_code,401)
        db.session.delete(self.member);db.session.commit();self.assertEqual(self.authorize().status_code,403)

    def test_workbench_admission_without_group_slots_and_idempotence(self):
        from workbench_sso_models import WorkbenchAdmission
        from models import ClubMembership
        person = db.session.get(PersonModel, self.user.person_id)
        person.verification_status = 'verified'
        self.user.account_kind = 'standard'
        ClubMembership.query.delete()
        db.session.commit()
        payload = {'audience': 'workbench', 'issuer': 'bme-training', 'subject': str(self.user.id),
            'person_id': str(person.id), 'operator_subject': str(self.user.id), 'reason': '正式内部成员名单'}
        response = self.client.post('/workbench-sso/admission', json=payload, headers=self.basic)
        self.assertEqual(response.status_code, 200, response.json)
        self.assertTrue(response.json['community_active'])
        self.assertEqual(self.client.post('/workbench-sso/admission', json=payload, headers=self.basic).status_code, 200)
        self.assertEqual(WorkbenchAdmission.query.count(), 1)
        self.assertEqual(ClubMembership.query.count(), 0)
        row = WorkbenchAdmission.query.one()
        row.status = 'revoked'
        db.session.commit()
        self.assertEqual(self.client.post('/workbench-sso/admission', json=payload, headers=self.basic).status_code, 409)

    def test_workbench_admission_rejects_unverified_and_changed_person(self):
        payload = {'audience': 'workbench', 'issuer': 'bme-training', 'subject': str(self.user.id),
            'person_id': str(self.user.person_id), 'operator_subject': str(self.user.id), 'reason': '正式名单'}
        self.assertEqual(self.client.post('/workbench-sso/admission', json=payload, headers=self.basic).status_code, 403)
        self.assertEqual(self.client.post('/workbench-sso/admission', json=payload).status_code, 401)

    def test_group_delete_keeps_independent_workspace_references(self):
        import os
        from unittest.mock import patch, Mock
        self.user.role='super_admin'
        group=ClubGroup(name='独立工作区组');db.session.add(group);db.session.commit();gid=group.id
        remote=Mock(status_code=200);remote.json.return_value={'workspaces':1,'open_items':0}
        with patch.dict(os.environ,{'WORKBENCH_RETIRED':'true','WORKBENCH_USAGE_URL':'http://127.0.0.1:5003/api/integrations/account-usage','WORKBENCH_CLIENT_ID':'workbench','WORKBENCH_CLIENT_SECRET':'test-client'}):
            with patch('requests.post',return_value=remote):
                result=self.client.delete('/admin/club/groups/'+str(gid),headers={'Authorization':'Bearer '+self.token})
                self.assertEqual(result.status_code,409,result.json)
            with patch('requests.post',side_effect=RuntimeError('offline')):
                result=self.client.delete('/admin/club/groups/'+str(gid),headers={'Authorization':'Bearer '+self.token})
                self.assertEqual(result.status_code,503,result.json)
        self.assertIsNotNone(db.session.get(ClubGroup,gid))


if __name__ == '__main__': unittest.main()

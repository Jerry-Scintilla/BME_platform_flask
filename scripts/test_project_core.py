"""独立项目定向检查；隔离SQLite，真实治理服务及JWT端点。"""
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from flask_jwt_extended import JWTManager, create_access_token
import test_group_core as fixtures
from exts import db
from models import Project, ProjectChange, CampSession, GroupMembership
from project_models import PROJECT_CORE_TABLES
from services.projects import core
from blueprints.project_core import bp


class ProjectCoreTest(fixtures.GroupCoreSmoke):
    def setUp(self):
        super().setUp()
        db.metadata.create_all(db.engine,tables=list(PROJECT_CORE_TABLES))
        self.app.config.update(TESTING=True,PROJECT_CORE_ENABLED=True,JWT_SECRET_KEY='isolated-project-core-jwt-secret-20261016')
        JWTManager(self.app);self.app.register_blueprint(bp);self.client=self.app.test_client()

    def call(self,user,method,path,**kwargs):
        token=create_access_token(identity=user.email)
        with patch('services.auth_context.current_actor',return_value=SimpleNamespace(user=user)):
            return getattr(self.client,method)(path,headers={'Authorization':'Bearer '+token},**kwargs)

    def create_project(self):
        response=self.call(self.owner,'post','/project-core/projects',json={'title':'独立研发项目','idempotency_key':'create-one'})
        self.assertEqual(response.status_code,200,response.json)
        return response.json['project']

    def mutate(self,pid,data,user=None):
        return self.call(user or self.owner,'post',f'/project-core/projects/{pid}/members',json=data)

    def test_standalone_create_replay_and_access(self):
        p=self.create_project();self.assertEqual(p['my_role'],'leader');self.assertEqual(p['member_count'],1)
        self.assertEqual(self.create_project()['id'],p['id'])
        self.assertEqual(Project.query.count(),1)
        self.assertEqual(self.call(self.student,'get',f"/project-core/projects/{p['id']}").status_code,403)
        self.assertEqual(self.call(self.owner,'get','/project-core/projects').json['projects'][0]['id'],p['id'])
        bad=self.call(self.owner,'post','/project-core/projects',json={'title':'不同项目','idempotency_key':'create-one'})
        self.assertEqual(bad.status_code,409)

    def test_member_add_remove_transfer_and_version(self):
        p=self.create_project();pid=p['id']
        args={'action':'add','user_id':self.student.id,'expected_version':p['version'],'reason':'加入团队','idempotency_key':'member-add'}
        response=self.mutate(pid,args);self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(self.mutate(pid,args).status_code,200)
        self.assertEqual(len(response.json['members']),2)
        self.assertEqual(self.mutate(pid,{**args,'idempotency_key':'stale'}).status_code,409)
        response=self.mutate(pid,{'action':'transfer','user_id':self.student.id,'expected_version':2,'reason':'负责人交接','idempotency_key':'transfer'})
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(self.call(self.student,'get',f'/project-core/projects/{pid}').json['project']['my_role'],'leader')
        self.assertEqual(self.mutate(pid,{'action':'remove','user_id':self.owner.id,'expected_version':3,'reason':'退出团队','idempotency_key':'exit'},self.student).status_code,200)
        self.assertEqual(self.call(self.owner,'get',f'/project-core/projects/{pid}').status_code,403)

    def test_status_and_concurrent_update(self):
        p=self.create_project();path=f"/project-core/projects/{p['id']}"
        args={'expected_version':1,'idempotency_key':'pause','reason':'暂缓研发','status':'paused'}
        response=self.call(self.owner,'patch',path,json=args);self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(self.mutate(p['id'],{'action':'add','user_id':self.student.id,'expected_version':2,'reason':'成员加入','idempotency_key':'denied'}).status_code,409)
        self.assertEqual(self.call(self.owner,'patch',path,json={**args,'expected_version':0,'idempotency_key':'stale'}).status_code,409)
        self.assertEqual(self.call(self.owner,'patch',path,json={**args,'expected_version':2,'idempotency_key':'resume','status':'active'}).status_code,200)
        self.assertEqual(ProjectChange.query.filter_by(project_id=p['id']).count(),3)

    def test_feature_gate_and_invalid_inputs(self):
        self.app.config['PROJECT_CORE_ENABLED']=False
        self.assertFalse(self.call(self.owner,'get','/project-core/status').json['enabled'])
        self.assertEqual(self.call(self.owner,'get','/project-core/projects').status_code,503)
        self.app.config['PROJECT_CORE_ENABLED']=True
        self.assertEqual(self.call(self.owner,'post','/project-core/projects',json={'title':'','idempotency_key':'empty'}).status_code,400)
        self.assertEqual(Project.query.count(),0)

    def test_admin_without_person_and_inactive_owner_handoff(self):
        import config
        from models import UserModel
        p=self.create_project()
        admin=UserModel(username='独立管理员',email='independent-admin@fixture.invalid',password='fixture-only',role='super_admin',status='active',lifecycle='active',account_kind='management_aux')
        db.session.add(admin);db.session.commit()
        with patch.object(config,'IDENTITY_ENFORCEMENT_MODE','enforce'):
            self.assertEqual(self.call(admin,'get',f"/project-core/projects/{p['id']}").status_code,200)
        self.assertEqual(self.mutate(p['id'],{'action':'add','user_id':self.student.id,'expected_version':1,'idempotency_key':'handoff-add','reason':'负责人交接准备'}).status_code,200)
        self.owner.status='banned';db.session.commit()
        response=self.mutate(p['id'],{'action':'transfer','user_id':self.student.id,'expected_version':2,'idempotency_key':'inactive-handoff','reason':'停用账号负责人交接'},admin)
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(self.call(self.student,'get',f"/project-core/projects/{p['id']}").json['project']['my_role'],'leader')


if __name__=='__main__':unittest.main()

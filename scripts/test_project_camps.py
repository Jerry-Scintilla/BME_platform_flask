"""跨营组合、参营人员和长期团队分离的定向检查。"""
import unittest
from datetime import date,timedelta
import test_project_core as fixtures
import test_project_delivery as sqlite_types
from exts import db
import models as m
from services.projects import core


class ProjectCampsTest(fixtures.ProjectCoreTest):
    def setUp(self):
        super().setUp();db.create_all()
        from blueprints.camp import bp as camp_bp
        from blueprints.camp_delivery import bp as delivery_bp
        self.app.register_blueprint(camp_bp);self.app.register_blueprint(delivery_bp)
        self.teacher=self.make_user('camp_teacher')
        self.camp=m.CampSession(name='秋季项目营',category='project',status='selecting',is_featured=True,start_date=date.today(),end_date=date.today()+timedelta(days=100))
        self.other=m.CampSession(name='另一项目营',category='project',status='selecting',is_featured=True,start_date=date.today(),end_date=date.today()+timedelta(days=100))
        db.session.add_all([self.camp,self.other]);db.session.flush()
        for c in [self.camp,self.other]:db.session.add(m.CampStaff(camp_session_id=c.id,user_id=self.teacher.id,role='teacher',status='active',assigned_by=self.actor.id))
        db.session.commit()

    def setup_binding(self,camp=None):
        p=self.create_project();row=db.session.get(m.Project,p['id'])
        if not core.role(row,self.student):
            r=self.mutate(p['id'],{'action':'add','user_id':self.student.id,'expected_version':row.version,'reason':'参营团队','idempotency_key':'setup-member'})
            self.assertEqual(r.status_code,200,r.json)
        row=db.session.get(m.Project,p['id'])
        response=self.call(self.owner,'post',f"/project-core/projects/{p['id']}/camps",json={'camp_id':(camp or self.camp).id,'user_ids':[self.owner.id,self.student.id],'expected_version':row.version,'reason':'申请参营','idempotency_key':'join-'+str((camp or self.camp).id)})
        self.assertEqual(response.status_code,200,response.json)
        return response.json['project'],response.json['binding']

    def approve(self,p,b):
        response=self.call(self.teacher,'post',f"/project-core/projects/{p['id']}/camps/{b['id']}",json={'action':'approve','expected_version':p['version'],'expected_binding_version':b['version'],'reason':'批准参营','idempotency_key':'approve-'+str(b['id'])})
        self.assertEqual(response.status_code,200,response.json)
        return response.json

    def test_two_camps_closure_does_not_end_project_or_team(self):
        p,b=self.setup_binding();self.approve(p,b)
        p,b=self.setup_binding(self.other);self.approve(p,b)
        self.assertEqual(m.Project.query.count(),1)
        self.assertEqual(m.ProjectCampBinding.query.filter_by(status='active').count(),2)
        row=db.session.get(m.Project,p['id']);code,zero=row.code,row.zero_date
        self.camp.status='archived';db.session.commit()
        self.assertEqual(core.role(row,self.student),'member')
        self.assertEqual(row.status,'active');self.assertEqual(row.code,code);self.assertEqual(row.zero_date,zero)
        self.assertEqual(self.call(self.owner,'get',f"/project-core/projects/{p['id']}/calendar").status_code,200)
        self.assertEqual(self.call(self.teacher,'get',f"/project-core/camps/{self.other.id}/projects").status_code,200)
        self.assertEqual(self.call(self.teacher,'get',f"/project-core/projects/{p['id']}/members").status_code,403)

    def test_participant_removal_keeps_long_term_membership(self):
        p,b=self.setup_binding();approved=self.approve(p,b)
        path=f"/project-core/projects/{p['id']}/camps/{b['id']}"
        r=self.call(self.teacher,'post',path,json={'action':'participants','user_ids':[self.owner.id],'expected_version':approved['project']['version'],'expected_binding_version':approved['binding']['version'],'reason':'结束本期参与','idempotency_key':'remove-participant'})
        self.assertEqual(r.status_code,200,r.json)
        self.assertEqual(core.role(db.session.get(m.Project,p['id']),self.student),'member')
        self.assertEqual(m.ProjectCampParticipant.query.filter_by(binding_id=b['id'],user_id=self.student.id).one().status,'ended')
        self.assertEqual(self.call(self.student,'post',path,json={}).status_code,403)
        self.assertEqual(self.call(self.owner,'get',f"/project-core/projects/{p['id']+1}/context?binding={b['id']}").status_code,404)


    def test_close_endpoint_freezes_period_and_keeps_long_project(self):
        p,b=self.setup_binding();self.approve(p,b)
        self.camp.status='running';db.session.commit()
        response=self.call(self.actor,'post',f'/camp/sessions/{self.camp.id}/transitions',json={'action':'close'})
        self.assertEqual(response.status_code,200,response.json)
        binding=db.session.get(m.ProjectCampBinding,b['id']);project=db.session.get(m.Project,p['id'])
        self.assertEqual(binding.status,'ended');self.assertEqual(project.status,'active')
        self.assertEqual(core.role(project,self.owner),'leader');self.assertEqual(core.role(project,self.student),'member')
        self.assertEqual(len(db.session.get(m.ProjectBindingSnapshot,binding.id).snapshot['participants']),2)
        self.assertIsNotNone(m.CampArchive.query.filter_by(camp_session_id=self.camp.id).first())
        self.assertEqual(self.call(self.owner,'get',f'/project-core/projects/{project.id}/calendar').status_code,200)


if __name__=='__main__':unittest.main()

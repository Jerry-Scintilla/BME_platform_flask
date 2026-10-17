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


    def test_context_separates_project_period_and_teacher_permissions(self):
        p,b=self.setup_binding();self.approve(p,b)
        path=f"/project-core/projects/{p['id']}/context?binding={b['id']}"
        owner=self.call(self.owner,'get',path).json
        member=self.call(self.student,'get',path).json
        teacher=self.call(self.teacher,'get',path+'&view=camp').json
        admin=self.call(self.actor,'get',path).json
        self.assertEqual(owner['binding']['my_role'],'leader')
        self.assertTrue(owner['binding']['is_participant'])
        self.assertTrue(owner['binding']['can_read_work'])
        self.assertTrue(owner['binding']['can_write_work'])
        self.assertTrue(owner['binding']['can_request_participants'])
        self.assertFalse(owner['binding']['can_manage_participants'])
        self.assertEqual(member['binding']['my_role'],'member')
        self.assertTrue(member['binding']['can_write_work'])
        self.assertFalse(member['binding']['can_request_participants'])
        self.assertIsNone(teacher['binding']['my_role'])
        self.assertFalse(teacher['binding']['is_participant'])
        self.assertTrue(teacher['binding']['can_read_work'])
        self.assertFalse(teacher['binding']['can_write_work'])
        self.assertTrue(teacher['binding']['can_manage_participants'])
        self.assertFalse(teacher['binding']['can_request_participants'])
        self.assertFalse(teacher['project']['can_write'])
        self.assertFalse(teacher['project']['can_manage'])
        self.assertNotIn('research_summary',teacher['project'])
        self.assertIsNone(admin['binding']['my_role'])
        self.assertFalse(admin['binding']['is_participant'])
        self.assertTrue(admin['binding']['can_write_work'])
        self.assertTrue(admin['binding']['can_manage_participants'])
        self.assertEqual(self.call(self.teacher,'get',f"/project-core/projects/{p['id']}/members").status_code,403)
        self.assertEqual(self.call(self.teacher,'get',f"/project-core/projects/{p['id']}/records").status_code,403)
        self.assertEqual(self.call(self.teacher,'get',f"/project-core/projects/{p['id']}/calendar").status_code,403)

    def test_context_uses_period_leader_and_explicit_read_only_view(self):
        p,b=self.setup_binding();self.approve(p,b)
        binding=db.session.get(m.ProjectCampBinding,b['id'])
        binding.leader_user_id=self.student.id;db.session.commit()
        path=f"/project-core/projects/{p['id']}/context?binding={b['id']}"
        owner=self.call(self.owner,'get',path).json
        member=self.call(self.student,'get',path).json
        self.assertEqual(owner['project']['my_role'],'leader')
        self.assertEqual(owner['binding']['my_role'],'member')
        self.assertTrue(owner['binding']['can_request_participants'])
        self.assertEqual(member['project']['my_role'],'member')
        self.assertEqual(member['binding']['my_role'],'leader')
        self.assertFalse(member['binding']['can_request_participants'])
        viewed=self.call(self.owner,'get',path+'&view=camp').json
        self.assertTrue(viewed['binding']['can_read_work'])
        self.assertFalse(viewed['binding']['can_write_work'])
        self.assertFalse(viewed['binding']['can_request_participants'])
        self.assertFalse(viewed['project']['can_write'])

    def test_context_does_not_grant_period_access_to_long_term_only_member(self):
        p,b=self.setup_binding();self.approve(p,b)
        participant=m.ProjectCampParticipant.query.filter_by(binding_id=b['id'],user_id=self.student.id).one()
        participant.status='ended';db.session.commit()
        path=f"/project-core/projects/{p['id']}/context?binding={b['id']}"
        data=self.call(self.student,'get',path).json
        self.assertEqual(data['project']['my_role'],'member')
        self.assertTrue(data['project']['can_write'])
        self.assertIsNone(data['binding']['my_role'])
        self.assertFalse(data['binding']['is_participant'])
        self.assertFalse(data['binding']['can_read_work'])
        self.assertFalse(data['binding']['can_write_work'])
        participant.status='active'
        m.CampMember.query.filter_by(camp_session_id=self.camp.id,user_id=self.student.id).one().status='ended'
        db.session.commit()
        data=self.call(self.student,'get',path).json
        self.assertFalse(data['binding']['is_participant'])
        self.assertFalse(data['binding']['can_read_work'])
        self.assertFalse(data['binding']['can_write_work'])
        self.assertEqual(self.call(self.owner,'get',f"/project-core/projects/{p['id']+1}/context?binding={b['id']}").status_code,404)
        self.assertEqual(self.call(self.owner,'get',path+'&binding='+str(b['id'])).status_code,400)

    def test_context_pending_and_finished_bindings_are_not_writable(self):
        p,b=self.setup_binding()
        path=f"/project-core/projects/{p['id']}/context?binding={b['id']}"
        pending=self.call(self.owner,'get',path).json['binding']
        self.assertFalse(pending['can_read_work']);self.assertFalse(pending['can_write_work'])
        self.assertFalse(pending['can_request_participants'])
        self.assertTrue(self.call(self.teacher,'get',path).json['binding']['can_manage_participants'])
        self.approve(p,b)
        project=db.session.get(m.Project,p['id']);binding=db.session.get(m.ProjectCampBinding,b['id'])
        project.status='paused';db.session.commit()
        paused=self.call(self.owner,'get',path).json['binding']
        self.assertTrue(paused['can_read_work']);self.assertFalse(paused['can_write_work'])
        self.assertFalse(paused['can_request_participants'])
        self.assertFalse(self.call(self.teacher,'get',path).json['binding']['can_manage_participants'])
        project.status='active';db.session.commit()
        response=self.call(self.teacher,'post',f"/project-core/projects/{p['id']}/camps/{b['id']}",json={
            'action':'end','expected_version':project.version,'expected_binding_version':binding.version,
            'reason':'本期结束','idempotency_key':'context-end-period'})
        self.assertEqual(response.status_code,200,response.json)
        for user in (self.owner,self.student,self.teacher):
            ended=self.call(user,'get',path).json['binding']
            self.assertTrue(ended['can_read_work'])
            self.assertFalse(ended['is_participant'])
            self.assertFalse(ended['can_write_work'])
            self.assertFalse(ended['can_manage_participants'])
            self.assertFalse(ended['can_request_participants'])
        self.assertTrue(self.call(self.owner,'get',path).json['project']['can_write'])

    def test_empty_migration_request_is_not_a_pending_roster_change(self):
        p,b=self.setup_binding();self.approve(p,b)
        project=db.session.get(m.Project,p['id']);binding=db.session.get(m.ProjectCampBinding,b['id'])
        path=f"/project-core/projects/{p['id']}/context?binding={b['id']}"
        self.assertFalse(self.call(self.owner,'get',path).json['binding']['change_pending'])
        binding.requested_users=[];db.session.commit()
        self.assertFalse(self.call(self.teacher,'get',path).json['binding']['change_pending'])
        self.assertEqual(binding.requested_users,[])
        request=self.call(self.owner,'post',f"/project-core/projects/{p['id']}/camps/{b['id']}/participants-request",json={
            'user_ids':[self.owner.id],'expected_version':project.version,'expected_binding_version':binding.version,
            'reason':'仅负责人参加后续本期工作','idempotency_key':'context-roster-request'})
        self.assertEqual(request.status_code,200,request.json)
        self.assertTrue(request.json['binding']['change_pending'])
        self.assertTrue(self.call(self.student,'get',path).json['binding']['change_pending'])
        self.assertEqual(self.call(self.student,'get',path).json['binding']['requested_users'],[])
        approved=self.call(self.teacher,'post',f"/project-core/projects/{p['id']}/camps/{b['id']}",json={
            'action':'participants','expected_version':project.version,'expected_binding_version':binding.version,
            'reason':'确认本期名单调整','idempotency_key':'context-roster-confirm'})
        self.assertEqual(approved.status_code,200,approved.json)
        self.assertFalse(approved.json['binding']['change_pending'])


if __name__=='__main__':unittest.main()

"""项目工作区授权、通知定位与只读投影定向检查，使用隔离SQLite。"""
import unittest
from datetime import date
import test_project_dashboard as fixtures
from exts import db
import models as m
from services.camp_groups import projects
from blueprints import camp_project, camp_meeting


class ProjectWorkspaceTest(fixtures.ProjectDashboardTest):
    def setUp(self):
        super().setUp()
        self.app.register_blueprint(camp_project.bp)
        self.app.register_blueprint(camp_meeting.bp)

    def test_node_target_checks_camp_project_and_current_authorization(self):
        path=f'/camp/projects/{self.camp.id}/node-target/{self.node.id}'
        self.assertEqual(self.call(self.owner,'get',path).json['unit_id'],self.project.id)
        self.assertEqual(self.call(self.teacher,'get',path).status_code,200)
        self.assertEqual(self.call(self.student,'get',path).status_code,403)
        self.assertEqual(self.call(self.actor,'get',f'/camp/projects/{self.camp.id+1}/node-target/{self.node.id}').status_code,404)
        self.staff.status='ended';db.session.commit()
        self.assertEqual(self.call(self.teacher,'get',path).status_code,403)

    def test_roster_read_does_not_expose_candidates_and_rechecks_exit(self):
        self.add_student()
        path=f'/camp/units/{self.project.id}/members'
        result=self.call(self.student,'get',path)
        self.assertEqual(result.status_code,200)
        self.assertEqual(len(result.json['members']),2)
        self.assertEqual(set(result.json['members'][0]),{'user_id','username','role'})
        self.assertNotIn('candidates',result.json)
        projects.end_member(self.project,self.student.id,'remove','admin',self.actor.id,'检查退出')
        db.session.commit()
        self.assertEqual(self.call(self.student,'get',path).status_code,403)

    def test_node_notifications_are_unambiguous(self):
        self.add_student();self.submit(self.student);self.evaluate(score=0)
        rows=m.NotificationModel.query.filter_by(source_type='project_node').all()
        self.assertGreaterEqual(len(rows),2)
        self.assertTrue(all(r.source_id==self.node.id and r.camp_session_id==self.camp.id for r in rows))

    def test_node_target_rejects_inconsistent_camp_unit(self):
        other=m.CampSession(name='另一个营期',category='project',status='running',start_date=date.today(),end_date=date.today())
        db.session.add(other);db.session.flush()
        other_unit=m.CampUnit(camp_session_id=other.id,unit_type='project',name='跨营项目',owner_user_id=self.owner.id,status='active')
        db.session.add(other_unit);db.session.flush();projects.bind_new_project(other_unit,self.actor.id)
        self.node.unit_id=other_unit.id;db.session.commit()
        self.assertEqual(self.call(self.teacher,'get',f'/camp/projects/{self.camp.id}/node-target/{self.node.id}').status_code,404)

    def test_teacher_meeting_filter_is_scoped_to_project(self):
        other=m.CampUnit(camp_session_id=self.camp.id,unit_type='project',name='另一项目',owner_user_id=self.owner.id,status='active')
        db.session.add(other);db.session.flush();projects.bind_new_project(other,self.actor.id)
        db.session.add_all([m.CampMeeting(camp_session_id=self.camp.id,scope='unit',unit_id=self.project.id,title='本项目组会',meeting_date=date.today(),created_by=self.owner.id),m.CampMeeting(camp_session_id=self.camp.id,scope='unit',unit_id=other.id,title='另一项目组会',meeting_date=date.today(),created_by=self.owner.id)])
        db.session.commit()
        result=self.call(self.teacher,'get',f'/camp/sessions/{self.camp.id}/meetings/all?unit_id={self.project.id}')
        self.assertEqual(result.status_code,200,result.json)
        self.assertEqual(result.json['total'],1)
        self.assertEqual(result.json['meetings'][0]['title'],'本项目组会')
        self.assertEqual(self.call(self.teacher,'get',f'/camp/sessions/{self.camp.id}/meetings/all?unit_id=99999').status_code,400)
        self.assertEqual(self.call(self.teacher,'get',f'/camp/sessions/{self.camp.id}/meetings/all?unit_id=bad').status_code,400)


if __name__=='__main__':unittest.main()

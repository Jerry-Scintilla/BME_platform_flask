"""第三部分定向检查：SQLite 内存服务及真实 Flask 路由，不启动 app、Redis 或业务数据库。"""
import unittest
import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.mysql import LONGTEXT
@compiles(LONGTEXT, 'sqlite')
def sqlite_longtext(_type, _compiler, **_kw):
    return 'TEXT'

import test_group_core as foundation
from exts import db
from models import (CampSession, CampMember, TrainingGroupProfile, CampTeachingEvent, CampMentorProfile,
    CampCourseAssignment, CourseModel, Chapter, CampChapterCertification, CampChapterMaterial,
    CampLearningProgress, CampLeave, CampAttendancePlan, CampMentorMatch, CampMentorPreference,
    CampMentorFavorite, CampStaff, CampMemberEvent, CampPersonParticipationModel, AuditLog,
    NotificationModel, UserCourseModel, CampPolicy, CampChapterMaterialAttachment, LearningProgressModel,
    CampMeeting, CampMeetingAttachment, CampMeetingTask, CampMeetingTaskSubmission, CampMeetingChapterPlan, CampMeetingTaskAttachment)
from services.camp_groups import training as service, training_access as access
from services.groups import GroupError


class TrainingSmoke(unittest.TestCase):
    make_user = foundation.GroupCoreSmoke.make_user
    tearDown = foundation.GroupCoreSmoke.tearDown

    def setUp(self):
        foundation.GroupCoreSmoke.setUp(self)
        tables = set()
        def include(table):
            if table in tables:
                return
            tables.add(table)
            for fk in table.foreign_keys:
                include(fk.column.table)
        for model in (CampMember, TrainingGroupProfile, CampTeachingEvent, CampMentorProfile,
                      CampCourseAssignment, Chapter, CampChapterCertification, CampChapterMaterial,
                      CampLearningProgress, CampLeave, CampAttendancePlan, CampMentorMatch,
                      CampMentorPreference, CampMentorFavorite, CampStaff, CampMemberEvent,
                      CampPersonParticipationModel, AuditLog, NotificationModel, UserCourseModel, CampChapterMaterialAttachment, LearningProgressModel, CampMeeting, CampMeetingAttachment, CampMeetingTask, CampMeetingTaskSubmission, CampMeetingChapterPlan, CampMeetingTaskAttachment):
            include(model.__table__)
        db.metadata.create_all(db.engine, tables=list(tables))
        self.course = CourseModel(title='C 语言', introduction='fixture', creator_id=self.actor.id)
        db.session.add(self.course)
        db.session.flush()
        self.chapter = Chapter(course_id=self.course.id, name='学习单元', level=2)
        self.camp = CampSession(name='多组营', category='learning', status='selecting',
            start_date=date.today(), end_date=date.today()+timedelta(days=30), training_groups_ready=True,
            mentor_selection_enabled=True, ms_preference_start=datetime.now()-timedelta(days=2),
            ms_preference_deadline=datetime.now()-timedelta(days=1),
            ms_tags=json.dumps([{'name':'编程','course_ids':[self.course.id]}]))
        db.session.add_all([self.camp,self.chapter]);db.session.flush()
        self.other = self.make_user('mentor2')
        for u,role in [(self.owner,'mentor'),(self.student,'student'),(self.other,'mentor')]:
            db.session.add(CampMember(camp_session_id=self.camp.id,user_id=u.id,role=role))
        db.session.flush()
        self.a = service.create_group(self.camp.id,actor_id=self.actor.id,mentor_id=self.owner.id,name='甲组',direction='编程')
        self.b = service.create_group(self.camp.id,actor_id=self.actor.id,mentor_id=self.other.id,name='乙组',direction='编程')
        db.session.commit()

    def test_dual_identity_and_course_conflict(self):
        service.add_role(self.a,self.other.id,'learner',self.actor.id)
        self.assertEqual(set(access.member_roles(CampMember.query.filter_by(user_id=self.other.id).one())), {'student','mentor'})
        self.assertTrue(access.can_teach(self.camp.id,self.owner.id,self.other.id,self.course.id))
        self.assertFalse(access.can_teach(self.camp.id,self.other.id,self.other.id,self.course.id))
        service.add_role(self.a,self.student.id,'learner',self.actor.id)
        with self.assertRaises(GroupError), db.session.begin_nested():
            service.add_role(self.b,self.student.id,'learner',self.actor.id)
        self.assertEqual(CampCourseAssignment.query.filter_by(student_user_id=self.student.id).count(),1)
        self.assertFalse(access.can_teach(self.camp.id,self.other.id,self.student.id,self.course.id))
        service.add_role(self.b,self.student.id,'learner',self.actor.id,transfer_courses=True)
        self.assertTrue(access.can_teach(self.camp.id,self.other.id,self.student.id,self.course.id))
        self.assertFalse(access.can_teach(self.camp.id,self.owner.id,self.student.id,self.course.id))

    def test_group_leave_preserves_progress_and_obligations(self):
        service.add_role(self.a,self.student.id,'learner',self.actor.id)
        db.session.add(CampAttendancePlan(camp_session_id=self.camp.id,user_id=self.student.id,date=date.today(),source='self_selected'))
        cert = CampChapterCertification(camp_session_id=self.camp.id,student_user_id=self.student.id,
            chapter_id=self.chapter.id,course_id=self.course.id,mentor_user_id=self.owner.id,score=0)
        db.session.add(cert);db.session.flush();cert_id=cert.id
        service.remove_role(self.a,self.student.id,'learner',self.actor.id)
        self.assertIsNone(CampCourseAssignment.query.filter_by(student_user_id=self.student.id).one().unit_id)
        self.assertEqual(db.session.get(CampChapterCertification,cert_id).score,0)
        self.assertEqual(CampAttendancePlan.query.count(),1)
        self.assertTrue(CampMember.query.filter_by(user_id=self.student.id).one().needs_attendance)

    def test_recruitment_capacity_release_and_read_only(self):
        p=db.session.get(TrainingGroupProfile,self.a.id);p.recruitment_mentor_id=self.owner.id;p.capacity=1
        db.session.add(CampMentorProfile(camp_session_id=self.camp.id,user_id=self.owner.id,tags='["编程"]',capacity=1));db.session.flush()
        service.recruit(self.camp.id,self.student.id,self.owner.id,self.owner.id,source='mentor_pick')
        self.assertEqual(access.approver_ids(self.camp.id,self.student.id),[self.owner.id])
        with self.assertRaises(GroupError), db.session.begin_nested():
            service.recruit(self.camp.id,self.other.id,self.owner.id,self.actor.id)
        service.recruit(self.camp.id,self.student.id,None,self.owner.id,source='mentor_pick')
        self.assertIsNone(CampMember.query.filter_by(user_id=self.student.id).one().recruitment_unit_id)
        self.assertEqual(CampMentorMatch.query.count(),0)
        self.camp.status='archived';db.session.flush()
        with self.assertRaises(GroupError):
            service.add_role(self.a,self.student.id,'learner',self.actor.id)

    def client_as(self,user):
        from flask_jwt_extended import JWTManager, create_access_token
        from blueprints.camp import bp as camp_bp
        from blueprints.camp_training import bp as training_bp
        from blueprints.camp_ms import bp as ms_bp
        from blueprints.camp_meeting import bp as meeting_bp
        self.app.config.update(TESTING=True, JWT_SECRET_KEY='isolated-training-test-secret-32-chars', JWT_TOKEN_LOCATION=['headers'])
        JWTManager(self.app)
        self.app.register_blueprint(camp_bp)
        self.app.register_blueprint(training_bp)
        self.app.register_blueprint(ms_bp)
        self.app.register_blueprint(meeting_bp)
        token=create_access_token(identity=user.email)
        self.auth_patch=patch('services.auth_context.current_actor',return_value=SimpleNamespace(user=user))
        self.auth_patch.start();self.addCleanup(self.auth_patch.stop)
        return self.app.test_client(), {'Authorization':'Bearer '+token}

    def test_certification_route_and_self_denial(self):
        service.add_role(self.a,self.student.id,'learner',self.actor.id);db.session.commit()
        client,headers=self.client_as(self.owner)
        path=f'/camp/sessions/{self.camp.id}/team/progress/certify'
        response=client.post(path,headers=headers,json={'student_user_id':self.student.id,'chapter_id':self.chapter.id,'score':0})
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(CampChapterCertification.query.one().score,0)
        response=client.post(path,headers=headers,json={'student_user_id':self.owner.id,'chapter_id':self.chapter.id})
        self.assertEqual(response.status_code,403,response.json)
        response=client.delete(path,headers=headers,json={'student_user_id':self.student.id,'chapter_id':self.chapter.id})
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(CampChapterCertification.query.count(),0)
        self.assertEqual(CampTeachingEvent.query.filter_by(action='certification_revoke').count(),1)

    def test_leave_single_decision_and_dual_roles_response(self):
        service.add_role(self.a,self.student.id,'learner',self.actor.id)
        service.add_role(self.a,self.other.id,'learner',self.actor.id)
        db.session.add(CampLeave(camp_session_id=self.camp.id,user_id=self.student.id,start_date=date.today(),end_date=date.today(),reason='fixture',status='pending'))
        db.session.commit();leave=CampLeave.query.one()
        client,headers=self.client_as(self.owner)
        path=f'/camp/leave/{leave.id}/approve'
        response=client.post(path,headers=headers,json={'approve':True})
        self.assertEqual(response.status_code,200,response.json)
        response=client.post(path,headers=headers,json={'approve':False})
        self.assertEqual(response.status_code,400,response.json)
        self.assertEqual(db.session.get(CampLeave,leave.id).status,'approved')

    def test_explicit_invalid_course_context_and_legacy_roles(self):
        from blueprints.camp_course_assign import active_scope_camp
        service.add_role(self.a,self.student.id,'learner',self.actor.id)
        with self.assertRaises(GroupError):
            active_scope_camp(self.student.id,self.course.id,prefer_sid=self.camp.id+100)
        self.camp.training_groups_ready=False;db.session.flush()
        self.assertTrue(CampMember.query.filter_by(user_id=self.owner.id,is_mentor=True).one().is_mentor)
        self.assertTrue(CampMember.query.filter_by(user_id=self.student.id,is_learner=True).one().is_learner)

    def test_group_management_and_member_admission_routes(self):
        client, headers = self.client_as(self.actor)
        new_user = self.make_user('new_mentor'); db.session.commit()
        response = client.post(f'/camp/sessions/{self.camp.id}/members', headers=headers,
                               json={'user_id':new_user.id,'role':'mentor'})
        self.assertEqual(response.status_code,200,response.json)
        self.assertTrue(access.has_role(self.camp.id,new_user.id,'mentor'))
        response = client.post(f'/camp/sessions/{self.camp.id}/training-groups', headers=headers,
            json={'mentor_user_id':new_user.id,'name':'第三组','direction':'编程'})
        self.assertEqual(response.status_code,200,response.json)
        response=client.get(f'/camp/sessions/{self.camp.id}/training-groups',headers=headers)
        self.assertEqual(response.status_code,200,response.json)
        group=next(g for g in response.json['groups'] if g['name']=='第三组')
        response=client.post(f'/camp/sessions/{self.camp.id}/training-groups/{group["unit_id"]}/members',headers=headers,
            json={'user_id':self.student.id,'role':'learner','expected_version':group['version']})
        self.assertEqual(response.status_code,200,response.json)
        response=client.get(f'/camp/sessions/{self.camp.id}/progress/board',headers=headers)
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(response.json['summary']['student_count'],1)
        # 与前端一致，暂停/恢复时连同完整配置提交，不能被暂停状态卡死。
        path=f'/camp/sessions/{self.camp.id}/training-groups'
        for status in ('paused', 'active'):
            response=client.get(path,headers=headers)
            current=next(g for g in response.json['groups'] if g['unit_id']==group['unit_id'])
            self.assertEqual(len(current['members']),2)
            response=client.put(f'{path}/{group["unit_id"]}',headers=headers,json={
                'expected_version':current['version'],'name':current['name'],
                'owner_user_id':current['owner_user_id'],'direction':current['direction'],
                'course_ids':current['course_ids'],'capacity':current['capacity'],'status':status})
            self.assertEqual(response.status_code,200,response.json)
        self.assertTrue(access.can_teach(self.camp.id,new_user.id,self.student.id,self.course.id))

    def test_real_recruitment_pick_and_release_routes(self):
        profile=db.session.get(TrainingGroupProfile,self.a.id)
        profile.recruitment_mentor_id=self.owner.id
        db.session.add(CampMentorProfile(camp_session_id=self.camp.id,user_id=self.owner.id,tags='["编程"]',capacity=1))
        db.session.commit()
        client,headers=self.client_as(self.owner)
        path=f'/camp/ms/{self.camp.id}/pick'
        response=client.post(path,headers=headers,json={'student_user_id':self.student.id,'action':'pick'})
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(CampCourseAssignment.query.one().unit_id,self.a.id)
        response=client.post(path,headers=headers,json={'student_user_id':self.student.id,'action':'release'})
        self.assertEqual(response.status_code,200,response.json)
        self.assertIsNone(CampCourseAssignment.query.one().unit_id)
        self.assertEqual(CampCourseAssignment.query.one().status,'active')

    def test_multi_group_views_and_meetings(self):
        service.add_role(self.a,self.other.id,'learner',self.actor.id)
        service.add_role(self.b,self.student.id,'learner',self.actor.id)
        db.session.commit()
        client,headers=self.client_as(self.other)
        response=client.get(f'/camp/sessions/{self.camp.id}',headers=headers)
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(set(response.json['session']['available_perspectives']),{'student','mentor'})
        response=client.get(f'/camp/sessions/{self.camp.id}/my-direction',headers=headers)
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(response.json['courses'][0]['unit_id'],self.a.id)
        response=client.get(f'/camp/sessions/{self.camp.id}/team/progress?unit_id={self.b.id}',headers=headers)
        self.assertEqual(response.status_code,200,response.json)
        self.assertTrue(response.json['students'][0]['courses'][0]['can_certify'])
        response=client.post(f'/camp/sessions/{self.camp.id}/team-meetings',headers=headers,
            data={'unit_id':self.b.id,'title':'组会','meeting_date':date.today().isoformat()})
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(CampMeeting.query.one().unit_id,self.b.id)
        response=client.get(f'/camp/sessions/{self.camp.id}/team-meetings?unit_id={self.b.id}',headers=headers)
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(len(response.json['meetings']),1)

    def test_empty_direction_group_then_add_courses(self):
        self.camp.start_date = date.today() - timedelta(days=7)
        db.session.commit()
        client, headers = self.client_as(self.actor)
        path = f'/camp/sessions/{self.camp.id}'
        tags = [{'name': '编程', 'course_ids': [self.course.id]},
                {'name': '待设计方向', 'course_ids': []}]
        response = client.put(path, headers=headers, json={'ms_tags': tags})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json['session']['ms_directions'], tags)
        response = client.post(path + '/training-groups', headers=headers, json={
            'mentor_user_id': self.owner.id, 'name': '先建组后配课', 'direction': '待设计方向'})
        self.assertEqual(response.status_code, 200, response.json)
        from models import CampUnit
        unit = db.session.get(CampUnit, response.json['unit_id'])
        service.add_role(unit, self.student.id, 'learner', self.actor.id)
        db.session.commit()
        self.assertEqual(CampCourseAssignment.query.count(), 0)
        self.assertEqual(UserCourseModel.query.count(), 0)
        tags[1]['course_ids'] = [self.course.id]
        response = client.put(path, headers=headers, json={'ms_tags': tags})
        self.assertEqual(response.status_code, 200, response.json)
        assignment = CampCourseAssignment.query.one()
        self.assertEqual((assignment.student_user_id, assignment.unit_id), (self.student.id, unit.id))
        self.assertEqual(UserCourseModel.query.count(), 1)
        tags[1]['course_ids'] = []
        response = client.put(path, headers=headers, json={'ms_tags': tags})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(CampCourseAssignment.query.one().status, 'active')
        self.assertEqual(UserCourseModel.query.count(), 1)

    def test_empty_courses_keep_invalid_course_and_time_checks(self):
        self.camp.start_date = date.today() - timedelta(days=7)
        db.session.commit()
        client, headers = self.client_as(self.actor)
        path = f'/camp/sessions/{self.camp.id}'
        response = client.put(path, headers=headers, json={
            'ms_tags': [{'name': '无效课程', 'course_ids': [999999]}]})
        self.assertEqual(response.status_code, 400, response.json)
        self.assertIn('关联课程无效', response.json['message'])
        response = client.put(path, headers=headers, json={
            'ms_tags': [{'name': '空课程', 'course_ids': []}],
            'ms_preference_start': (datetime.now() + timedelta(days=1)).isoformat(timespec='seconds')})
        self.assertEqual(response.status_code, 400, response.json)
        self.assertIn('志愿开始', response.json['message'])
        self.camp.mentor_selection_enabled = False
        db.session.commit()
        response = client.put(path, headers=headers, json={'ms_tags': [{'name': '空课程', 'course_ids': []}]})
        self.assertEqual(response.status_code, 200, response.json)
        self.camp.status = 'archived'
        db.session.commit()
        response = client.put(path, headers=headers, json={'ms_tags': [{'name': '归档不可改', 'course_ids': []}]})
        self.assertEqual(response.status_code, 400, response.json)

    def test_create_camp_with_empty_course_direction(self):
        from models import CampCycle
        cycle = CampCycle(code='empty-course-test', name='空课程测试周期')
        db.session.add(cycle)
        db.session.commit()
        client, headers = self.client_as(self.actor)
        response = client.post('/camp/sessions', headers=headers, json={
            'name': '空课程方向营', 'cycle_id': cycle.id, 'category': 'learning',
            'start_date': date.today().isoformat(),
            'end_date': (date.today() + timedelta(days=30)).isoformat(),
            'mentor_selection_enabled': True,
            'ms_preference_start': (datetime.now() + timedelta(days=1)).isoformat(timespec='seconds'),
            'ms_preference_deadline': (datetime.now() + timedelta(days=2)).isoformat(timespec='seconds'),
            'ms_tags': [{'name': '先建方向', 'course_ids': []}]})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json['session']['ms_directions'], [{'name': '先建方向', 'course_ids': []}])

    def test_failed_api_does_not_commit_partial_membership(self):
        service.add_role(self.a,self.student.id,'learner',self.actor.id);db.session.commit()
        client,headers=self.client_as(self.actor)
        response=client.post(f'/camp/sessions/{self.camp.id}/training-groups/{self.b.id}/members',headers=headers,
            json={'user_id':self.student.id,'role':'learner','expected_version':self.b.camp_group.version})
        self.assertEqual(response.status_code,409,response.json)
        self.assertEqual(access.learner_units(self.camp.id,self.student.id),[self.a.id])
        self.assertEqual(CampCourseAssignment.query.one().unit_id,self.a.id)


if __name__=='__main__':
    unittest.main()

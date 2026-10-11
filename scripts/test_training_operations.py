"""第四部分运营收口定向检查；仅 SQLite 内存、模拟存储/通知，不连接业务服务。"""
import unittest
import csv
import io
from datetime import date, datetime, timedelta
from unittest.mock import patch
from sqlalchemy.dialects import mysql
import test_training_groups as fixtures
from exts import db
import models as m
from services.camp_groups import training as service, training_access as access, training_history as history, maintenance


class OperationsSmoke(unittest.TestCase):
    setUp = fixtures.TrainingSmoke.setUp
    tearDown = fixtures.TrainingSmoke.tearDown
    make_user = fixtures.TrainingSmoke.make_user
    client_as = fixtures.TrainingSmoke.client_as

    def enroll(self):
        service.add_role(self.a, self.student.id, 'learner', self.actor.id)
        db.session.commit()

    def staff(self):
        teacher = self.make_user('camp_teacher')
        db.session.add(m.CampStaff(camp_session_id=self.camp.id, user_id=teacher.id,
                                 role='teacher', assigned_by=self.actor.id))
        db.session.commit()
        return teacher

    def meeting(self, *, old=False):
        created = datetime.now() - timedelta(days=1) if old else datetime.now()
        meeting = m.CampMeeting(camp_session_id=self.camp.id, scope='team', unit_id=self.a.id,
            mentor_id=self.owner.id, title='例会', meeting_date=date.today(), created_by=self.owner.id,
            chapter_due_at=datetime.now()+timedelta(hours=2))
        db.session.add(meeting); db.session.flush()
        task = m.CampMeetingTask(meeting_id=meeting.id, title='练习', submit_type='text',
            created_by=self.owner.id, created_at=created, required=True,
            due_at=datetime.now()+timedelta(hours=2))
        plan = m.CampMeetingChapterPlan(meeting_id=meeting.id, chapter_id=self.chapter.id,
            course_id=self.course.id, created_at=created)
        db.session.add_all([task, plan]); db.session.commit()
        return meeting, task, plan

    def material(self):
        from blueprints.camp_material import bp
        self.app.register_blueprint(bp)
        material = m.CampChapterMaterial(camp_session_id=self.camp.id, student_user_id=self.student.id,
            chapter_id=self.chapter.id, course_id=self.course.id, content='私有材料')
        db.session.add(material); db.session.flush()
        att = m.CampChapterMaterialAttachment(material_id=material.id, object_key='fixture/no-network',
                                             filename='sample.txt', content_type='text/plain')
        db.session.add(att); db.session.commit()
        return material, att

    def test_staff_history_and_local_certification(self):
        self.enroll()
        teacher = self.staff()
        assignment = m.CampCourseAssignment.query.one()
        assignment.status = 'ended'
        member = m.CampMember.query.filter_by(user_id=self.student.id).one()
        member.status = 'removed'
        db.session.add(m.CampChapterCertification(camp_session_id=self.camp.id, student_user_id=self.student.id,
            course_id=self.course.id, chapter_id=self.chapter.id, mentor_user_id=self.owner.id, score=0))
        db.session.commit()
        client, headers = self.client_as(teacher)
        url = f'/camp/sessions/{self.camp.id}/progress/board'
        self.assertEqual(client.get(url, headers=headers).json['summary']['student_count'], 0)
        result = client.get(url+'?include_history=1', headers=headers)
        self.assertEqual(result.status_code, 200, result.json)
        row = result.json['groups'][0]['students'][0]
        self.assertEqual(row['membership_status'], 'removed')
        self.assertEqual(row['courses'][0]['assignment_status'], 'ended')
        self.assertEqual(row['courses'][0]['chapters'][0]['score'], 0)
        self.assertFalse(history.include_history(self.camp, self.owner, True))
        self.camp.status='archived'; db.session.commit()
        self.assertTrue(client.get(url, headers=headers).json['include_history'])

    def test_global_course_completion_does_not_finish_this_camp(self):
        from blueprints.camp import _course_block
        self.enroll()
        row = m.UserCourseModel.query.filter_by(user_id=self.student.id,course_id=self.course.id).first()
        if row is None:
            row=m.UserCourseModel(user_id=self.student.id,course_id=self.course.id)
            db.session.add(row)
        row.status='completed';row.camp_session_id=None;db.session.commit()
        self.assertEqual(_course_block(self.camp,self.course.id,self.student.id)['course_status'],'active')

    def test_staff_historical_attendance_export_is_deduplicated(self):
        self.enroll(); teacher=self.staff()
        m.CheckRecord.__table__.create(db.engine,checkfirst=True)
        db.session.add(m.CampAttendancePlan(camp_session_id=self.camp.id,user_id=self.student.id,
                                           date=date.today(),source='self_selected'))
        member=m.CampMember.query.filter_by(user_id=self.student.id).one()
        member.status='removed';member.attendance_required=False
        db.session.commit()
        self.assertEqual(history.attendance_ids(self.camp,teacher,history=True),[self.student.id])
        client,headers=self.client_as(teacher)
        response=client.get(f'/camp/attendance/export/{self.camp.id}?include_history=1',headers=headers)
        self.assertEqual(response.status_code,200,response.json)
        rows=list(csv.reader(io.StringIO(response.data.decode('utf-8-sig'))))
        self.assertEqual(sum(str(self.student.id) == row[0] for row in rows),1)

    def test_staff_material_read_only_and_stale_mentor_link(self):
        self.enroll(); teacher=self.staff();material,att=self.material()
        client,headers=self.client_as(self.owner)
        path=f'/camp/materials/attachments/{att.id}'
        token=client.get(path+'/token',headers=headers)
        self.assertEqual(token.status_code,200,token.json)
        url=token.json['url']
        service.add_role(self.b,self.student.id,'learner',self.actor.id,transfer_courses=True)
        db.session.commit()
        self.assertEqual(client.get(url).status_code,403)
        with patch('services.auth_context.current_actor',return_value=type('Actor',(),{'user':teacher})()):
            listing=client.get(f'/camp/sessions/{self.camp.id}/materials?student_user_id={self.student.id}',headers=headers)
            self.assertEqual(len(listing.json['materials']),1)
            self.assertEqual(client.delete(f'/camp/materials/{material.id}',headers=headers).status_code,403)
            token=client.get(path+'/token',headers=headers)
            self.assertEqual(token.status_code,200,token.json)
            with patch('blueprints.camp_material.storage.get_object',return_value=io.BytesIO(b'fixture')):
                downloaded=client.get(token.json['url'])
                self.assertEqual(downloaded.status_code,200)
                self.assertEqual(downloaded.data,b'fixture')
        with patch('services.auth_context.current_actor',return_value=type('Actor',(),{'user':self.other})()):
            # 新责任导生可读；解除本营老师资格则其短签也立即失效。
            self.assertEqual(client.get(path+'/token',headers=headers).status_code,200)
        m.CampStaff.query.filter_by(user_id=teacher.id).one().status='ended';db.session.commit()
        self.assertEqual(client.get(token.json['url']).status_code,403)

    def test_newcomer_is_not_given_old_tasks_or_reminders(self):
        self.enroll();meeting,task,plan=self.meeting(old=True)
        client,headers=self.client_as(self.student)
        detail=client.get(f'/camp/meetings/{meeting.id}/detail',headers=headers).json
        self.assertFalse(detail['tasks'][0]['required_for_me'])
        self.assertFalse(detail['chapters'][0]['required_for_me'])
        from blueprints import camp_scheduler as scheduler
        with patch.object(scheduler,'_redis_guard',return_value=True) as cursor, patch('blueprints.notification.create_notification') as notify:
            self.assertEqual(scheduler._task_deadline_scan(),0)
            self.assertEqual(scheduler._chapter_due_scan(),0)
            cursor.assert_not_called();notify.assert_not_called()

    def test_existing_student_reminded_then_exit_stops_reminders(self):
        self.enroll();meeting,task,plan=self.meeting()
        from blueprints import camp_scheduler as scheduler
        with patch.object(scheduler,'_redis_guard',return_value=True), patch('blueprints.notification.create_notification') as notify:
            self.assertEqual(scheduler._task_deadline_scan(),1)
            self.assertEqual(scheduler._chapter_due_scan(),1)
            self.assertEqual([c.args[0] for c in notify.call_args_list],[self.student.id]*2)
            service.remove_role(self.a,self.student.id,'learner',self.actor.id);db.session.commit()
            notify.reset_mock()
            self.assertEqual(scheduler._task_deadline_scan(),0)
            self.assertEqual(scheduler._chapter_due_scan(),0)
            notify.assert_not_called()

    def test_removed_student_submission_remains_and_material_zip_is_scoped(self):
        self.enroll();meeting,task,plan=self.meeting();material,att=self.material()
        db.session.add(m.CampMeetingTaskSubmission(task_id=task.id,student_user_id=self.student.id,content='历史提交'))
        db.session.commit()
        service.add_role(self.b,self.student.id,'learner',self.actor.id,transfer_courses=True)
        service.remove_role(self.a,self.student.id,'learner',self.actor.id);db.session.commit()
        client,headers=self.client_as(self.owner)
        detail=client.get(f'/camp/meetings/{meeting.id}/detail',headers=headers).json
        self.assertEqual(detail['tasks'][0]['submission_count'],1)
        self.assertEqual(detail['tasks'][0]['expected_count'],1)
        self.assertIn(str(self.student.id),detail['tasks'][0]['submissions'])
        self.assertNotIn(self.student.id,detail['chapters'][0]['visible_user_ids'])
        from blueprints.camp_meeting import _planned_materials
        self.assertEqual(_planned_materials(meeting,[self.student],self.owner)[1],{})
        roster=history.group_roster(self.a)
        row=next(r for r in roster if r['user_id']==self.student.id)
        self.assertTrue(all(not r['active'] for r in row['roles']))

    def test_edit_retains_chapter_assignment_time(self):
        self.enroll();meeting,task,plan=self.meeting(old=True)
        created=plan.created_at
        client,headers=self.client_as(self.owner)
        with patch('blueprints.camp_meeting.create_notification'):
            response=client.put(f'/camp/meetings/{meeting.id}/assignments',headers=headers,json={
                'chapters':[self.chapter.id], 'tasks':[{'id':task.id,'title':task.title,'submit_type':'text'}]})
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(m.CampMeetingChapterPlan.query.one().created_at,created)
        self.assertFalse(response.json['chapters'][0]['expected_count'])

    def test_shared_http_and_exclusive_cutover_sql(self):
        table=m.CampMaintenanceFence.__table__
        shared=str(maintenance.fence_statement(table,[self.camp.id],shared=True).compile(dialect=mysql.dialect()))
        exclusive=str(maintenance.fence_statement(table,[self.camp.id],shared=False,nowait=True).compile(dialect=mysql.dialect()))
        self.assertIn('LOCK IN SHARE MODE',shared)
        self.assertIn('FOR UPDATE NOWAIT',exclusive)
        self.assertNotIn('camp_session.',shared)
        maintenance.install(self.app)
        client,headers=self.client_as(self.actor)
        with patch.object(maintenance,'leases',wraps=maintenance.leases) as lease:
            response=client.get(f'/camp/sessions/{self.camp.id}/training-groups',headers=headers)
            self.assertEqual(response.status_code,200,response.json)
            self.assertTrue(lease.call_args.kwargs['shared'])


class LegacyOperationsSmoke(unittest.TestCase):
    # 复用旧营隔离夹具，不继承并重复执行其整组用例。
    import test_training_cutover as legacy
    setUp = legacy.TrainingCutoverSmoke.setUp
    tearDown = legacy.TrainingCutoverSmoke.tearDown
    make_user = legacy.TrainingCutoverSmoke.make_user
    map = legacy.TrainingCutoverSmoke.map

    def test_mapping_keeps_old_task_eligibility_without_assigning_to_later_joiners(self):
        from services.camp_groups import training_migration as migration
        self.learner.joined_at=datetime.now()-timedelta(days=4)
        m.CampMentorProfile.query.one().capacity=2
        task=m.CampMeetingTask(meeting_id=self.meeting.id,title='切换前作业',submit_type='text',
            created_by=self.owner.id,created_at=datetime.now()-timedelta(days=2))
        db.session.add(task);db.session.commit()
        run,_=self.map()
        migration.switch(self.camp.id,run.id,self.actor.id);db.session.commit()
        migration.resume(self.camp.id,run.id,self.actor.id);db.session.commit()
        self.assertEqual(access.meeting_students(self.meeting,assigned_at=task.created_at),[self.student.id])
        newcomer=self.make_user('newcomer')
        db.session.add(m.CampMember(camp_session_id=self.camp.id,user_id=newcomer.id,role='student'))
        db.session.flush()
        unit=db.session.get(m.CampUnit,self.meeting.unit_id)
        service.add_role(unit,newcomer.id,'learner',self.actor.id);db.session.commit()
        self.assertEqual(access.meeting_students(self.meeting,assigned_at=task.created_at),[self.student.id])


if __name__ == '__main__':
    unittest.main()

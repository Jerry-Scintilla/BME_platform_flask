"""阶段 4 定向检查：仅 SQLite 内存，不连接业务数据库，不执行真实迁移。"""
import unittest
from datetime import date, datetime, timedelta
from unittest.mock import patch
from sqlalchemy import select
import test_training_groups  # 注册隔离 SQLite 的 LONGTEXT 编译
import test_group_core as foundation
from exts import db
import models as m
from services.camp_groups import training_migration as migration, maintenance
from services.groups import GroupError


class TrainingCutoverSmoke(unittest.TestCase):
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
        for table in [*migration.snapshot_tables().values(), m.TrainingMigrationRun.__table__,
                      m.TrainingGroupProfile.__table__, m.CampTeachingEvent.__table__]:
            include(table)
        db.metadata.create_all(db.engine,tables=list(tables))
        self.course = m.CourseModel(title='旧课程',introduction='fixture',creator_id=self.actor.id)
        db.session.add(self.course);db.session.flush()
        self.camp = m.CampSession(name='旧大一营',category='learning',status='running',
            start_date=date.today(),end_date=date.today()+timedelta(days=30),
            ms_tags='[{"name":"编程","course_ids":['+str(self.course.id)+']}]')
        db.session.add(self.camp);db.session.flush()
        self.mentor = m.CampMember(camp_session_id=self.camp.id,user_id=self.owner.id,role='mentor')
        self.learner = m.CampMember(camp_session_id=self.camp.id,user_id=self.student.id,role='student',team_mentor_id=self.owner.id)
        self.assignment = m.CampCourseAssignment(camp_session_id=self.camp.id,student_user_id=self.student.id,course_id=self.course.id,source_ref_id=self.owner.id)
        self.meeting = m.CampMeeting(camp_session_id=self.camp.id,scope='team',mentor_id=self.owner.id,
            title='旧组会',meeting_date=date.today(),created_by=self.owner.id)
        db.session.add_all([self.mentor,self.learner,self.assignment,self.meeting,
            m.CampMentorProfile(camp_session_id=self.camp.id,user_id=self.owner.id,tags='["编程"]',capacity=1),
            m.CampAttendancePlan(camp_session_id=self.camp.id,user_id=self.student.id,date=date.today(),source='self_selected')])
        db.session.flush()
        self.chapter = m.Chapter(course_id=self.course.id,name='章节',level=2)
        db.session.add(self.chapter);db.session.flush()
        db.session.add_all([
            m.CampChapterCertification(camp_session_id=self.camp.id,student_user_id=self.student.id,
                chapter_id=self.chapter.id,course_id=self.course.id,mentor_user_id=self.owner.id,score=0),
            m.CampMeetingAttachment(meeting_id=self.meeting.id,object_key='unchanged/private.pdf',filename='资料.pdf')])
        db.session.commit()

    def map(self, decisions=None):
        report=migration.preview(self.camp.id,decisions)
        self.assertEqual(report['conflicts'],[])
        run=migration.freeze(self.camp.id,self.actor.id);db.session.commit()
        run=migration.apply_mapping(self.camp.id,run.id,actor_user_id=self.actor.id,
            expected_digest=report['digest'],decisions=decisions)
        db.session.commit()
        return run,report

    def test_map_switch_restore_preserves_facts_and_retry(self):
        before=migration.snapshot(self.camp.id)
        run,report=self.map()
        self.assertFalse(self.camp.training_groups_ready)
        self.assertTrue(migration.verify(self.camp.id,run.id)['recovery_allowed'])
        mapped=self.assignment.unit_id
        self.assertEqual(self.learner.recruitment_unit_id,mapped)
        self.assertEqual(self.meeting.unit_id,mapped)
        self.assertIsNone(self.mentor.attendance_required)
        self.assertEqual(m.CampAttendancePlan.query.count(),1)
        self.assertEqual(m.CampChapterCertification.query.one().score,0)
        count=m.GroupMembershipEvent.query.count()
        migration.apply_mapping(self.camp.id,run.id,actor_user_id=self.actor.id,expected_digest=report['digest'])
        self.assertEqual(m.GroupMembershipEvent.query.count(),count)
        migration.switch(self.camp.id,run.id,self.actor.id);db.session.commit()
        self.assertTrue(self.camp.training_groups_ready)
        self.assertTrue(self.camp.training_migration_paused)
        migration.resume(self.camp.id,run.id,self.actor.id);db.session.commit()
        migration.freeze(self.camp.id,self.actor.id);db.session.commit()
        migration.restore(self.camp.id,run.id,self.actor.id);db.session.commit()
        self.assertFalse(self.camp.training_groups_ready)
        self.assertIsNone(self.assignment.unit_id)
        self.assertIsNone(self.learner.recruitment_unit_id)
        self.assertIsNone(self.meeting.unit_id)
        self.assertEqual(migration.facts(migration.snapshot(self.camp.id),run.mapping),migration.facts(before))
        self.assertEqual(m.CampMeetingAttachment.query.one().object_key,'unchanged/private.pdf')
        self.assertEqual(m.GroupMembership.query.filter_by(status='ended').count(),2)
        migration.restore(self.camp.id,run.id,self.actor.id)
        migration.resume(self.camp.id,run.id,self.actor.id);db.session.commit()
        self.assertFalse(self.camp.training_migration_paused)
        # 恢复档案保留，后续可以建立新的批次再预览。
        retry=migration.freeze(self.camp.id,self.actor.id);db.session.commit()
        self.assertNotEqual(retry.id,run.id)
        self.assertEqual(migration.preview(self.camp.id)['conflicts'],[])
        with self.assertRaises(GroupError) as caught:
            migration.resume(self.camp.id,run.id,self.actor.id)
        self.assertEqual(caught.exception.machine,'MIGRATION_RUN_STALE')

    def test_stale_preview_and_conflicts_do_not_map(self):
        report=migration.preview(self.camp.id)
        run=migration.freeze(self.camp.id,self.actor.id);db.session.commit()
        self.meeting.title='变化';db.session.commit()
        with self.assertRaises(GroupError) as caught:
            migration.apply_mapping(self.camp.id,run.id,actor_user_id=self.actor.id,expected_digest=report['digest'])
        self.assertEqual(caught.exception.machine,'GROUP_MIGRATION_STALE')
        self.assertEqual(m.CampGroup.query.count(),0)
        self.learner.team_mentor_id=self.student.id;db.session.commit()
        self.assertTrue(migration.preview(self.camp.id)['conflicts'])
        migration.resume(self.camp.id,run.id,self.actor.id)
        self.assertEqual(run.state,'cancelled')

    def test_explicit_unassigned_course_decision(self):
        self.learner.team_mentor_id=None;db.session.commit()
        report=migration.preview(self.camp.id)
        self.assertIn(f'assignment_responsibility_unknown:{self.assignment.id}',report['conflicts'])
        decisions={'assignment_targets':{str(self.assignment.id):{'mentor_id':None,'reason':'原导生已释放，保留待分配修读'}}}
        run,_=self.map(decisions)
        self.assertIsNone(self.assignment.unit_id)
        self.assertTrue(migration.verify(self.camp.id,run.id)['baseline_equal'])

    def test_business_change_blocks_switch_or_restore(self):
        run,_=self.map()
        self.meeting.title='映射后发生业务修改';db.session.commit()
        with self.assertRaises(GroupError):
            migration.switch(self.camp.id,run.id,self.actor.id)
        with self.assertRaises(GroupError) as caught:
            migration.restore(self.camp.id,run.id,self.actor.id)
        self.assertEqual(caught.exception.machine,'MIGRATION_FORWARD_ONLY')
        self.assertEqual(self.meeting.title,'映射后发生业务修改')
        self.assertEqual(run.state,'mapped')

    def test_nested_rollback_leaves_no_partial_mapping(self):
        report=migration.preview(self.camp.id)
        run=migration.freeze(self.camp.id,self.actor.id);db.session.commit()
        with patch.object(migration,'state_digest',side_effect=RuntimeError('fixture failure')):
            with self.assertRaises(RuntimeError):
                migration.apply_mapping(self.camp.id,run.id,actor_user_id=self.actor.id,expected_digest=report['digest'])
        db.session.expire_all()
        self.assertEqual(m.CampGroup.query.count(),0)
        self.assertIsNone(self.assignment.unit_id)
        self.assertEqual(run.state,'frozen')

    def test_request_pause_and_resume_guard(self):
        from flask import Blueprint
        bp=Blueprint('camp_guard_fixture',__name__)
        @bp.route('/camp/sessions/<int:sid>/fixture',methods=['GET','POST'])
        def fixture(sid):
            return {'ok':True}
        self.app.register_blueprint(bp)
        maintenance.install(self.app)
        run=migration.freeze(self.camp.id,self.actor.id);db.session.commit()
        client=self.app.test_client()
        self.assertEqual(client.post(f'/camp/sessions/{self.camp.id}/fixture').status_code,423)
        self.assertEqual(client.get(f'/camp/sessions/{self.camp.id}/fixture').status_code,423)
        migration.resume(self.camp.id,run.id,self.actor.id);db.session.commit()
        self.assertEqual(client.post(f'/camp/sessions/{self.camp.id}/fixture').status_code,200)

    def test_snapshot_contains_children_and_is_read_only(self):
        report=migration.preview(self.camp.id)
        self.assertEqual(report['baseline']['camp_meeting_attachment']['count'],1)
        self.assertFalse(db.session.new)
        self.assertFalse(db.session.dirty)
        self.assertEqual(m.TrainingMigrationRun.query.count(),0)
        self.assertEqual(m.CampGroup.query.count(),0)
        with self.assertRaises(GroupError):
            migration.freeze(self.camp.id,self.student.id)

    def test_live_new_group_work_cannot_be_rolled_back(self):
        from services.camp_groups import training, training_access
        run,_=self.map()
        migration.switch(self.camp.id,run.id,self.actor.id)
        migration.resume(self.camp.id,run.id,self.actor.id);db.session.commit()
        self.assertTrue(training_access.can_teach(self.camp.id,self.owner.id,self.student.id,self.course.id))
        unit=training.create_group(self.camp.id,actor_id=self.actor.id,mentor_id=self.owner.id,name='切换后新组',direction='编程')
        db.session.commit()
        migration.freeze(self.camp.id,self.actor.id);db.session.commit()
        with self.assertRaises(GroupError) as caught:
            migration.restore(self.camp.id,run.id,self.actor.id)
        self.assertEqual(caught.exception.machine,'MIGRATION_FORWARD_ONLY')
        self.assertTrue(self.camp.training_groups_ready)
        self.assertEqual(db.session.get(m.CampUnit,unit.id).name,'切换后新组')
        self.assertEqual(len(migration.state_document(self.camp.id)['camp_group']),2)

    def test_paused_scheduler_does_not_claim_notification_cursors(self):
        from blueprints import camp_scheduler as scheduler
        migration.freeze(self.camp.id,self.actor.id);db.session.commit()
        with patch.object(scheduler,'_redis_guard') as cursor:
            scheduler._deadline_reminder_scan()
            scheduler._attendance_digest_scan(date.today(),force=True)
            scheduler._task_deadline_scan()
            scheduler._chapter_due_scan()
            cursor.assert_not_called()

    def test_archived_camp_keeps_status_and_original_times(self):
        former=self.make_user('former_mentor')
        db.session.add(m.CampMember(camp_session_id=self.camp.id,user_id=former.id,role='mentor',
            status='removed',ended_at=datetime.now()))
        old_meeting=m.CampMeeting(camp_session_id=self.camp.id,scope='team',mentor_id=former.id,
            title='已离营导生的历史组会',meeting_date=date.today(),created_by=former.id)
        db.session.add(old_meeting)
        self.camp.status='archived';db.session.commit()
        joined=self.learner.joined_at
        run,_=self.map()
        migration.switch(self.camp.id,run.id,self.actor.id);db.session.commit()
        self.assertEqual(self.camp.status,'archived')
        self.assertEqual(self.learner.joined_at,joined)
        self.assertTrue(migration.verify(self.camp.id,run.id)['baseline_equal'])
        unit=db.session.get(m.CampUnit,old_meeting.unit_id)
        self.assertEqual(unit.status,'terminated')
        self.assertEqual(unit.camp_group.status,'archived')
        self.assertIsNone(db.session.get(m.TrainingGroupProfile,unit.id).recruitment_mentor_id)
        self.assertEqual(m.GroupRoleAssignment.query.filter_by(group_id=unit.group_id).count(),0)


if __name__=='__main__':
    unittest.main()

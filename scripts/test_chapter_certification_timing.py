"""认证时效轻量回归：SQLite 内存、真实路由，不连接业务库或外部服务。"""
import unittest
from datetime import date, datetime, timedelta

import test_training_groups as fixtures
from exts import db
import models as m
from services.camp_groups import training, chapter_timing


class ChapterTimingTest(unittest.TestCase):
    setUp = fixtures.TrainingSmoke.setUp
    tearDown = fixtures.TrainingSmoke.tearDown
    make_user = fixtures.TrainingSmoke.make_user
    client_as = fixtures.TrainingSmoke.client_as

    def prepare(self, hours=-1):
        training.add_role(self.a, self.student.id, 'learner', self.actor.id)
        db.session.commit()
        meeting = m.CampMeeting(camp_session_id=self.camp.id, scope='team', unit_id=self.a.id,
            mentor_id=self.owner.id, title='认证时效', meeting_date=date.today(), created_by=self.owner.id,
            chapter_due_at=datetime.now()+timedelta(hours=hours) if hours is not None else None)
        db.session.add(meeting)
        db.session.flush()
        plan = m.CampMeetingChapterPlan(meeting_id=meeting.id, chapter_id=self.chapter.id, course_id=self.course.id)
        db.session.add(plan)
        db.session.commit()
        self.client, self.headers = self.client_as(self.owner)
        self.url = f'/camp/sessions/{self.camp.id}/team/progress/certify'
        return meeting, plan

    def certify(self, method='post', **extra):
        return getattr(self.client, method)(self.url, headers=self.headers, json=dict(
            student_user_id=self.student.id, chapter_id=self.chapter.id, **extra))

    def detail(self, meeting, role='member'):
        from blueprints.camp_meeting import _detail_payload
        return _detail_payload(meeting, self.student if role == 'member' else self.owner, role)

    def summary(self, meeting):
        from blueprints.camp_meeting import _meeting_summaries
        return _meeting_summaries(self.camp.id, [meeting.id], {meeting.id: meeting.chapter_due_at},
            self.student, [self.student.id], False)[meeting.id]

    def test_late_certification_preserves_submission_and_completion(self):
        meeting, plan = self.prepare()
        self.assertEqual(self.detail(meeting)['chapters'][0]['my_timing']['status'], 'overdue_pending_submission')
        material = m.CampChapterMaterial(camp_session_id=self.camp.id, student_user_id=self.student.id,
            chapter_id=self.chapter.id, course_id=self.course.id, content='作业',
            created_at=meeting.chapter_due_at-timedelta(minutes=10))
        db.session.add(material)
        db.session.commit()
        self.assertEqual(self.detail(meeting)['chapters'][0]['my_timing']['status'], 'overdue_pending_certification')
        self.assertEqual(self.summary(meeting)['my_chapter_overdue'], 1)
        result = self.certify(score=80)
        self.assertEqual(result.status_code, 200, result.json)
        timing = self.detail(meeting)['chapters'][0]['my_timing']
        self.assertEqual(timing['status'], 'certified_late')
        self.assertEqual(timing['submission_status'], 'submitted_on_time')
        self.assertEqual(self.summary(meeting)['my_chapter_overdue'], 0)
        self.assertEqual(self.summary(meeting)['my_chapter_late_certified'], 1)
        self.assertEqual(m.UserCourseModel.query.filter_by(user_id=self.student.id).one().status, 'completed')
        db.session.delete(material)
        db.session.commit()
        self.assertEqual(self.detail(meeting)['chapters'][0]['my_timing'], timing)

    def test_each_meeting_has_its_own_deadline(self):
        meeting, plan = self.prepare()
        other = m.CampMeeting(camp_session_id=self.camp.id, scope='team', unit_id=self.a.id,
            mentor_id=self.owner.id, title='另一次布置', meeting_date=date.today(), created_by=self.owner.id,
            chapter_due_at=datetime.now()+timedelta(days=1))
        db.session.add(other)
        db.session.flush()
        db.session.add(m.CampMeetingChapterPlan(meeting_id=other.id, chapter_id=self.chapter.id, course_id=self.course.id))
        db.session.commit()
        self.assertEqual(self.certify().status_code, 200)
        self.assertEqual(self.detail(meeting)['chapters'][0]['my_timing']['status'], 'certified_late')
        self.assertEqual(self.detail(other)['chapters'][0]['my_timing']['status'], 'certified_on_time')

    def test_deadline_edit_and_score_change_do_not_rewrite_result(self):
        meeting, plan = self.prepare()
        self.assertEqual(self.certify(score=60).status_code, 200)
        saved = self.detail(meeting)['chapters'][0]['my_timing']
        new_due = (datetime.now()+timedelta(days=3)).strftime('%Y-%m-%d %H:%M')
        result = self.client.put(f'/camp/meetings/{meeting.id}/assignments', headers=self.headers,
            json={'chapters': [self.chapter.id], 'chapter_due_at': new_due, 'tasks': []})
        self.assertEqual(result.status_code, 200, result.json)
        self.assertEqual(self.certify(score=90).status_code, 200)
        self.assertEqual(self.detail(meeting)['chapters'][0]['my_timing'], saved)
        event = m.CampTeachingEvent.query.filter_by(action='chapter_assignment').one()
        self.assertEqual(event.before['due_at'], saved['due_at'])
        self.assertEqual(event.actor_user_id, self.owner.id)
        self.assertEqual(len(result.json['chapter_assignment_history']), 1)

    def test_revoke_retains_audit_and_recertification_uses_current_deadline(self):
        meeting, plan = self.prepare()
        self.assertEqual(self.certify().status_code, 200)
        saved = self.detail(meeting)['chapters'][0]['my_timing']
        self.assertEqual(self.certify('delete').status_code, 200)
        event = m.CampTeachingEvent.query.filter_by(action='certification_revoke').one()
        self.assertEqual(event.before['meeting_timings'][str(plan.id)], saved)
        self.assertEqual(self.summary(meeting)['my_chapter_overdue'], 1)
        meeting.chapter_due_at = datetime.now()+timedelta(hours=1)
        db.session.commit()
        self.assertEqual(self.certify().status_code, 200)
        self.assertEqual(self.detail(meeting)['chapters'][0]['my_timing']['status'], 'certified_on_time')
        self.assertEqual(m.CampTeachingEvent.query.filter_by(action='certification').count(), 2)

    def test_historical_certification_is_not_inferred_after_score_change(self):
        meeting, plan = self.prepare()
        db.session.add(m.CampChapterCertification(camp_session_id=self.camp.id, student_user_id=self.student.id,
            chapter_id=self.chapter.id, course_id=self.course.id, mentor_user_id=self.owner.id))
        db.session.commit()
        self.assertEqual(self.certify(score=88).status_code, 200)
        self.assertEqual(self.detail(meeting)['chapters'][0]['my_timing']['status'], 'historical_certified')
        self.assertEqual(self.summary(meeting)['my_chapter_late_certified'], 0)

    def test_no_deadline_and_pending_states(self):
        meeting, plan = self.prepare(hours=None)
        self.assertEqual(self.detail(meeting)['chapters'][0]['my_timing']['status'], 'pending_submission')
        db.session.add(m.CampChapterMaterial(camp_session_id=self.camp.id, student_user_id=self.student.id,
            chapter_id=self.chapter.id, course_id=self.course.id, content='作业'))
        db.session.commit()
        self.assertEqual(self.detail(meeting)['chapters'][0]['my_timing']['status'], 'pending_certification')
        self.assertEqual(self.certify().status_code, 200)
        self.assertEqual(self.detail(meeting)['chapters'][0]['my_timing']['status'], 'certified')

    def test_exact_deadline_counts_as_on_time(self):
        meeting, plan = self.prepare()
        timing = chapter_timing.snapshot(meeting, meeting.chapter_due_at, self.owner.id, meeting.chapter_due_at)
        self.assertEqual(timing['status'], 'certified_on_time')
        self.assertEqual(timing['submission_status'], 'submitted_on_time')

    def test_archived_camp_still_blocks_certification(self):
        meeting, plan = self.prepare()
        self.camp.status = 'archived'
        db.session.commit()
        self.assertEqual(self.certify().status_code, 400)
        self.assertEqual(m.CampChapterCertification.query.count(), 0)

    def test_new_assignment_captures_existing_certification_without_reclassifying_old_plan(self):
        meeting, plan = self.prepare()
        db.session.add(m.CampChapterCertification(camp_session_id=self.camp.id, student_user_id=self.student.id,
            chapter_id=self.chapter.id, course_id=self.course.id, mentor_user_id=self.owner.id,
            certified_at=datetime.now()-timedelta(days=2)))
        other = m.CampMeeting(camp_session_id=self.camp.id, scope='team', unit_id=self.a.id,
            mentor_id=self.owner.id, title='新增布置', meeting_date=date.today(), created_by=self.owner.id)
        db.session.add(other)
        db.session.commit()
        result = self.client.put(f'/camp/meetings/{other.id}/assignments', headers=self.headers,
            json={'chapters': [self.chapter.id], 'chapter_due_at': datetime.now().strftime('%Y-%m-%d %H:%M'), 'tasks': []})
        self.assertEqual(result.status_code, 200, result.json)
        self.assertEqual(self.detail(other)['chapters'][0]['my_timing']['status'], 'certified_on_time')
        self.assertEqual(self.detail(meeting)['chapters'][0]['my_timing']['status'], 'historical_certified')

    def test_late_material_is_distinguished_and_unauthorized_mentor_cannot_certify(self):
        meeting, plan = self.prepare()
        db.session.add(m.CampChapterMaterial(camp_session_id=self.camp.id, student_user_id=self.student.id,
            chapter_id=self.chapter.id, course_id=self.course.id, content='迟交'))
        db.session.commit()
        from types import SimpleNamespace
        from unittest.mock import patch
        with patch('services.auth_context.current_actor', return_value=SimpleNamespace(user=self.other)):
            self.assertEqual(self.certify().status_code, 403)
        self.assertEqual(m.CampChapterCertification.query.count(), 0)
        self.assertEqual(self.certify().status_code, 200)
        self.assertEqual(self.detail(meeting)['chapters'][0]['my_timing']['submission_status'], 'submitted_late')

    def test_no_current_obligation_does_not_create_a_timing_snapshot(self):
        meeting, plan = self.prepare()
        plan.created_at = datetime.now()-timedelta(days=3)
        db.session.commit()
        self.assertEqual(self.certify().status_code, 200)
        self.assertEqual(self.detail(meeting)['chapters'][0]['my_timing']['status'], 'not_required')
        self.assertEqual(m.CampChapterCertification.query.one().meeting_timings, {})


if __name__ == '__main__':
    unittest.main()

"""项目总览定向检查：隔离SQLite；不启动应用、调度器或访问业务库。"""
import json
import unittest
from datetime import date, timedelta
from unittest.mock import patch
from sqlalchemy import event

import test_project_delivery as fixtures
from exts import db
import models as m
from blueprints import camp_delivery as delivery
from services.camp_groups import projects
from services.camp_groups.dashboard import build_dashboard


class ProjectDashboardTest(unittest.TestCase):
    make_user = fixtures.ProjectDeliveryTest.make_user
    tearDown = fixtures.ProjectDeliveryTest.tearDown
    unit = fixtures.ProjectDeliveryTest.unit
    join_camp = fixtures.ProjectDeliveryTest.join_camp
    call = fixtures.ProjectDeliveryTest.call
    submit = fixtures.ProjectDeliveryTest.submit
    review = fixtures.ProjectDeliveryTest.review
    add_student = fixtures.ProjectDeliveryTest.add_student
    evaluate = fixtures.ProjectDeliveryTest.evaluate

    def setUp(self):
        fixtures.ProjectDeliveryTest.setUp(self)
        self.teacher = self.make_user('management_teacher')
        self.staff = m.CampStaff(camp_session_id=self.camp.id, user_id=self.teacher.id, role='teacher', status='active', assigned_by=self.actor.id)
        db.session.add(self.staff)
        db.session.commit()

    def board(self, user=None, suffix=''):
        return self.call(user or self.teacher, 'get', f'/camp/projects/{self.camp.id}/dashboard{suffix}')

    def test_staff_scope_revocation_and_read_only(self):
        self.assertEqual(self.board().status_code, 200)
        self.assertEqual(self.board(self.owner).status_code, 403)
        self.assertEqual(self.board(self.student).status_code, 403)
        self.assertEqual(self.board(self.actor).status_code, 200)
        self.staff.status = 'ended'; db.session.commit()
        self.assertEqual(self.board().status_code, 403)
        self.staff.status = 'active'; self.staff.camp_session_id = self.camp.id + 100
        # SQLite FK forbids a fake camp; create the distinct real scope.
        db.session.add(m.CampSession(id=self.camp.id + 100, name='另一个营', category='project', status='running', start_date=date.today(), end_date=date.today()))
        db.session.commit()
        self.assertEqual(self.board().status_code, 403)

    def test_camp_type_not_found_and_unauthenticated(self):
        self.assertEqual(self.client.get(f'/camp/projects/{self.camp.id}/dashboard').status_code, 401)
        self.assertEqual(self.call(self.actor, 'get', '/camp/projects/9999/dashboard').status_code, 404)
        self.camp.category = 'learning'; db.session.commit()
        self.assertEqual(self.board().status_code, 400)

    def test_solo_latest_submission_deadline_and_pending_queue(self):
        self.node.due_date = date.today() - timedelta(days=1); db.session.commit()
        first = self.submit().json['submission']['id']
        self.review(first, 'return', note='补齐材料')
        second = self.submit(content='新版').json['submission']['id']
        response = self.board(); self.assertEqual(response.status_code, 200, response.json)
        data = response.json
        self.assertEqual(data['summary']['pending_reviews'], 1)
        self.assertEqual(data['summary']['overdue_nodes'], 1)
        self.assertEqual(data['projects'][0]['nodes'][0]['submitted_count'], 1)
        self.review(second)
        data = self.board().json
        self.assertEqual(data['summary']['completion_rate'], 100)
        self.assertEqual(data['summary']['pending_reviews'], 0)
        self.assertEqual(data['summary']['overdue_nodes'], 0)

    def test_current_members_zero_score_and_pending_evaluations(self):
        self.add_student()
        self.submit(self.student)
        data = self.board().json
        self.assertEqual(data['summary']['pending_reviews'], 0)
        self.assertEqual(data['summary']['pending_evaluations'], 1)
        self.evaluate(score=0)
        self.assertEqual(self.board().json['summary']['completion_rate'], 100)
        other = self.make_user('new_member')
        self.add_student(other)
        self.assertEqual(self.board().json['summary']['completion_rate'], 0)
        projects.end_member(self.project, other.id, 'remove', 'admin', self.actor.id, '检查当前名单')
        db.session.commit()
        self.assertEqual(self.board().json['summary']['completion_rate'], 100)

    def test_today_not_overdue_and_paused_projects_have_no_live_tasks(self):
        self.node.due_date = date.today(); db.session.commit()
        self.submit()
        self.assertFalse(self.board().json['projects'][0]['nodes'][0]['overdue'])
        self.node.due_date -= timedelta(days=1)
        projects.set_status(self.project, 'paused', self.actor.id, '暂停检查')
        db.session.commit()
        data = self.board().json
        self.assertEqual(data['summary']['pending_reviews'], 0)
        self.assertEqual(data['summary']['overdue_nodes'], 0)
        self.assertEqual(data['projects'][0]['status'], 'paused')

    def test_missing_nodes_is_not_completed_and_empty_camp_keeps_member_count(self):
        db.session.delete(self.node); db.session.commit()
        data = self.board().json
        self.assertIsNone(data['summary']['completion_rate'])
        self.assertEqual(data['summary']['completed_projects'], 0)
        self.assertEqual(data['summary']['unconfigured_projects'], 1)
        self.assertIn('unconfigured', data['projects'][0]['risks'])
        projects.set_status(self.project, 'terminated', self.actor.id, '检查保留历史')
        db.session.commit()
        # An independent empty camp still counts its admitted members.
        empty = m.CampSession(name='仅有入营成员', category='project', status='running', start_date=date.today(), end_date=date.today())
        db.session.add(empty); db.session.flush()
        db.session.add(m.CampMember(camp_session_id=empty.id, user_id=self.student.id, role='member'))
        db.session.commit()
        self.assertEqual(build_dashboard(empty)['summary']['participant_count'], 1)

    def test_matrix_keeps_duplicate_titles_and_custom_stages(self):
        db.session.add_all([m.CampMilestone(camp_session_id=self.camp.id, unit_id=self.project.id, title='研发节点', order_no=2),
                           m.CampMilestone(camp_session_id=self.camp.id, unit_id=self.project.id, title='临床调研', order_no=3)])
        db.session.commit()
        data = self.board().json
        self.assertEqual([c['title'] for c in data['columns']], ['研发节点', '研发节点（2）', '临床调研'])
        self.assertEqual(len({n['stage_key'] for n in data['projects'][0]['nodes']}), 3)

    def test_details_latest_material_and_teacher_download_revocation(self):
        import io
        with patch.object(delivery.storage, 'put_object', return_value=None):
            submitted = self.call(self.owner, 'post', f'/camp/milestones/{self.node.id}/submissions',
                                  data={'content': '材料详情', 'Files': (io.BytesIO(b'example'), 'evidence.txt')})
        self.assertEqual(submitted.status_code, 200, submitted.json)
        detail = self.board(suffix=f'/{self.project.id}').json['project']
        latest = detail['nodes'][0]['submissions'][0]
        self.assertEqual(latest['content'], '材料详情')
        self.assertEqual(len(detail['nodes'][0]['submissions']), 1)
        self.assertNotIn('submissions', self.board().json['projects'][0]['nodes'][0])
        self.assertNotIn('object_key', latest['attachments'][0])
        aid = latest['attachments'][0]['id']
        token = self.call(self.teacher, 'get', f'/camp/submissions/attachments/{aid}/token')
        self.assertEqual(token.status_code, 200)
        with patch.object(delivery.storage, 'get_object', return_value=b'example'):
            self.assertEqual(self.client.get(token.json['url']).status_code, 200)
        self.assertEqual(self.call(self.teacher, 'post', f'/camp/submissions/{latest["id"]}/review', json={'action': 'approve'}).status_code, 403)
        self.staff.status = 'ended'; db.session.commit()
        self.assertEqual(self.call(self.teacher, 'get', f'/camp/submissions/attachments/{aid}/token').status_code, 403)
        self.assertEqual(self.client.get(token.json['url']).status_code, 403)

    def test_detail_rejects_cross_camp_and_invalid_project_keys(self):
        self.assertEqual(self.board(suffix='/999999').status_code, 404)
        self.assertEqual(self.board(suffix='/archive-0').status_code, 404)

    def test_archive_is_frozen_and_missing_archive_is_explicit(self):
        sub_id = self.submit().json['submission']['id']; self.review(sub_id)
        self.camp.status = 'archived'
        archive = delivery.freeze_camp_archive(self.camp, self.actor.id)
        self.node.title = '后来的标题'; self.node.status = 'open'; db.session.commit()
        data = self.board().json
        self.assertTrue(data['archived'])
        self.assertEqual(data['summary']['completion_rate'], 100)
        self.assertEqual(data['summary']['pending_reviews'], 0)
        self.assertEqual(data['columns'][0]['title'], '研发节点')
        self.assertEqual(self.board(suffix='/archive-0').status_code, 200)
        self.assertEqual(self.board(suffix=f'/{self.project.id}').status_code, 404)
        db.session.delete(archive); db.session.commit()
        self.assertEqual(self.board().status_code, 409)

    def test_query_count_does_not_grow_with_node_count(self):
        for index in range(40):
            db.session.add(m.CampMilestone(camp_session_id=self.camp.id, unit_id=self.project.id, title=f'节点{index}', order_no=index + 2))
        db.session.commit()
        statements = []
        def record(_conn, _cursor, statement, *_args):
            if statement.lstrip().upper().startswith('SELECT'): statements.append(statement)
        event.listen(db.engine, 'before_cursor_execute', record)
        try:
            data = build_dashboard(self.camp)
        finally:
            event.remove(db.engine, 'before_cursor_execute', record)
        self.assertEqual(data['summary']['total_nodes'], 41)
        self.assertLessEqual(len(statements), 16)


if __name__ == '__main__':
    unittest.main()

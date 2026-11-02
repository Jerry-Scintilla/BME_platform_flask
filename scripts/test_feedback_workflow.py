"""工单现场与处理流程：隔离 SQLite / 内存存储，不导入生产 app 或连接业务数据库。"""
import io
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

# 蓝图导入会校验配置；隔离测试不依赖开发者的 .env 或真实密钥。
os.environ.setdefault('JWT_SECRET', 'isolated-feedback-workflow-secret-20261101')
os.makedirs('log', exist_ok=True)

from flask import Flask
from flask_jwt_extended import JWTManager, create_access_token
from exts import db
from models import (UserModel, FeedbackTicket, FeedbackTicketAttachment,
                    FeedbackTicketMessage, FeedbackTicketEvent, PermissionModel, UserPermissionModel)
from blueprints.feedback_tickets import bp as user_bp
from blueprints.feedback_tickets_admin import bp as admin_bp


class FeedbackWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
                               JWT_SECRET_KEY='isolated-feedback-workflow-secret-20261101')
        db.init_app(self.app)
        JWTManager(self.app)
        self.app.register_blueprint(user_bp)
        self.app.register_blueprint(admin_bp)
        self.context = self.app.app_context()
        self.context.push()
        tables = [UserModel, FeedbackTicket, FeedbackTicketAttachment, FeedbackTicketMessage,
                  FeedbackTicketEvent, PermissionModel, UserPermissionModel]
        db.metadata.create_all(db.engine, tables=[model.__table__ for model in tables])
        self.admin = self.make_user('admin', 'super_admin')
        self.reporter = self.make_user('reporter')
        self.other = self.make_user('other')
        db.session.commit()
        self.client = self.app.test_client()
        self.objects = {}
        self.patches = [patch('blueprints._audit_write'),
                        patch('blueprints.feedback_tickets._notify'),
                        patch('blueprints.feedback_tickets_admin._notify'),
                        patch('storage.storage.put_object', side_effect=self.put),
                        patch('storage.storage.get_object', side_effect=lambda key: io.BytesIO(self.objects[key])),
                        patch('storage.storage.remove_object', side_effect=lambda key: self.objects.pop(key, None))]
        self.mocks = [item.start() for item in self.patches]

    def make_user(self, name, role='user'):
        user = UserModel(username=name, email=name + '@fixture.invalid', password='fixture',
                         role=role, status='active', lifecycle='active')
        db.session.add(user)
        return user

    def put(self, key, stream, **kwargs):
        self.objects[key] = stream.read()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def call(self, actor, method, path, **kwargs):
        token = create_access_token(identity=actor.email)
        with patch('services.auth_context.current_actor', return_value=SimpleNamespace(user=actor)):
            return getattr(self.client, method)(path, headers={'Authorization': 'Bearer ' + token}, **kwargs)

    def create(self, **fields):
        payload = {'title': '页面按钮无法点击', 'description': '点击保存后没有任何变化，请处理。', **fields}
        response = self.call(self.reporter, 'post', '/feedback-tickets', data=payload, content_type='multipart/form-data')
        self.assertEqual(response.status_code, 200, response.json)
        return response.json['data']['id']

    def transition(self, tid, to, **extra):
        return self.call(self.admin, 'post', f'/admin/feedback-tickets/{tid}/transitions', json={'to': to, **extra})

    def test_snapshot_upload_context_and_attachment_access(self):
        tid = self.create(image=(io.BytesIO(b'upload'), 'upload.png', 'image/png'),
            snapshot=(io.BytesIO(b'snapshot'), 'page-snapshot.jpg', 'image/jpeg'),
            page_context=json.dumps({'url': 'https://bme.test/AMEII/camp?sid=4&tab=points&token=secret#password',
                                     'viewport_width': 390, 'theme': 'dark', 'unknown': 'discard'}))
        result = self.call(self.admin, 'get', f'/admin/feedback-tickets/{tid}').json['ticket']
        self.assertEqual(result['page_context']['url'], 'https://bme.test/AMEII/camp?sid=4&tab=points')
        self.assertNotIn('unknown', result['page_context'])
        self.assertEqual({a['kind'] for a in result['attachments']}, {'snapshot', 'upload'})
        attachment = next(a for a in result['attachments'] if a['kind'] == 'snapshot')
        self.assertIn('st=', attachment['url'])
        self.assertEqual(self.client.get(attachment['url']).data, b'snapshot')
        raw_url = '/feedback-tickets/attachments/' + str(attachment['id'])
        self.assertEqual(self.call(self.other, 'get', raw_url).status_code, 403)
        self.assertEqual(self.call(self.reporter, 'get', raw_url).data, b'snapshot')
        self.assertEqual(self.call(self.other, 'get', f'/feedback-tickets/{tid}').status_code, 403)
        self.assertEqual(self.create(), tid)
        self.assertEqual(FeedbackTicketAttachment.query.count(), 2)

    def test_invalid_attachment_and_context_do_not_write_partial_ticket(self):
        response = self.call(self.reporter, 'post', '/feedback-tickets', data={
            'title': '非法附件', 'image': (io.BytesIO(b'valid'), 'first.png', 'image/png'),
            'snapshot': (io.BytesIO(b'bad'), 'bad.svg', 'image/svg+xml')})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(FeedbackTicket.query.count(), 0)
        self.assertFalse(self.objects)
        response = self.call(self.reporter, 'post', '/feedback-tickets', data={
            'title': '非法上下文', 'page_context': '{broken'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(FeedbackTicket.query.count(), 0)
        response = self.call(self.reporter, 'post', '/feedback-tickets', data={
            'title': '页面信息过长', 'page_context': json.dumps({'title': '汉' * 3000}, ensure_ascii=False)})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(FeedbackTicket.query.count(), 0)
        response = self.call(self.reporter, 'post', '/feedback-tickets', data={
            'title': '大图片', 'snapshot': (io.BytesIO(b'x' * (5 * 1024 * 1024 + 1)), 'large.png', 'image/png')})
        self.assertEqual(response.status_code, 400)

    def test_arrangement_validation_is_atomic_and_notifications_do_not_repeat(self):
        tid = self.create()
        path = f'/admin/feedback-tickets/{tid}/triage'
        bad = self.call(self.admin, 'patch', path, json={'priority': 'urgent', 'assignee_user_id': self.other.id})
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(db.session.get(FeedbackTicket, tid).priority, 'medium')
        good = self.call(self.admin, 'patch', path, json={'priority': 'high', 'assignee_user_id': self.admin.id, 'expected_status': 'new'})
        self.assertEqual(good.status_code, 200)
        self.assertEqual(self.mocks[2].call_count, 2)
        saved = self.call(self.admin, 'patch', path, json={'priority': 'low', 'assignee_user_id': self.admin.id, 'expected_status': 'triaged'})
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(self.mocks[2].call_count, 2)
        stale = self.call(self.admin, 'patch', path, json={'priority': 'urgent', 'expected_status': 'new'})
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(db.session.get(FeedbackTicket, tid).priority, 'low')

    def test_storage_failure_rolls_back_both_attachments_and_ticket(self):
        attempts = []
        def save(key, stream, **kwargs):
            attempts.append(key)
            self.put(key, stream)
            if len(attempts) == 2:
                raise OSError('isolated storage failure')
        with patch('storage.storage.put_object', side_effect=save):
            response = self.call(self.reporter, 'post', '/feedback-tickets', data={
                'title': '存储失败', 'image': (io.BytesIO(b'upload'), 'first.png', 'image/png'),
                'snapshot': (io.BytesIO(b'snapshot'), 'page.jpg', 'image/jpeg')})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(FeedbackTicket.query.count(), 0)
        self.assertEqual(FeedbackTicketAttachment.query.count(), 0)
        self.assertFalse(self.objects)

    def test_progress_waiting_reply_resolution_and_reopen(self):
        tid = self.create()
        self.assertEqual(self.transition(tid, 'closed').status_code, 400)
        self.call(self.admin, 'patch', f'/admin/feedback-tickets/{tid}/triage', json={})
        self.assertEqual(self.transition(tid, 'in_progress', expected_status='triaged').status_code, 200)
        self.assertEqual(self.transition(tid, 'waiting_user').status_code, 400)
        self.assertEqual(self.transition(tid, 'waiting_user', reason='请补充点击步骤').status_code, 200)
        self.call(self.admin, 'post', f'/admin/feedback-tickets/{tid}/messages', json={'body': '内部定位', 'visibility': 'internal'})
        detail = self.call(self.reporter, 'get', f'/feedback-tickets/{tid}').json['ticket']
        self.assertEqual([m['body'] for m in detail['messages']], ['请补充点击步骤'])
        self.call(self.reporter, 'post', f'/feedback-tickets/{tid}/messages', json={'body': '点击右上角保存'})
        self.assertEqual(db.session.get(FeedbackTicket, tid).status, 'in_progress')
        self.assertEqual(self.transition(tid, 'resolved', resolution_code='invented').status_code, 400)
        self.assertEqual(self.transition(tid, 'resolved', resolution_code='fixed', resolution_summary='已修复').status_code, 200)
        self.assertEqual(self.transition(tid, 'closed').status_code, 200)
        result = self.call(self.reporter, 'post', f'/feedback-tickets/{tid}/reopen', json={})
        self.assertEqual(result.status_code, 200)
        ticket = db.session.get(FeedbackTicket, tid)
        self.assertIsNone(ticket.resolution_summary)
        self.assertIsNone(ticket.closed_at)
        self.assertIsNone(ticket.resolved_at)
        event = FeedbackTicketEvent.query.filter_by(event_type='reopened').one()
        self.assertEqual(event.from_status, 'closed')

    def test_queues_staff_permissions_and_server_actions(self):
        tid = self.create()
        self.assertEqual(self.call(self.other, 'get', '/admin/feedback-tickets').status_code, 403)
        for endpoint in ('stats', 'staff'):
            self.assertEqual(self.call(self.other, 'get', '/admin/feedback-tickets/' + endpoint).status_code, 403)
        self.assertEqual(self.call(self.admin, 'get', '/admin/feedback-tickets?queue=inbox').json['total'], 1)
        self.assertEqual(self.call(self.admin, 'get', '/admin/feedback-tickets?queue=done').json['total'], 0)
        self.assertEqual(self.call(self.admin, 'get', '/admin/feedback-tickets?assignee=mine').json['total'], 0)
        self.assertEqual(self.call(self.admin, 'get', '/admin/feedback-tickets/stats').json['data']['by_status']['new'], 1)
        self.assertEqual(self.call(self.admin, 'get', '/admin/feedback-tickets/staff').json['staff'], [{'id': self.admin.id, 'username': 'admin'}])
        detail = self.call(self.admin, 'get', f'/admin/feedback-tickets/{tid}').json['ticket']
        self.assertEqual(detail['allowed_transitions'], ['rejected'])
        self.assertEqual(self.transition(tid, 'rejected', reason='重复工单').status_code, 200)
        detail = self.call(self.admin, 'get', f'/admin/feedback-tickets/{tid}').json['ticket']
        self.assertEqual(detail['allowed_transitions'], ['reopened'])
        self.assertFalse(detail['can_triage'])
        self.assertEqual(self.transition(tid, 'reopened').status_code, 200)
        self.assertIsNone(db.session.get(FeedbackTicket, tid).resolution_summary)


if __name__ == '__main__':
    unittest.main()

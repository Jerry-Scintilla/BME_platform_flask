"""管理端准入与工作台摘要的隔离回归；只使用内存 SQLite。"""
import inspect
import unittest
from datetime import date, timedelta
from unittest.mock import patch

from flask import Flask
from flask_jwt_extended import JWTManager, create_access_token
from sqlalchemy import text
from sqlalchemy import event
from sqlalchemy.dialects.mysql import LONGTEXT, MEDIUMTEXT
from sqlalchemy.ext.compiler import compiles

from exts import db
from models import (
    CampJoinRequest, CampLeave, CampMember, CampMilestone,
    CampSession, CampSubmissionVersion, CampUnit, PermissionModel,
    ProjectApplicationVersion, UserModel, UserPermissionModel,
)
from blueprints import admin as admin_module, auth as auth_module
from services.request_guard import enforce_request_access


@compiles(MEDIUMTEXT, 'sqlite')
@compiles(LONGTEXT, 'sqlite')
def _mysql_text_on_sqlite(_element, _compiler, **_kwargs):
    return 'TEXT'


class AdminWorkbenchTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
            JWT_SECRET_KEY='test-secret-key-with-at-least-32-characters',
        )
        db.init_app(self.app)
        JWTManager(self.app)
        self.app.before_request(enforce_request_access)
        self.app.add_url_rule('/admin/probe', 'admin_probe', lambda: {'ok': True})
        self.app.add_url_rule('/user/probe', 'user_probe', lambda: {'ok': True})
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()

        self.admin = UserModel(username='管理员', email='admin@example.com', role='super_admin')
        self.admin.set_password('a' * 32)
        self.operator = UserModel(username='普通用户', email='user@example.com', role='user')
        self.operator.set_password('a' * 32)
        db.session.add_all([self.admin, self.operator])
        db.session.flush()
        permission = PermissionModel(name='system_management', description='系统管理')
        db.session.add(permission)
        db.session.flush()
        db.session.add(UserPermissionModel(user_id=self.operator.id, permission_id=permission.id))
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def test_business_permission_does_not_allow_admin_login(self):
        with self.app.test_request_context('/auth/admin_login', method='POST', json={
            'User_Email': 'user@example.com', 'User_Password': 'a' * 32,
        }):
            response, status = inspect.unwrap(auth_module.admin_login)()
        self.assertEqual(status, 403)
        self.assertEqual(response.get_json()['message'], '无管理端访问权限')
        with self.app.test_request_context('/auth/admin_login', method='POST', json={
            'User_Email': 'user@example.com', 'User_Password': 'b' * 32,
        }):
            _, wrong_password_status = inspect.unwrap(auth_module.admin_login)()
        self.assertEqual(wrong_password_status, 402)

    def test_super_admin_can_still_log_in(self):
        with self.app.test_request_context('/auth/admin_login', method='POST', json={
            'User_Email': 'admin@example.com', 'User_Password': 'a' * 32,
        }):
            response, status = inspect.unwrap(auth_module.admin_login)()
        self.assertEqual(status, 200)
        self.assertEqual(response.get_json()['role'], 'super_admin')
        self.assertTrue(response.get_json()['token'])

    def test_admin_api_requires_personal_super_admin_token(self):
        client = self.app.test_client()
        ordinary_token = create_access_token(identity=self.operator.email)
        admin_token = create_access_token(identity=self.admin.email)
        self.assertEqual(client.get('/admin/probe').status_code, 401)
        self.assertEqual(client.get('/admin/probe', headers={
            'Authorization': f'Bearer {ordinary_token}',
        }).status_code, 403)
        self.assertEqual(client.get('/admin/probe', headers={
            'Authorization': f'Bearer {admin_token}',
        }).status_code, 200)
        self.assertEqual(client.get('/user/probe', headers={
            'Authorization': f'Bearer {ordinary_token}',
        }).status_code, 200)

    def test_summary_groups_real_camp_counts(self):
        learning = CampSession(name='培训营', category='learning', status='running',
                               start_date=date.today() - timedelta(days=1),
                               end_date=date.today() + timedelta(days=10))
        project = CampSession(name='项目营', category='project', status='running',
                              start_date=date.today() - timedelta(days=1),
                              end_date=date.today() + timedelta(days=10))
        db.session.add_all([learning, project])
        db.session.flush()
        db.session.add_all([
            CampMember(camp_session_id=learning.id, user_id=self.operator.id, role='student'),
            CampJoinRequest(camp_session_id=learning.id, user_id=self.operator.id, status='pending'),
            CampLeave(camp_session_id=learning.id, user_id=self.operator.id, status='pending',
                      start_date=date.today(), end_date=date.today()),
            ProjectApplicationVersion(camp_session_id=project.id, submitted_by=self.operator.id,
                                      leader_user_id=self.operator.id, name='申报', status='pending'),
        ])
        unit = CampUnit(camp_session_id=project.id, unit_type='project', name='项目组',
                        owner_user_id=self.operator.id)
        db.session.add(unit)
        db.session.flush()
        milestone = CampMilestone(camp_session_id=project.id, unit_id=unit.id,
                                  title='节点', submit_mode='team')
        db.session.add(milestone)
        db.session.flush()
        db.session.add(CampSubmissionVersion(milestone_id=milestone.id, version=1,
                                             submitted_by=self.operator.id, status='submitted'))
        db.session.commit()

        with self.app.test_request_context('/admin/workbench/summary'):
            data = inspect.unwrap(admin_module.workbench_summary)().get_json()['data']

        self.assertEqual(data['pending']['camp_join'], 1)
        self.assertEqual(data['pending']['camp_leave'], 1)
        self.assertEqual(data['pending']['project_application'], 1)
        self.assertEqual(data['pending']['project_delivery'], 1)
        self.assertEqual(data['pending_by_camp']['camp_join'][str(learning.id)], 1)
        self.assertEqual(data['pending_by_camp']['project_delivery'][str(project.id)], 1)
        camps = {camp['id']: camp for camp in data['running_camps']}
        self.assertEqual(camps[learning.id]['member_count'], 1)
        self.assertEqual(camps[learning.id]['pending_join'], 1)
        self.assertEqual(camps[project.id]['pending_project_application'], 1)
        self.assertEqual(camps[project.id]['pending_project_delivery'], 1)
        self.assertEqual(data['section_status']['running_camps'], 'ok')
        self.assertIn(learning.id, [risk['camp_id'] for risk in data['risks']
                                    if risk['rule'] == 'owner_missing'])

    def test_missing_source_is_unknown_instead_of_zero(self):
        db.session.execute(text('DROP TABLE camp_join_request'))
        db.session.commit()
        with patch.object(self.app.logger, 'exception') as log_error:
            with self.app.test_request_context('/admin/workbench/summary'):
                data = inspect.unwrap(admin_module.workbench_summary)().get_json()['data']
        self.assertIsNone(data['pending']['camp_join'])
        self.assertEqual(data['source_status']['camp_join'], 'unavailable')
        self.assertTrue(log_error.called)

    def test_running_camp_query_count_does_not_grow_per_camp(self):
        def query_count():
            statements = []
            def capture(_conn, _cursor, statement, _params, _context, _executemany):
                if statement.lstrip().upper().startswith('SELECT'):
                    statements.append(statement)
            event.listen(db.engine, 'before_cursor_execute', capture)
            try:
                with self.app.test_request_context('/admin/workbench/summary'):
                    inspect.unwrap(admin_module.workbench_summary)()
            finally:
                event.remove(db.engine, 'before_cursor_execute', capture)
            return len(statements)

        db.session.add(CampSession(name='首营', category='learning', status='running',
                                   start_date=date.today(), end_date=date.today()))
        db.session.commit()
        baseline = query_count()
        db.session.add_all([
            CampSession(name=f'营期 {i}', category='learning', status='running',
                        start_date=date.today(), end_date=date.today())
            for i in range(8)
        ])
        db.session.commit()
        self.assertEqual(query_count(), baseline)


if __name__ == '__main__':
    unittest.main()

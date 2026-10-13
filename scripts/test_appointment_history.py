"""Isolated appointment history regression. Never imports the production app."""
import os
import sys
import unittest
from datetime import date, datetime, timedelta
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from flask import Flask
from flask_jwt_extended import JWTManager, create_access_token
from sqlalchemy.dialects.mysql import LONGTEXT, MEDIUMTEXT
from sqlalchemy.ext.compiler import compiles
from exts import db
from models import UserModel, PersonModel, ClubPosition, ClubGroup, ClubOfficer, ClubOrgEvent, WorkAccessGrant
from blueprints.officers import bp
from blueprints.club_admin import bp as club_bp
from blueprints.workbench_sso import directory_snapshot


@compiles(MEDIUMTEXT, 'sqlite')
@compiles(LONGTEXT, 'sqlite')
def mysql_text(_element, _compiler, **_kwargs):
    return 'TEXT'


class AppointmentHistoryTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
            JWT_SECRET_KEY='appointment-isolated-secret-with-more-than-32-chars')
        db.init_app(self.app)
        JWTManager(self.app)
        self.app.register_blueprint(bp)
        self.app.register_blueprint(club_bp)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = self.app.test_client()
        person = PersonModel(public_id='history-person', verified_name='测试人员', verification_status='verified')
        db.session.add(person)
        db.session.flush()
        self.user = UserModel(username='成员', email='member@history.test', password='unused', person_id=person.id, role='user')
        self.admin = UserModel(username='管理者', email='admin@history.test', password='unused', role='super_admin')
        self.group = ClubGroup(name='旧组')
        self.new_group = ClubGroup(name='新组')
        self.position = ClubPosition(name='组长', org_slot='group', sort_rank=10, per_group_limit=1)
        db.session.add_all([self.user, self.admin, self.group, self.new_group, self.position])
        db.session.commit()
        self.headers = {'Authorization': 'Bearer ' + create_access_token(identity=self.admin.email)}

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def appoint(self, **extra):
        response = self.client.post('/admin/officers', headers=self.headers, json={
            'user_id': self.user.id, 'title_id': self.position.id, 'group_id': self.group.id,
            'term_start': '2025-03-01', **extra})
        self.assertEqual(response.status_code, 200, response.json)
        return db.session.get(ClubOfficer, response.json['data']['id'])

    def backfill(self, **extra):
        return self.client.post('/admin/officers/history', headers=self.headers, json={
            'user_id': self.user.id, 'title': '历任副社长', 'source_note': '2025届任命名单，经负责人核对',
            'historical_year': 2025, **extra})

    def test_appointment_records_person_and_operation_time(self):
        row = self.appoint()
        self.assertEqual(row.person_id, self.user.person_id)
        self.assertEqual(row.org_slot_snapshot, 'group')
        self.assertIsNotNone(row.appointed_at)
        self.assertNotEqual(row.appointed_at.date(), row.term_start)
        self.assertEqual(ClubOrgEvent.query.filter_by(kind='officer_appointed').count(), 1)

    def test_transition_preserves_old_term_and_dates(self):
        row = self.appoint()
        response = self.client.put(f'/admin/officers/{row.id}', headers=self.headers, json={
            'mode': 'transition', 'reason': '调组', 'group_id': self.new_group.id, 'term_start': '2026-01-01'})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertNotEqual(row.id, response.json['data']['id'])
        db.session.refresh(row)
        self.assertEqual((row.status, row.department, row.term_start, row.term_end),
                         ('ended', '旧组', date(2025, 3, 1), date(2025, 12, 31)))
        self.assertIsNotNone(row.ended_at)
        self.assertEqual(ClubOrgEvent.query.filter_by(kind='officer_ended').count(), 1)
        self.assertEqual(ClubOrgEvent.query.filter_by(kind='officer_appointed').count(), 2)

    def test_correction_requires_reason_and_preserves_diff(self):
        row = self.appoint()
        path = f'/admin/officers/{row.id}'
        self.assertEqual(self.client.put(path, headers=self.headers, json={'term_start': '2025-02-01'}).status_code, 400)
        response = self.client.put(path, headers=self.headers, json={
            'mode': 'correction', 'reason': '任命名单日期录入错误', 'term_start': '2025-02-01'})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(ClubOfficer.query.count(), 1)
        event = ClubOrgEvent.query.filter_by(kind='officer_edited').one()
        import json
        self.assertEqual(json.loads(event.detail_json)['changes']['term_start']['from'], '2025-03-01')

    def test_batch_appointment_creates_event_per_success(self):
        response = self.client.post('/admin/officers/batch', headers=self.headers, json={'items': [
            {'user_id': self.user.id, 'title_id': self.position.id, 'group_id': self.group.id},
            {'user_id': 99999, 'title_id': self.position.id}]})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json['data']['added'], 1)
        self.assertEqual(ClubOrgEvent.query.filter_by(kind='officer_appointed').count(), 1)
        self.assertIsNotNone(ClubOfficer.query.one().appointed_at)

    def test_backfill_unknown_dates_has_no_current_grants(self):
        response = self.backfill()
        self.assertEqual(response.status_code, 200, response.json)
        row = db.session.get(ClubOfficer, response.json['data']['id'])
        self.assertEqual((row.status, row.record_source), ('ended', 'backfill'))
        self.assertIsNone(row.term_start)
        self.assertIsNone(row.term_end)
        self.assertIsNone(row.appointed_at)
        self.assertIsNone(row.ended_at)
        self.assertEqual(WorkAccessGrant.query.count(), 0)
        history = self.client.get(f'/admin/officers/history/{self.user.id}?year=2025', headers=self.headers).json['data']
        self.assertEqual(history['records'][0]['date_precision'], 'year')
        self.assertEqual(history['records'][0]['years'], [2025])

    def test_backfill_rejects_future_and_reversed_dates_and_missing_source(self):
        for fields in ({'term_start': '2099-01-01'}, {'source_note': ''},
                       {'term_start': '2025-12-01', 'term_end': '2025-01-01'}, {'historical_year': True}):
            self.assertEqual(self.backfill(**fields).status_code, 400)
        self.assertEqual(ClubOfficer.query.count(), 0)

    def test_cross_year_history_includes_same_term_each_year(self):
        self.appoint()
        for year in (2025, 2026):
            response = self.client.get(f'/admin/officers/history/{self.user.id}?year={year}', headers=self.headers)
            self.assertEqual(len(response.json['data']['records']), 1)
        response = self.client.get(f'/admin/officers/history/{self.user.id}?year=2027', headers=self.headers)
        self.assertEqual(response.json['data']['records'], [])

    def test_history_unifies_accounts_and_merged_person(self):
        old = PersonModel(public_id='former-person', verification_status='verified', record_status='merged', merged_to_person_id=self.user.person_id)
        db.session.add(old)
        db.session.flush()
        other = UserModel(username='旧账号', email='old@history.test', password='unused', person_id=old.id)
        db.session.add(other)
        db.session.flush()
        db.session.add(ClubOfficer(user_id=other.id, person_id=old.id, title='旧职位', status='ended', term_start=date(2025, 1, 1), term_end=date(2025, 2, 1)))
        db.session.commit()
        response = self.client.get(f'/admin/officers/history/{self.user.id}', headers=self.headers)
        self.assertEqual(len(response.json['data']['records']), 1)
        snapshot = directory_snapshot()
        self.assertEqual(snapshot['officers'][0]['person_id'], self.user.person_id)
        self.assertNotIn('password', snapshot['users'][0])

    def test_ended_record_correction_does_not_reactivate(self):
        row = self.appoint()
        response = self.client.post(f'/admin/officers/{row.id}/end', headers=self.headers, json={'term_end': '2025-12-31', 'end_reason': '换届'})
        self.assertEqual(response.status_code, 200, response.json)
        ended_at = row.ended_at
        response = self.client.put(f'/admin/officers/{row.id}', headers=self.headers, json={
            'mode': 'correction', 'reason': '日期纠错', 'term_end': '2025-12-30'})
        self.assertEqual(response.status_code, 200, response.json)
        db.session.refresh(row)
        self.assertEqual(row.status, 'ended')
        self.assertEqual(row.ended_at, ended_at)

    def test_renewal_creates_new_term(self):
        old = self.appoint()
        response = self.client.post(f'/admin/club/groups/{self.group.id}/leader', headers=self.headers,
            json={'user_id': self.user.id, 'position_id': self.position.id, 'term_start': '2026-01-01', 'sync_primary': False})
        self.assertEqual(response.status_code, 200, response.json)
        db.session.refresh(old)
        self.assertEqual(old.term_end, date(2025, 12, 31))
        self.assertIsNotNone(old.ended_at)
        self.assertEqual(ClubOfficer.query.count(), 2)

    def test_permission_and_invalid_year(self):
        headers = {'Authorization': 'Bearer ' + create_access_token(identity=self.user.email)}
        self.assertEqual(self.client.get(f'/admin/officers/history/{self.user.id}', headers=headers).status_code, 403)
        self.assertEqual(self.client.get(f'/admin/officers/history/{self.user.id}?year=oops', headers=self.headers).status_code, 400)

    def test_historical_correction_updates_source_and_unknown_year(self):
        response = self.backfill()
        row_id = response.json['data']['id']
        response = self.client.put(f'/admin/officers/{row_id}', headers=self.headers, json={
            'mode': 'correction', 'reason': '核对原始名单', 'title': '历任社长',
            'historical_year': 2024, 'source_note': '2024届原始名单'})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json['data']['org_slot'], 'club')
        row = db.session.get(ClubOfficer, row_id)
        self.assertEqual((row.title, row.historical_year, row.source_note), ('历任社长', 2024, '2024届原始名单'))
        self.assertEqual(row.status, 'ended')
        self.assertEqual(WorkAccessGrant.query.count(), 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)

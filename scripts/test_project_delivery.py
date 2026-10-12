"""项目营交付定向回归：内存SQLite及模拟对象存储，不连接业务库。"""
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy.dialects.mysql import LONGTEXT, MEDIUMTEXT
from sqlalchemy.ext.compiler import compiles
from test_camp_group_adapter import CampGroupAdapterSmoke

@compiles(LONGTEXT, 'sqlite')
@compiles(MEDIUMTEXT, 'sqlite')
def sqlite_text(_type, _compiler, **_kw):
    return 'TEXT'

from exts import db
import models as m
from blueprints import camp_delivery as delivery
from services.camp_groups import projects
from services.camp_groups.delivery import progress, pending_reviews
from flask_jwt_extended import JWTManager, create_access_token


class ProjectDeliveryTest(unittest.TestCase):
    make_user = CampGroupAdapterSmoke.make_user
    tearDown = CampGroupAdapterSmoke.tearDown
    unit = CampGroupAdapterSmoke.unit
    join_camp = CampGroupAdapterSmoke.join_camp

    def setUp(self):
        CampGroupAdapterSmoke.setUp(self)
        db.session.commit()
        db.create_all()
        self.project = self.unit()
        self.camp.status = 'running'
        self.node = m.CampMilestone(camp_session_id=self.camp.id, unit_id=self.project.id,
                                   title='研发节点', submit_mode='team')
        db.session.add(self.node)
        db.session.commit()
        self.app.config.update(TESTING=True, JWT_SECRET_KEY='isolated-project-delivery-secret-32-chars')
        JWTManager(self.app)
        from blueprints.camp import bp as camp_bp
        self.app.register_blueprint(camp_bp)
        self.app.register_blueprint(delivery.bp)
        self.client = self.app.test_client()

    def call(self, user, method, path, **kwargs):
        token = create_access_token(identity=user.email)
        with patch('services.auth_context.current_actor', return_value=SimpleNamespace(user=user)):
            return getattr(self.client, method)(path, headers={'Authorization': 'Bearer '+token}, **kwargs)

    def submit(self, user=None, **kwargs):
        return self.call(user or self.owner, 'post', f'/camp/milestones/{self.node.id}/submissions',
                         data=kwargs or {'content': '本次材料'})

    def review(self, sub_id, action='approve', **kwargs):
        return self.call(self.actor, 'post', f'/camp/submissions/{sub_id}/review',
                         json={'action': action, **kwargs})

    def add_student(self, user=None):
        user = user or self.student
        self.join_camp(user)
        projects.add_member(self.project, user.id, 'member', 'admin', self.actor.id)
        db.session.commit()

    def evaluate(self, user=None, score=90):
        user = user or self.student
        return self.call(self.owner, 'put', f'/camp/milestones/{self.node.id}/evaluations/{user.id}',
                         json={'score': score, 'comment': '已完成'})

    def test_solo_submit_return_resubmit_review_and_archive(self):
        first = self.submit()
        self.assertEqual(first.status_code, 200, first.json)
        first_id = first.json['submission']['id']
        self.assertEqual(pending_reviews().count(), 1)
        self.assertEqual(self.review(first_id, 'return', note='补充验证').status_code, 200)
        second = self.submit(content='补充完成')
        second_id = second.json['submission']['id']
        self.assertEqual(second.json['submission']['version'], 2)
        self.assertEqual(self.review(first_id).status_code, 400)
        self.assertEqual(self.review(second_id).status_code, 200)
        self.assertTrue(progress(self.node, self.project)['node_complete'])
        self.assertEqual(self.submit().status_code, 400)
        self.camp.status = 'archived'
        archive = delivery.freeze_camp_archive(self.camp, self.actor.id)
        frozen = json.loads(archive.snapshot)['units'][0]['milestones'][0]
        self.assertEqual(frozen['status'], 'approved')
        self.assertEqual(frozen['completion_mode'], 'leader_review')
        self.assertEqual(frozen['final_versions'][0]['version'], 2)
        self.assertEqual(pending_reviews().count(), 0)

    def test_multimember_evaluation_is_canonical_and_private_in_archive(self):
        self.add_student()
        other = self.make_user('second_student')
        self.add_student(other)
        self.assertEqual(self.submit().status_code, 400)
        self.assertEqual(self.submit(self.student).status_code, 200)
        self.assertEqual(pending_reviews().count(), 0)
        self.assertEqual(self.evaluate(score=0).status_code, 200)
        self.assertFalse(progress(self.node, self.project)['node_complete'])
        self.assertEqual(self.evaluate(other).status_code, 200)
        self.assertEqual(self.node.status, 'approved')
        self.camp.status = 'archived'
        archive = delivery.freeze_camp_archive(self.camp, self.actor.id)
        snapshot = json.loads(archive.snapshot)
        frozen = snapshot['units'][0]['milestones'][0]
        self.assertTrue(frozen['node_complete'])
        self.assertEqual({e['score'] for e in frozen['evaluations']}, {0, 90})
        response = self.call(self.student, 'get', f'/camp/sessions/{self.camp.id}/archive')
        visible = response.json['archive']['snapshot']['units'][0]['milestones'][0]['evaluations']
        self.assertEqual([e['member_user_id'] for e in visible], [self.student.id])
        self.assertEqual(len(json.loads(archive.snapshot)['units'][0]['milestones'][0]['evaluations']), 2)

    def test_membership_changes_recompute_completion_on_read(self):
        self.add_student()
        self.evaluate()
        self.assertTrue(progress(self.node, self.project)['node_complete'])
        other = self.make_user('joining_student')
        self.add_student(other)
        response = self.call(self.owner, 'get', f'/camp/units/{self.project.id}/milestones')
        self.assertFalse(response.json['milestones'][0]['node_complete'])
        self.assertNotEqual(response.json['milestones'][0]['status'], 'approved')

    def test_archived_outcome_cannot_be_verified_or_rejected(self):
        outcome = m.CampOutcome(unit_id=self.project.id, title='成果', submitted_by=self.owner.id)
        db.session.add(outcome)
        self.camp.status = 'archived'
        archive = delivery.freeze_camp_archive(self.camp, self.actor.id)
        before = archive.snapshot
        for action in ['verify', 'reject']:
            response = self.call(self.actor, 'post', f'/camp/outcomes/{outcome.id}/verify',
                                 json={'action': action, 'reason': '测试'})
            self.assertEqual(response.status_code, 400)
        self.assertEqual(outcome.status, 'submitted')
        self.assertEqual(archive.snapshot, before)

    def test_review_permissions_latest_version_pause_and_archive(self):
        sub = self.submit().json['submission']['id']
        self.assertEqual(self.call(self.owner, 'post', f'/camp/submissions/{sub}/review',
                                   json={'action': 'approve'}).status_code, 403)
        self.assertEqual(self.call(self.student, 'post', f'/camp/submissions/{sub}/review',
                                   json={'action': 'approve'}).status_code, 403)
        self.project.status = 'paused'; db.session.commit()
        self.assertEqual(self.review(sub).status_code, 400)
        self.project.status = 'active'; self.camp.status = 'archived'; db.session.commit()
        self.assertEqual(self.review(sub).status_code, 400)
        self.assertEqual(self.submit().status_code, 400)

    def test_solo_can_submit_and_other_camp_member_cannot(self):
        response = self.call(self.owner, 'get', f'/camp/units/{self.project.id}/milestones')
        self.assertTrue(response.json['can_submit'])
        self.assertEqual(response.json['milestones'][0]['completion_mode'], 'leader_review')
        self.join_camp(self.student); db.session.commit()
        response = self.call(self.student, 'get', f'/camp/units/{self.project.id}/milestones')
        self.assertFalse(response.json['can_submit'])
        self.assertEqual(self.submit(self.student).status_code, 403)

    def test_multi_upload_total_limit_is_checked_before_storage(self):
        with patch.object(delivery, 'MAX_FILE_MB', 1), patch.object(delivery.storage, 'put_object') as put:
            response = self.submit(Files=[(io.BytesIO(b'a'*600000), 'a.txt'),
                                          (io.BytesIO(b'b'*600000), 'b.txt')])
            self.assertEqual(response.status_code, 413)
            put.assert_not_called()
        self.assertEqual(m.CampSubmissionVersion.query.count(), 0)

    def test_upload_boundary_and_signed_download(self):
        with patch.object(delivery, 'MAX_FILE_MB', 1), patch.object(delivery.storage, 'put_object'):
            response = self.submit(Files=[(io.BytesIO(b'a'*1048576), 'a.txt')])
        self.assertEqual(response.status_code, 200, response.json)
        aid = m.CampSubmissionAttachment.query.one().id
        self.assertEqual(self.client.get(f'/camp/submissions/attachments/{aid}').status_code, 401)
        self.assertEqual(self.call(self.student, 'get', f'/camp/submissions/attachments/{aid}/token').status_code, 403)
        signed = self.call(self.actor, 'get', f'/camp/submissions/attachments/{aid}/token').json['url']
        with patch.object(delivery.storage, 'get_object', return_value=b'file'):
            self.assertEqual(self.client.get(signed).status_code, 200)

    def test_upload_failure_cleans_objects_and_preserves_previous_version(self):
        first = self.submit().json['submission']['id']
        with patch.object(delivery.storage, 'put_object', side_effect=[None, OSError('storage down')]), \
             patch.object(delivery.storage, 'remove_object') as remove:
            response = self.submit(Files=[(io.BytesIO(b'a'), 'a.txt'), (io.BytesIO(b'b'), 'b.txt')])
            self.assertEqual(response.status_code, 500)
            self.assertEqual(remove.call_count, 2)
        self.assertEqual(m.CampSubmissionVersion.query.count(), 1)
        self.assertEqual(db.session.get(m.CampSubmissionVersion, first).status, 'submitted')

    def test_close_failure_rolls_back_status_and_notification(self):
        count = m.NotificationModel.query.count()
        with patch.object(delivery, 'freeze_camp_archive', side_effect=RuntimeError('freeze failed')):
            response = self.call(self.actor, 'post', f'/camp/sessions/{self.camp.id}/transitions', json={'action': 'close'})
        self.assertEqual(response.status_code, 500)
        self.assertEqual(db.session.get(m.CampSession, self.camp.id).status, 'running')
        self.assertEqual(m.CampArchive.query.count(), 0)
        self.assertEqual(m.NotificationModel.query.count(), count)

    def test_close_success_and_revision_keep_snapshot_immutable(self):
        sub = self.submit().json['submission']['id']; self.review(sub)
        response = self.call(self.actor, 'post', f'/camp/sessions/{self.camp.id}/transitions', json={'action': 'close'})
        self.assertEqual(response.status_code, 200, response.json)
        archive = m.CampArchive.query.one(); before = archive.snapshot
        response = self.call(self.actor, 'post', f'/camp/sessions/{self.camp.id}/archive/revisions',
                             json={'expected_version': 1, 'reason': '补充备注'})
        self.assertEqual(response.status_code, 200)
        duplicate = self.call(self.actor, 'post', f'/camp/sessions/{self.camp.id}/archive/revisions',
                              json={'expected_version': 1, 'reason': '旧版本'})
        self.assertEqual(duplicate.status_code, 409)
        response = self.call(self.actor, 'get', f'/camp/sessions/{self.camp.id}/archive')
        self.assertEqual(len(response.json['revisions']), 1)
        self.assertEqual(archive.snapshot, before)

    def test_notifications_do_not_depend_on_audit_commit(self):
        with patch('blueprints._audit_write'):
            response = self.submit()
            self.assertEqual(response.status_code, 200)
            sub_id = response.json['submission']['id']
            self.assertEqual(self.review(sub_id).status_code, 200)
        db.session.remove()
        self.assertEqual(m.NotificationModel.query.count(), 2)
        self.assertEqual(db.session.get(m.CampSubmissionVersion, sub_id).status, 'approved')

    def test_scored_materials_become_assets_and_evaluated_nodes_cannot_be_deleted(self):
        self.add_student()
        with patch.object(delivery.storage, 'put_object'):
            self.submit(self.student, Files=[(io.BytesIO(b'proof'), 'proof.txt')])
        self.evaluate()
        response = self.call(self.owner, 'delete', f'/camp/milestones/{self.node.id}')
        self.assertEqual(response.status_code, 400)
        self.camp.status = 'archived'
        archive = delivery.freeze_camp_archive(self.camp, self.actor.id)
        self.assertTrue(m.CampSubmissionAttachment.query.one().is_asset)
        self.assertEqual(json.loads(archive.snapshot)['units'][0]['milestones'][0]['final_versions'][0]['version'], 1)

    def test_paused_project_cannot_register_outcome(self):
        self.project.status = 'paused'; db.session.commit()
        response = self.call(self.owner, 'post', f'/camp/units/{self.project.id}/outcomes', json={'title': '成果'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(m.CampOutcome.query.count(), 0)


if __name__ == '__main__':
    unittest.main()

"""小组核心定向检查：仅 SQLite 内存中的新表及必要人员表，不导入 app。

运行：.venv/bin/python scripts/test_group_core.py
此文件不覆盖营期接入、MySQL 并发、迁移或全站回归。
"""
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from flask import Flask
from sqlalchemy import event

import config
from exts import db
from models import (UserModel, PersonModel, PersonPrimaryAccountModel, IdentityEventModel,
                    IdentityExceptionGrantModel, CampGroup, GroupMembership,
                    GroupRoleAssignment, GroupMembershipEvent, GroupOperation)
from group_models import GROUP_CORE_TABLES
from services.identity.person import create_provisional
from services.groups import (GroupService, GroupError, GovernancePolicy,
                             GovernanceContext, GovernanceDecision)


class FixturePolicy(GovernancePolicy):
    """仅在此隔离检查中装配；模拟营期层查询到的真实绑定与有效参与。"""
    def __init__(self, kind):
        self.kind = kind
        self.bound_groups = set()
        self.denied_users = set()

    def authorize(self, *, context, actor, group, action, payload):
        if context.kind != self.kind or context.scope_id != 10:
            return None
        if group is not None and group.id not in self.bound_groups:
            return None
        if actor.role != 'super_admin':
            return None
        return GovernanceDecision('fixture:teacher', 'camp_join', 10)

    def participant_allowed(self, *, context, group, user):
        return user.id not in self.denied_users

    def role_allowed(self, *, context, group, user, role, scope_type, scope_id):
        return scope_type == 'group' or scope_id == 12


class GroupCoreSmoke(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        db.init_app(self.app)
        self.ctx = self.app.app_context()
        self.ctx.push()

        @event.listens_for(db.engine, 'connect')
        def connected(conn, _):
            conn.isolation_level = None
            conn.execute('PRAGMA foreign_keys=ON')

        @event.listens_for(db.engine, 'begin')
        def begin(conn):
            conn.exec_driver_sql('BEGIN')

        tables = [UserModel.__table__, PersonModel.__table__, PersonPrimaryAccountModel.__table__,
                  IdentityEventModel.__table__, IdentityExceptionGrantModel.__table__]
        db.metadata.create_all(db.engine, tables=tables + list(GROUP_CORE_TABLES))
        self.settings = patch.multiple(config, IDENTITY_ENFORCEMENT_MODE='shadow',
                                       IDENTITY_VERIFY_GATE_DEFAULT='shadow',
                                       IDENTITY_VERIFY_GATES_RAW='')
        self.settings.start()
        self.actor = self.make_user('teacher', admin=True)
        self.owner = self.make_user('owner')
        self.student = self.make_user('student')
        db.session.commit()
        self.policy = FixturePolicy('training')
        self.project_policy = FixturePolicy('project')
        self.service = GroupService({'training': self.policy, 'project': self.project_policy})
        self.context = GovernanceContext('training', 10)
        self.sequence = 0

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.settings.stop()
        self.ctx.pop()

    def make_user(self, name, admin=False):
        user = UserModel(username=name, password='fixture-only', email=name + '@fixture.invalid',
                         role='super_admin' if admin else 'user')
        db.session.add(user)
        db.session.flush()
        create_provisional(user)
        return user

    def args(self, group=None, **overrides):
        self.sequence += 1
        args = dict(context=self.context, actor_user_id=self.actor.id,
                    idempotency_key=str(self.sequence), reason='定向检查')
        if group:
            args.update(group_id=group['id'], expected_version=group['version'])
        args.update(overrides)
        return args

    def create(self, context=None):
        context = context or self.context
        args = self.args(context=context)
        result = self.service.create_group(name='教学小组', owner_user_id=self.owner.id, **args)
        policy = self.policy if context.kind == 'training' else self.project_policy
        policy.bound_groups.add(result['group']['id'])
        return result, args

    def roles(self, group, user_id):
        return self.service.effective_roles(context=self.context, actor_user_id=self.actor.id,
                                            group_id=group['id'], user_id=user_id)

    def test_membership_roles_and_owner_lifecycle(self):
        result, _ = self.create()
        group = self.service.add_member(user_id=self.student.id, **self.args(result['group']))['group']
        for role in ('learner', 'mentor'):
            group = self.service.grant_role(user_id=self.student.id, role=role,
                                             **self.args(group))['group']
        self.assertEqual({r['role'] for r in self.roles(group, self.student.id)}, {'learner', 'mentor'})
        with self.assertRaisesRegex(GroupError, '转交'):
            self.service.end_membership(user_id=self.owner.id, **self.args(group))
        group = self.service.end_membership(user_id=self.student.id, **self.args(group))['group']
        group = self.service.add_member(user_id=self.student.id, **self.args(group))['group']
        self.assertEqual(self.roles(group, self.student.id), [])
        self.assertEqual(GroupMembership.query.filter_by(user_id=self.student.id).count(), 1)
        group = self.service.transfer_owner(user_id=self.student.id, **self.args(group))['group']
        self.service.end_membership(user_id=self.owner.id, **self.args(group))
        self.assertEqual(GroupRoleAssignment.query.filter_by(active_slot=1).count(), 0)

    def test_idempotency_version_and_outer_rollback(self):
        result, args = self.create()
        repeated = self.service.create_group(name='教学小组', owner_user_id=self.owner.id, **args)
        self.assertEqual(repeated, result)
        self.assertEqual(GroupMembershipEvent.query.count(), 1)
        with self.assertRaises(GroupError) as caught:
            self.service.create_group(name='不同内容', owner_user_id=self.owner.id, **args)
        self.assertEqual(caught.exception.machine, 'GROUP_IDEMPOTENCY_CONFLICT')
        current = self.service.rename_group(name='改名', **self.args(result['group']))['group']
        with self.assertRaises(GroupError) as caught:
            self.service.rename_group(name='旧版本', **self.args(result['group']))
        self.assertEqual(caught.exception.machine, 'GROUP_VERSION_CONFLICT')
        self.assertEqual(db.session.get(CampGroup, current['id']).name, '改名')
        db.session.rollback()
        self.assertEqual(CampGroup.query.count(), 0)
        self.assertEqual(GroupOperation.query.count(), 0)
        self.assertEqual(GroupMembershipEvent.query.count(), 0)

    def test_default_deny_binding_and_archive(self):
        with self.assertRaises(GroupError):
            GroupService().create_group(name='无治理', owner_user_id=self.owner.id, **self.args())
        result, _ = self.create()
        with self.assertRaises(GroupError):
            self.service.get_group(context=GovernanceContext('training', 99),
                                   actor_user_id=self.actor.id, group_id=result['group']['id'])
        group = self.service.archive_group(**self.args(result['group']))['group']
        with self.assertRaises(GroupError) as caught:
            self.service.add_member(user_id=self.student.id, **self.args(group))
        self.assertEqual(caught.exception.machine, 'GROUP_READ_ONLY')
        self.assertEqual(self.roles(group, self.owner.id), [])

    def test_person_uniqueness_and_appointment_gate(self):
        result, _ = self.create()
        group = self.service.add_member(user_id=self.student.id, **self.args(result['group']))['group']
        secondary = UserModel(username='secondary', password='fixture-only', email='second@fixture.invalid',
                              person_id=self.student.person_id)
        db.session.add(secondary)
        db.session.flush()
        with self.assertRaises(GroupError) as caught:
            self.service.add_member(user_id=secondary.id, **self.args(group))
        self.assertEqual(caught.exception.machine, 'GROUP_DUPLICATE_PERSON')
        with patch.multiple(config, IDENTITY_VERIFY_GATE_DEFAULT='enforce',
                            IDENTITY_VERIFICATION_ENABLED=True, IDENTITY_UI_ENABLED=True):
            with self.assertRaises(GroupError) as caught:
                self.service.grant_role(user_id=self.student.id, role='mentor', **self.args(group))
            self.assertEqual(caught.exception.machine, 'IDENTITY_VERIFICATION_REQUIRED')
            self.service.grant_role(user_id=self.student.id, role='learner', **self.args(group))

    def test_role_scope_expiry_and_participation_filter(self):
        result, _ = self.create()
        group = result['group']
        with self.assertRaises(GroupError):
            self.service.grant_role(user_id=self.owner.id, role='mentor', scope_type='course',
                                     scope_id=99, **self.args(group))
        group = self.service.grant_role(user_id=self.owner.id, role='mentor', scope_type='course',
                                         scope_id=12, expires_at=datetime.now() + timedelta(days=1),
                                         **self.args(group))['group']
        role = GroupRoleAssignment.query.one()
        role.started_at = datetime.now() - timedelta(days=2)
        role.expires_at = datetime.now() - timedelta(days=1)
        db.session.flush()
        self.assertEqual(self.roles(group, self.owner.id), [])
        group = self.service.grant_role(user_id=self.owner.id, role='mentor', scope_type='course',
                                         scope_id=12, **self.args(group))['group']
        self.assertEqual(len(self.roles(group, self.owner.id)), 1)
        self.assertEqual(GroupRoleAssignment.query.count(), 2)
        self.policy.denied_users.add(self.owner.id)
        self.assertEqual(self.roles(group, self.owner.id), [])

    def test_project_uses_same_service_with_separate_policy(self):
        context = GovernanceContext('project', 10)
        result, _ = self.create(context)
        group = self.service.grant_role(user_id=self.owner.id, role='project_leader',
                                         **self.args(result['group'], context=context))['group']
        self.assertEqual(group['version'], 2)
        with self.assertRaises(GroupError):
            self.service.grant_role(user_id=self.owner.id, role='mentor',
                                     **self.args(group, context=context))
        with self.assertRaises(GroupError):
            self.service.get_group(context=self.context, actor_user_id=self.actor.id, group_id=group['id'])


if __name__ == '__main__':
    unittest.main()

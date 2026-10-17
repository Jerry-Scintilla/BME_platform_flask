"""阶段 2 定向检查：SQLite 内存，仅项目适配/旧成员映射，不启动 app 或调度器。"""
import unittest
from datetime import date, datetime, timedelta

import test_group_core as foundation
from exts import db
from models import (CampSession, CampUnit, CampUnitMember, CampMember, CampMemberEvent,
                    CampMembershipEvent, CampPersonParticipationModel, UnitPersonParticipationModel,
                    LegacyUnitMembershipMap, GroupMembership, GroupMembershipEvent,
                    GroupRoleAssignment, PersonPrimaryAccountModel, UserModel)
from services.camp_groups import projects
from services.camp_groups.reads import UnitMembershipRead
from services.camp_groups.migration import preview_project, map_project
from services.groups import GroupError, GovernanceContext


class CampGroupAdapterSmoke(unittest.TestCase):
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
        for model in (CampSession, CampUnit, CampUnitMember, CampMember, CampMemberEvent,
                      CampMembershipEvent, CampPersonParticipationModel, UnitPersonParticipationModel,
                      LegacyUnitMembershipMap):
            include(model.__table__)
        from models import ProjectLegacyMap, ProjectCampParticipant
        include(ProjectLegacyMap.__table__); include(ProjectCampParticipant.__table__)
        db.metadata.create_all(db.engine, tables=list(tables))
        self.camp = CampSession(name='项目适配检查', category='project', status='selecting',
                                start_date=date(2026, 10, 1), end_date=date(2026, 12, 1))
        db.session.add(self.camp)
        db.session.flush()

    def unit(self, legacy=False):
        unit = CampUnit(camp_session_id=self.camp.id, unit_type='project', name='项目',
                        owner_user_id=self.owner.id, status='active')
        db.session.add(unit)
        db.session.flush()
        if not legacy:
            projects.bind_new_project(unit, self.actor.id)
        return unit

    def join_camp(self, user):
        db.session.add(CampMember(camp_session_id=self.camp.id, user_id=user.id, role='member'))
        db.session.flush()

    def test_project_lifecycle_and_single_member_source(self):
        unit = self.unit()
        self.join_camp(self.student)
        self.assertEqual(CampUnitMember.query.count(), 0)
        self.assertEqual(projects.unit_role(unit, self.owner), 'leader')
        projects.add_member(unit, self.student.id, 'member', 'leader_pick', self.owner.id)
        self.assertEqual(UnitMembershipRead.query.filter_by(unit_id=unit.id, status='active').count(), 2)
        projects.transfer_leader(unit, self.student.id, self.actor.id, '交接')
        self.assertEqual(unit.owner_user_id, self.student.id)
        self.assertEqual(unit.legacy_owner_user_id, self.owner.id)
        self.assertEqual(projects.unit_role(unit, self.owner), 'member')
        projects.set_status(unit, 'paused', self.actor.id, '暂停')
        with self.assertRaises(GroupError):
            projects.end_member(unit, self.owner.id, 'deselect', 'leader_pick', self.student.id)
        projects.set_status(unit, 'active', self.actor.id, '恢复')
        projects.set_status(unit, 'terminated', self.actor.id, '结束')
        self.assertEqual(GroupMembership.query.filter_by(status='active').count(), 0)
        self.assertEqual(GroupRoleAssignment.query.filter_by(active_slot=1).count(), 0)
        self.assertEqual(CampUnitMember.query.count(), 0)

    def test_selection_idempotency_context_and_camp_archive(self):
        unit = self.unit()
        self.join_camp(self.student)
        version = projects.selection_version(unit.id)
        ops = [{'user_id': self.student.id, 'op': 'add'}]
        result = projects.select_members(unit, self.owner.id, ops, 'request-1', version)
        repeated = projects.select_members(unit, self.owner.id, ops, 'request-1', version)
        self.assertEqual(result, repeated)
        self.assertEqual(result['results'][0]['result'], 'added')
        with self.assertRaises(GroupError):
            projects.select_members(unit, self.owner.id, ops, 'request-2', version)
        with self.assertRaises(GroupError):
            projects.core.get_group(context=GovernanceContext('project', unit.id + 99),
                                    actor_user_id=self.actor.id, group_id=unit.group_id)
        self.camp.status = 'archived'
        db.session.flush()
        with self.assertRaises(GroupError):
            projects.end_member(unit, self.student.id, 'remove', 'admin_adjust', self.actor.id)

    def test_old_mapping_preserves_ids_times_and_is_repeatable(self):
        unit = self.unit(legacy=True)
        self.join_camp(self.owner)
        started = datetime.now() - timedelta(days=10)
        old_owner = CampUnitMember(unit_id=unit.id, user_id=self.owner.id, role='leader',
                                   status='active', started_at=started)
        old_student = CampUnitMember(unit_id=unit.id, user_id=self.student.id, role='member',
                                     status='ended', started_at=started, ended_at=started + timedelta(days=2))
        db.session.add_all([old_owner, old_student])
        db.session.flush()
        old_id = old_owner.id
        # 保留数据库实际存储时间；旧 MySQL DATETIME 字段不保存微秒。
        db.session.refresh(old_owner)
        started = old_owner.started_at
        report = preview_project(unit.id)
        self.assertEqual(report['conflicts'], [])
        result = map_project(unit.id, actor_user_id=self.actor.id, expected_digest=report['digest'])
        self.assertEqual(result['state'], 'mapped')
        self.assertEqual(LegacyUnitMembershipMap.query.count(), 2)
        self.assertEqual(db.session.get(CampUnitMember, old_id).started_at, started)
        mapped = UnitMembershipRead.query.filter_by(unit_id=unit.id, user_id=self.owner.id).one()
        self.assertEqual(mapped.started_at, started)
        self.assertEqual(mapped.source, 'core')
        self.assertEqual(UnitMembershipRead.query.filter_by(unit_id=unit.id).count(), 2)
        count = GroupMembershipEvent.query.count()
        map_project(unit.id, actor_user_id=self.actor.id, expected_digest=report['digest'])
        self.assertEqual(GroupMembershipEvent.query.count(), count)
        with self.assertRaises(RuntimeError):
            with db.session.begin_nested():
                mapped.role = 'member'
                db.session.flush()

    def test_mapping_conflict_is_read_only(self):
        unit = self.unit(legacy=True)
        db.session.add(CampUnitMember(unit_id=unit.id, user_id=self.student.id, role='member', status='active'))
        db.session.flush()
        report = preview_project(unit.id)
        self.assertIn('owner_membership_mismatch', report['conflicts'])
        with self.assertRaises(GroupError):
            map_project(unit.id, actor_user_id=self.actor.id, expected_digest=report['digest'])
        self.assertIsNone(unit.group_id)
        self.assertEqual(GroupMembership.query.count(), 0)

    def test_camp_exit_requires_owner_transfer(self):
        unit = self.unit()
        self.join_camp(self.student)
        projects.add_member(unit, self.student.id, 'member', 'admin', self.actor.id)
        with self.assertRaises(GroupError):
            projects.end_camp_participations(self.camp.id, self.owner.id, self.actor.id, '退出')
        projects.end_camp_participations(self.camp.id, self.student.id, self.actor.id, '退出')
        self.assertIsNone(projects.unit_role(unit, self.student))

    def test_person_rebind_cascade_and_legacy_anchor_guard(self):
        from services.camp_groups.identity import prepare_person_rebind
        from services.auth_context import AuthRejected
        self.unit()
        with self.assertRaises(AuthRejected) as caught:
            prepare_person_rebind(self.owner, self.student.person_id, self.actor.id)
        self.assertEqual(caught.exception.machine, 'GROUP_LEGACY_ANCHOR_REVIEW_REQUIRED')
        result = self.service.create_group(name='独立核心参与', owner_user_id=self.student.id,
            context=self.context, actor_user_id=self.actor.id, idempotency_key='rebind-fixture', reason='检查人员关联')
        member = GroupMembership.query.filter_by(group_id=result['group']['id'], user_id=self.student.id).one()
        prepare_person_rebind(self.student, self.owner.person_id, self.actor.id)
        PersonPrimaryAccountModel.query.filter_by(person_id=self.student.person_id).delete()
        self.student.person_id = self.owner.person_id
        db.session.flush()
        db.session.refresh(member)
        self.assertEqual(member.person_id, self.owner.person_id)
        self.assertEqual(member.user_id, self.student.id)


if __name__ == '__main__':
    unittest.main()

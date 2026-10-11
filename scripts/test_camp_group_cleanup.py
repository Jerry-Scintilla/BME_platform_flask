"""阶段 5 定向检查：旧营兼容、单一招生读取、项目旧实体写边界及旧 API 命名空间。"""
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from flask import Blueprint, Flask, jsonify
import test_training_groups as fixtures
import test_camp_group_adapter as project_fixtures
from exts import db
import models as m
from services.camp_groups import recruitment, training, training_access as access, legacy_boundary
from services.camp_groups.reads import UnitMembershipRead
from services.camp_groups.migration import preview_project, map_project
from services.groups import GroupError


class TrainingCleanup(unittest.TestCase):
    setUp = fixtures.TrainingSmoke.setUp
    tearDown = fixtures.TrainingSmoke.tearDown
    make_user = fixtures.TrainingSmoke.make_user
    client_as = fixtures.TrainingSmoke.client_as

    def recruit(self):
        profile=db.session.get(m.TrainingGroupProfile,self.a.id)
        profile.recruitment_mentor_id=self.owner.id
        recruitment.assign(m.CampMember.query.filter_by(user_id=self.student.id).one(),
                           self.owner.id,self.actor.id,daily=True)
        db.session.commit()
        return m.CampMember.query.filter_by(user_id=self.student.id).one()

    def test_core_recruitment_wins_over_stale_legacy_column_and_sql_agrees(self):
        member=self.recruit()
        # 模拟切换前遗留列；仅夹具直接 SQL，不是新业务写入口。
        table=m.CampMember.__table__
        db.session.connection().execute(table.update().where(table.c.id==member.id).values({table.c.team_mentor_id:self.other.id}))
        db.session.commit();db.session.expire_all()
        self.assertEqual(member.legacy_team_mentor_id,self.other.id)
        self.assertEqual(member.team_mentor_id,self.owner.id)
        self.assertEqual(m.CampMember.query.filter_by(team_mentor_id=self.owner.id).one().id,member.id)
        self.assertEqual(m.CampMember.query.filter_by(team_mentor_id=self.other.id).count(),0)
        from sqlalchemy.orm import aliased
        alias=aliased(m.CampMember)
        joined=db.session.query(alias).join(m.CampSession, m.CampSession.id==alias.camp_session_id).filter(alias.team_mentor_id==self.owner.id)
        self.assertEqual(joined.one().id,member.id)
        recruitment.assign(member,None,self.actor.id,daily=True);db.session.commit()
        self.assertIsNone(member.team_mentor_id)
        self.assertEqual(member.legacy_team_mentor_id,self.other.id)
        self.assertEqual(m.CampMentorMatch.query.count(),0)
        self.assertIsNone(m.CampCourseAssignment.query.one().unit_id)

    def test_legacy_camp_writes_still_work_and_do_not_create_core_memberships(self):
        self.camp.training_groups_ready=False;db.session.commit()
        count=m.GroupMembership.query.count()
        member=m.CampMember.query.filter_by(user_id=self.student.id).one()
        recruitment.assign(member,self.owner.id,self.actor.id,daily=True);db.session.commit()
        self.assertEqual(member.legacy_team_mentor_id,self.owner.id)
        self.assertEqual(member.team_mentor_id,self.owner.id)
        self.assertIsNone(member.recruitment_unit_id)
        self.assertEqual(m.CampMember.query.filter_by(team_mentor_id=self.owner.id).count(),1)
        self.assertEqual(m.CampMentorMatch.query.one().source,'admin')
        recruitment.assign(member,None,self.actor.id,daily=True);db.session.commit()
        self.assertEqual(m.CampMentorMatch.query.count(),0)
        self.assertEqual(m.GroupMembership.query.count(),count)

    def test_ready_camp_rejects_property_and_raw_orm_legacy_writes(self):
        member=self.recruit()
        with self.assertRaises(GroupError):
            member.team_mentor_id=self.other.id
        with self.assertRaises(GroupError), db.session.begin_nested():
            member.legacy_team_mentor_id=self.other.id
            db.session.flush()
        self.assertEqual(member.team_mentor_id,self.owner.id)

    def test_failed_reassignment_keeps_ledger_and_binding(self):
        member=self.recruit()
        db.session.add(m.CampMentorProfile(camp_session_id=self.camp.id,user_id=self.other.id,capacity=0,tags='[]'))
        db.session.commit()
        with self.assertRaises(GroupError), db.session.begin_nested():
            recruitment.assign(member,self.other.id,self.actor.id,daily=True)
        self.assertEqual(member.team_mentor_id,self.owner.id)
        self.assertEqual(m.CampMentorMatch.query.one().mentor_user_id,self.owner.id)
        self.assertEqual(m.CampCourseAssignment.query.one().unit_id,self.a.id)

    def test_retiring_recruitment_mentor_releases_binding_after_owner_transfer(self):
        member=self.recruit()
        training.add_role(self.a,self.other.id,'mentor',self.actor.id)
        training.core.transfer_owner(user_id=self.other.id,**training.command(self.a,self.actor.id,'交接'))
        db.session.expire(self.a,['camp_group'])
        training.end_participation(self.camp.id,self.owner.id,self.actor.id);db.session.commit()
        self.assertIsNone(member.recruitment_unit_id)
        self.assertIsNone(member.team_mentor_id)
        self.assertIsNone(db.session.get(m.TrainingGroupProfile,self.a.id).recruitment_mentor_id)
        self.assertEqual(m.CampMentorMatch.query.count(),0)
        self.assertEqual(m.CampCourseAssignment.query.count(),1)
        self.assertIsNone(m.CampCourseAssignment.query.one().unit_id)

    def test_project_projection_excludes_training_and_live_scope_is_not_cached(self):
        m.CampUnitMember.__table__.create(db.engine,checkfirst=True)
        m.LegacyUnitMembershipMap.__table__.create(db.engine,checkfirst=True)
        training.add_role(self.a,self.student.id,'learner',self.actor.id);db.session.commit()
        self.assertEqual(UnitMembershipRead.query.count(),0)
        self.assertEqual(access.taught_students(self.camp.id,self.owner.id),{self.student.id})
        meeting=SimpleNamespace(camp_session_id=self.camp.id,unit_id=self.a.id,mentor_id=self.owner.id)
        self.assertEqual(access.meeting_students(meeting),[self.student.id])
        self.student.status='disabled';db.session.flush()
        self.assertEqual(access.taught_students(self.camp.id,self.owner.id),set())
        self.assertEqual(access.meeting_students(meeting),[])


class ProjectCleanup(unittest.TestCase):
    setUp = project_fixtures.CampGroupAdapterSmoke.setUp
    tearDown = project_fixtures.CampGroupAdapterSmoke.tearDown
    make_user = project_fixtures.CampGroupAdapterSmoke.make_user
    unit = project_fixtures.CampGroupAdapterSmoke.unit
    join_camp = project_fixtures.CampGroupAdapterSmoke.join_camp

    def test_mapped_legacy_members_cannot_be_written_or_deleted(self):
        unit=self.unit(legacy=True);self.join_camp(self.owner)
        old=m.CampUnitMember(unit_id=unit.id,user_id=self.owner.id,role='leader',status='active')
        db.session.add(old);db.session.commit()
        report=preview_project(unit.id)
        map_project(unit.id,actor_user_id=self.actor.id,expected_digest=report['digest']);db.session.commit()
        with self.assertRaises(GroupError),db.session.begin_nested():
            old.role='member';db.session.flush()
        with self.assertRaises(GroupError),db.session.begin_nested():
            db.session.delete(old);db.session.flush()
        with self.assertRaises(GroupError),db.session.begin_nested():
            db.session.add(m.CampUnitMember(unit_id=unit.id,user_id=self.student.id,role='member'));db.session.flush()
        self.assertEqual(UnitMembershipRead.query.one().source,'core')
        self.assertEqual(UnitMembershipRead.query.one().role,'leader')


class LegacyApiBoundary(unittest.TestCase):
    def test_camp_context_rejected_before_legacy_mutation_but_old_clients_preserved(self):
        app=Flask(__name__);called=[]
        legacy=Blueprint('course_group',__name__)
        @legacy.route('/course-groups',methods=['GET','POST','OPTIONS'])
        def create():
            called.append(True);return jsonify(code=200)
        other=Blueprint('camp',__name__)
        @other.route('/camp/test',methods=['POST'])
        def camp():
            return jsonify(code=200)
        app.register_blueprint(legacy);app.register_blueprint(other);legacy_boundary.install(app)
        client=app.test_client()
        for key in legacy_boundary.CAMP_KEYS:
            response=client.post('/course-groups',json={key:1,'group_id':1})
            self.assertEqual(response.status_code,409)
            self.assertEqual(response.json['machine'],'LEGACY_GROUP_CONTEXT_UNSUPPORTED')
        self.assertEqual(client.get('/course-groups?sid=1').status_code,409)
        self.assertEqual(client.post('/course-groups',data={'unit_id':'1'}).status_code,409)
        self.assertEqual(called,[])
        self.assertEqual(client.post('/course-groups',json={'group_id':1,'course_id':1}).status_code,200)
        self.assertEqual(client.post('/camp/test',json={'sid':1}).status_code,200)
        self.assertEqual(client.options('/course-groups?sid=1').status_code,200)


if __name__=='__main__':
    unittest.main()

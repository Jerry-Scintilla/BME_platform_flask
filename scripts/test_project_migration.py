"""显式映射、唯一写入与原考核事实的定向检查。"""
import unittest
from datetime import date
import test_project_camps as fixtures
from exts import db
import models as m
from services.projects import core,legacy
from services.camp_groups import projects
from services.camp_groups.reads import UnitMembershipRead
from services.groups import GroupError


class ProjectMigrationTest(fixtures.ProjectCampsTest):
    def legacy_unit(self):
        unit=m.CampUnit(camp_session_id=self.camp.id,name='存量研发项目',unit_type='project',owner_user_id=self.owner.id,status='active')
        db.session.add(unit);db.session.flush();projects.bind_new_project(unit,self.actor.id)
        projects.ensure_camp_participant(self.camp.id,self.student.id,self.actor.id,'原参营人员')
        projects.add_member(unit,self.student.id,'member','admin',self.actor.id)
        node=m.CampMilestone(camp_session_id=self.camp.id,unit_id=unit.id,title='原考核节点')
        db.session.add(node);db.session.flush()
        db.session.add(m.CampNodeEvaluation(camp_session_id=self.camp.id,unit_id=unit.id,milestone_id=node.id,member_user_id=self.student.id,leader_user_id=self.owner.id,score=0))
        db.session.commit();return unit,node

    def migrate(self,unit,action,digest):
        r=self.call(self.actor,'post',f'/project-core/legacy/{unit.id}/migrate',json={'action':action,'source_digest':digest,'reason':'独立项目切换检查'})
        self.assertEqual(r.status_code,200,r.json);return r.json

    def test_prepare_compare_switch_preserves_ids_members_and_score(self):
        unit,node=self.legacy_unit();digest=legacy.preview(unit.id)['source_digest'];count=m.GroupMembership.query.count()
        prepared=self.migrate(unit,'prepare',digest)
        self.assertEqual(self.call(self.owner,'get',f"/project-core/projects/{prepared['project_id']}").status_code,403)
        self.assertEqual(UnitMembershipRead.query.filter_by(unit_id=unit.id,status='active').count(),2)
        switched=self.migrate(unit,'switch',digest)
        self.assertEqual(self.migrate(unit,'switch',digest)['project_id'],switched['project_id'])
        self.assertEqual(m.GroupMembership.query.count(),count)
        self.assertEqual(UnitMembershipRead.query.filter_by(unit_id=unit.id,status='active').count(),2)
        self.assertEqual(m.CampNodeEvaluation.query.filter_by(milestone_id=node.id).one().score,0)
        self.assertEqual(m.ProjectMilestone.query.count(),0)
        self.assertEqual(projects.unit_role(unit,self.owner),'leader')
        result=projects.select_members(unit,self.owner.id,[{'user_id':self.student.id,'op':'remove'}],'old-write',projects.selection_version(unit.id))
        self.assertEqual(result['results'][0]['result'],'forbidden')
        self.assertEqual(UnitMembershipRead.query.filter_by(unit_id=unit.id,status='active').count(),2)
        db.session.rollback()
        projects.end_camp_participations(self.camp.id,self.student.id,self.actor.id,'退出本期')
        db.session.commit()
        self.assertEqual(core.role(db.session.get(m.Project,switched['project_id']),self.student),'member')
        self.assertEqual(UnitMembershipRead.query.filter_by(unit_id=unit.id,status='active').count(),1)

    def test_stale_mapping_cancel_and_reprepare(self):
        unit,node=self.legacy_unit();digest=legacy.preview(unit.id)['source_digest']
        prepared=self.migrate(unit,'prepare',digest)
        unit.legacy_name='项目背景来源变化';db.session.commit()
        r=self.call(self.actor,'post',f'/project-core/legacy/{unit.id}/migrate',json={'action':'switch','source_digest':digest,'reason':'过期检查'})
        self.assertEqual(r.status_code,409)
        digest=legacy.preview(unit.id)['source_digest'];self.migrate(unit,'cancel',digest)
        self.assertEqual(core.role(db.session.get(m.Project,prepared['project_id']),self.owner),None)
        self.migrate(unit,'prepare',digest);self.migrate(unit,'switch',digest)

    def test_unactivated_shadow_is_not_frozen_by_camp_close(self):
        from services.projects.camps import close_camp_bindings
        unit,_=self.legacy_unit();digest=legacy.preview(unit.id)['source_digest']
        prepared=self.migrate(unit,'prepare',digest)
        close_camp_bindings(self.camp.id,self.actor.id);db.session.commit()
        self.assertIsNone(db.session.get(m.ProjectBindingSnapshot,prepared['binding_id']))
        self.migrate(unit,'cancel',digest)
        self.migrate(unit,'prepare',digest)


    def test_archived_legacy_stays_on_original_archive_path(self):
        unit,node=self.legacy_unit();self.camp.status='archived';db.session.commit()
        source=legacy.preview(unit.id)
        self.assertFalse(source['ready'])
        response=self.call(self.actor,'post',f'/project-core/legacy/{unit.id}/migrate',json={'action':'prepare','source_digest':source['source_digest'],'reason':'历史档案保持原读取'})
        self.assertEqual(response.status_code,409,response.json)
        self.assertEqual(m.ProjectLegacyMap.query.count(),0)
        self.assertEqual(m.CampNodeEvaluation.query.filter_by(milestone_id=node.id).one().score,0)


if __name__=='__main__':unittest.main()

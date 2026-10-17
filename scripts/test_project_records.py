"""项目记录、附件权限、冻结共享与提醒去重的定向检查。"""
import io,json,unittest
from datetime import date
from unittest.mock import patch
import test_project_camps as fixtures
from exts import db
import models as m
from services.projects import core,records,reminders


class ProjectRecordsTest(fixtures.ProjectCampsTest):
    def save_record(self,p,b=None,files=False,title='项目记录'):
        row=db.session.get(m.Project,p['id'])
        args={'kind':'meeting','title':title,'content':'本次验证结论','occurred_on':date.today().isoformat(),'reason':'研发留痕','binding_id':b['id'] if b else None,'expected_version':row.version,'idempotency_key':'record-'+str(row.version)}
        path=f'/project-core/projects/{row.id}/records'
        if files:
            with patch.object(records.storage,'put_object'):
                return self.call(self.owner,'post',path,data={'metadata':json.dumps(args),'files':(io.BytesIO(b'private-data'),'record.txt')},content_type='multipart/form-data')
        return self.call(self.owner,'post',path,json=args)

    def test_record_files_acl_and_revocation(self):
        p=self.create_project();r=self.save_record(p,files=True)
        self.assertEqual(r.status_code,200,r.json)
        aid=r.json['record']['files'][0]['id']
        self.assertEqual(self.call(self.student,'get',f'/project-core/files/{aid}/token').status_code,403)

        self.assertEqual(self.call(self.owner,'get',f'/project-core/files/{aid}/token').status_code,200)
        with patch.object(records.storage,'get_object',return_value=b'private-data'):
            self.assertEqual(self.call(self.owner,'get',f'/project-core/files/{aid}').data,b'private-data')
        row=db.session.get(m.Project,p['id']);self.mutate(row.id,{'action':'add','user_id':self.student.id,'reason':'加入','expected_version':row.version,'idempotency_key':'add-reader'})
        self.assertEqual(self.call(self.student,'get',f'/project-core/files/{aid}/token').status_code,200)
        row=db.session.get(m.Project,row.id);self.mutate(row.id,{'action':'remove','user_id':self.student.id,'reason':'退出','expected_version':row.version,'idempotency_key':'remove-reader'})
        self.assertEqual(self.call(self.student,'get',f'/project-core/files/{aid}/token').status_code,403)

    def test_old_record_deep_link_survives_recent_list_limit(self):
        p,b=self.setup_binding();self.approve(p,b);project=db.session.get(m.Project,p['id'])
        for index in range(201):db.session.add(m.ProjectRecord(project_id=project.id,binding_id=b['id'],kind='record',title='长期记录'+str(index),occurred_on=date.today(),created_by=self.owner.id,status='published'))
        db.session.commit();first=m.ProjectRecord.query.order_by(m.ProjectRecord.id).first()
        attachment=m.ProjectRecordFile(record_id=first.id,object_key='project/history/file.txt',filename='历史附件.txt',size=1,sha256='a'*64)
        db.session.add(attachment);db.session.commit()
        path=f'/project-core/projects/{project.id}/records'
        recent=self.call(self.owner,'get',path).json['records']
        self.assertEqual(len(recent),200);self.assertNotIn(first.id,[r['id'] for r in recent])
        result=self.call(self.owner,'get',path+f'?record_id={first.id}')
        self.assertEqual([r['id'] for r in result.json['records']],[first.id])
        self.assertEqual(self.call(self.teacher,'get',path+f'?record_id={first.id}').status_code,403)
        self.assertEqual(self.call(self.teacher,'get',f'/project-core/files/{attachment.id}/token?binding={b["id"]}').status_code,200)
        self.assertEqual(self.call(self.owner,'get',path+'?record_id=invalid').status_code,400)

    def test_shared_records_are_frozen_when_binding_ends(self):
        p,b=self.setup_binding();approved=self.approve(p,b)
        response=self.save_record(p,b);self.assertEqual(response.status_code,200,response.json)
        row=db.session.get(m.Project,p['id']);binding=db.session.get(m.ProjectCampBinding,b['id'])
        r=self.call(self.teacher,'post',f'/project-core/projects/{row.id}/camps/{binding.id}',json={'action':'end','expected_version':row.version,'expected_binding_version':binding.version,'reason':'本期结束','idempotency_key':'end-binding'})
        self.assertEqual(r.status_code,200,r.json)
        saved=db.session.get(m.ProjectRecord,response.json['record']['id']);saved.content='结营后的新记录内容';db.session.commit()
        result=self.call(self.teacher,'get',f'/project-core/projects/{row.id}/records?binding={binding.id}')
        self.assertEqual(result.status_code,200,result.json)
        self.assertEqual(result.json['records'][0]['content'],'本次验证结论')
        self.assertEqual(core.role(row,self.owner),'leader')
        self.assertEqual(self.call(self.teacher,'get',f'/project-core/projects/{row.id}/records').status_code,403)
        retry=self.call(self.owner,'post',f'/project-core/projects/{row.id}/camps',json={'camp_id':binding.camp_session_id,'user_ids':[self.owner.id,self.student.id],'expected_version':row.version,'reason':'不可覆盖冻结本期','idempotency_key':'retry-ended-camp'})
        self.assertEqual(retry.status_code,409,retry.json)
        self.assertEqual(db.session.get(m.ProjectCampBinding,binding.id).status,'ended')

    def test_reminder_dry_run_and_idempotence(self):
        p=self.create_project();row=db.session.get(m.Project,p['id'])
        db.session.add(m.ProjectMilestone(project_id=row.id,title='今日验收',due_on=date.today(),schedule_mode='absolute',status='planned',created_by=self.owner.id));db.session.commit()
        self.assertEqual(len(reminders.run(apply=False)),1)
        self.assertEqual(m.ProjectReminder.query.count(),0)
        self.assertEqual(len(reminders.run(apply=True)),1)
        self.assertEqual(len(reminders.run(apply=True)),0)
        self.assertEqual(m.NotificationModel.query.filter_by(source_type='project_core').count(),1)

    def test_former_participant_archive_only_keeps_own_assessment(self):
        p,b=self.setup_binding();self.approve(p,b)
        project=db.session.get(m.Project,p['id']);binding=db.session.get(m.ProjectCampBinding,b['id'])
        node=m.CampMilestone(camp_session_id=self.camp.id,unit_id=binding.legacy_unit_id,title='个人历史评价')
        db.session.add(node);db.session.flush()
        db.session.add(m.CampNodeEvaluation(camp_session_id=self.camp.id,unit_id=binding.legacy_unit_id,milestone_id=node.id,member_user_id=self.student.id,leader_user_id=self.owner.id,score=0))
        db.session.commit()
        path=f'/project-core/projects/{project.id}/camps/{binding.id}'
        def change(action,key,**kw):
            response=self.call(self.teacher,'post',path,json={'action':action,'expected_version':project.version,'expected_binding_version':binding.version,'reason':'检查退出后的历史边界','idempotency_key':key,**kw})
            self.assertEqual(response.status_code,200,response.json)
        change('participants','end-student',user_ids=[self.owner.id])
        self.assertEqual(self.save_record(p,b,title='退出后新增的本期共享内容').status_code,200)
        change('end','seal-period')
        response=self.mutate(project.id,{'action':'remove','user_id':self.student.id,'expected_version':project.version,'reason':'结束长期参与','idempotency_key':'end-long-member'})
        self.assertEqual(response.status_code,200,response.json)
        archive=self.call(self.student,'get',path+'/archive')
        self.assertEqual(archive.status_code,200,archive.json)
        self.assertEqual(archive.json['archive']['records'],[])
        self.assertEqual(archive.json['archive']['assessment_nodes'][0]['evaluations'][0]['score'],0)
        self.assertEqual(self.call(self.student,'get',f'/project-core/projects/{project.id}/records?binding={binding.id}').json['records'],[])

    def test_storage_failure_keeps_previous_records_and_cleans_objects(self):
        p=self.create_project();row=db.session.get(m.Project,p['id'])
        args={'kind':'record','title':'附件失败','occurred_on':date.today().isoformat(),'reason':'检查回滚','expected_version':row.version,'idempotency_key':'storage-failure'}
        with patch.object(records.storage,'put_object',side_effect=OSError('storage unavailable')),patch.object(records.storage,'remove_object') as cleanup:
            result=self.call(self.owner,'post',f'/project-core/projects/{row.id}/records',data={'metadata':json.dumps(args),'files':(io.BytesIO(b'x'),'file.txt')},content_type='multipart/form-data')
        self.assertEqual(result.status_code,500)
        self.assertEqual(m.ProjectRecord.query.count(),0);self.assertEqual(m.ProjectRecordFile.query.count(),0)
        cleanup.assert_called_once()


if __name__=='__main__':unittest.main()

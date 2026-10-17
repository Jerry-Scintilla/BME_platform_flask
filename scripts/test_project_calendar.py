"""D0与项目日历定向检查。"""
import unittest
from datetime import date,timedelta
import test_project_core as fixtures
from exts import db
from models import Project,ProjectMilestone,ProjectCalendarEvent
from services.projects import calendar


class ProjectCalendarTest(fixtures.ProjectCoreTest):
    def save(self,p,data,source='event',eid=None):
        p=db.session.get(Project,p['id'])
        payload={'title':'研发计划','reason':'日历检查','expected_version':p.version,'idempotency_key':f'{source}-{eid}-{p.version}',**data}
        path=f'/project-core/projects/{p.id}/schedule/{source}'+(f'/{eid}' if eid else '')
        r=self.call(self.owner,'patch' if eid else 'post',path,json=payload)
        self.assertEqual(r.status_code,200,r.json)
        return r.json['item']

    def zero(self,p,value):
        row=db.session.get(Project,p['id'])
        return self.call(self.owner,'patch',f'/project-core/projects/{row.id}/zero-date',json={'zero_date':value,'confirm':True,'reason':'确认启动日','expected_version':row.version,'idempotency_key':'zero-'+str(row.version)})

    def test_zero_offsets_fixed_actual_and_rebase(self):
        p=self.create_project();today=date.today()
        first=self.save(p,{'schedule_mode':'relative','offset_days':21},'milestone')
        fixed=self.save(p,{'schedule_mode':'absolute','due_on':(today+timedelta(days=30)).isoformat()})
        self.assertIsNone(first['due_on'])
        self.assertEqual(self.zero(p,today.isoformat()).status_code,200)
        done=self.save(p,{'schedule_mode':'relative','offset_days':-7,'status':'done','actual_on':today.isoformat()},'milestone')
        version=db.session.get(Project,p['id']).version
        preview=self.call(self.owner,'post',f"/project-core/projects/{p['id']}/zero-date/preview",json={'zero_date':(today+timedelta(days=2)).isoformat()})
        self.assertEqual(len(preview.json['changes']),1)
        self.assertEqual(db.session.get(Project,p['id']).version,version)
        self.assertEqual(self.zero(p,(today+timedelta(days=2)).isoformat()).status_code,200)
        entries={r['key']:r for r in self.call(self.owner,'get',f"/project-core/projects/{p['id']}/calendar").json['items']}
        self.assertEqual(entries[first['key']]['due_on'],(today+timedelta(days=23)).isoformat())
        self.assertEqual(entries[fixed['key']]['due_on'],fixed['due_on'])
        self.assertEqual(entries[done['key']]['due_on'],done['due_on'])
        self.assertEqual(entries[done['key']]['actual_on'],today.isoformat())
        edited=self.save(p,{'title':'完成后补充说明','schedule_mode':'relative','offset_days':-7,'status':'done','actual_on':today.isoformat()},'milestone',done['id'])
        self.assertEqual(edited['due_on'],done['due_on'])

    def test_range_permissions_and_invalid_completion(self):
        p=self.create_project()
        base=f"/project-core/projects/{p['id']}"
        args={'title':'关键事件','reason':'检查','schedule_mode':'relative','offset_days':True,'expected_version':1,'idempotency_key':'invalid'}
        self.assertEqual(self.call(self.owner,'post',base+'/schedule/event',json=args).status_code,400)
        self.assertEqual(ProjectCalendarEvent.query.count(),0)
        args.update(offset_days=0,status='done')
        self.assertEqual(self.call(self.owner,'post',base+'/schedule/event',json=args).status_code,400)
        self.assertEqual(self.call(self.student,'get',base+'/calendar').status_code,403)
        self.assertEqual(self.call(self.owner,'get',base+'/calendar?from=2026-01-01').status_code,400)
        self.assertEqual(self.call(self.owner,'get',base+'/calendar?from=2026-01-01&to=2028-01-01').status_code,400)


if __name__=='__main__':unittest.main()

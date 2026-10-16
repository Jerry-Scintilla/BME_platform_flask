"""项目组会、研发记录、成果和受授权附件。"""
import hashlib
import os
import uuid
from exts import db
from storage import storage
from models import Project,ProjectRecord,ProjectRecordFile,ProjectBindingSnapshot,ProjectCampBinding,ProjectChange,UserModel
from . import core,calendar,camps,ProjectError


def serialize(row,user=None,project=None,current=False):
    files=ProjectRecordFile.query.filter_by(record_id=row.id)
    if current:files=files.populate_existing().with_for_update()
    return {'id':row.id,'kind':row.kind,'title':row.title,'content':row.content,'occurred_on':row.occurred_on.isoformat(),
            'status':row.status,'created_by':row.created_by,'author':db.session.get(UserModel,row.created_by).username,
            'binding_id':row.binding_id,'verified_by':row.verified_by,
            'can_edit':bool(user and project and project.status=='active' and core.role(project,user) and (row.created_by==user.id or core.role(project,user) in ('admin','leader'))),
            'can_verify':bool(user and user.is_admin() and row.kind=='outcome' and project and project.status=='active'),
            'files':[{'id':f.id,'filename':f.filename,'size':f.size} for f in files]}


def read(project,user,binding=None,start=None,end=None,force_binding=False,record_id=None):
    def query(q):
        if record_id:q=q.filter_by(id=record_id)
        if start:q=q.filter(ProjectRecord.occurred_on.between(start,end))
        return q.order_by(ProjectRecord.occurred_on.desc(),ProjectRecord.id.desc()).limit(501 if start else 200)
    if core.role(project,user) and not force_binding:
        return [serialize(r,user,project) for r in query(ProjectRecord.query.filter_by(project_id=project.id))]
    if not binding:raise ProjectError('无权读取项目记录',status=403)
    frozen=db.session.get(ProjectBindingSnapshot,binding.id)
    if binding.status=='ended':
        if not camps.staff(user,binding.camp_session_id) and not (frozen and any(p['user_id']==user.id for p in frozen.snapshot.get('participants',[]))):return []
        return [r for r in (frozen.snapshot.get('records',[]) if frozen else []) if (not record_id or r['id']==record_id) and (not start or start.isoformat()<=r['occurred_on']<=end.isoformat())]
    if not camps.staff(user,binding.camp_session_id):raise ProjectError('无权读取项目记录',status=403)
    return [serialize(r) for r in query(ProjectRecord.query.filter_by(project_id=project.id,binding_id=binding.id))]


def freeze(binding,actor_id):
    if db.session.get(ProjectBindingSnapshot,binding.id):return
    project=db.session.get(Project,binding.project_id)
    data={'project':core.snapshot(project),'participants':camps.summary(binding,db.session.get(UserModel,actor_id))['participants'],
          'records':[serialize(r,current=True) for r in ProjectRecord.query.filter_by(project_id=project.id,binding_id=binding.id).populate_existing().with_for_update()],
          'zero_date':project.zero_date.isoformat() if project.zero_date else None}
    data['participation_history']=[{'user_id':p.user_id,'name':db.session.get(UserModel,p.user_id).username,'status':p.status,'started_at':p.started_at.isoformat(),'ended_at':p.ended_at.isoformat() if p.ended_at else None} for p in camps.ProjectCampParticipant.query.filter_by(binding_id=binding.id).populate_existing().with_for_update()]
    if binding.legacy_unit_id:
        from models import CampUnit,CampMilestone,CampSubmissionAttachment,CampOutcome,CampMeeting,CampNodeEvaluation,CampSubmissionVersion
        from services.camp_groups.reads import current_members
        from services.camp_groups.delivery import progress
        unit=db.session.get(CampUnit,binding.legacy_unit_id)
        roster=[r for r in current_members(unit) if r.status=='active']
        data['participants']=[{'user_id':r.user_id,'name':db.session.get(UserModel,r.user_id).username,'role':r.role} for r in roster]
        nodes=[]
        for node in CampMilestone.query.filter_by(unit_id=unit.id).order_by(CampMilestone.order_no).populate_existing().with_for_update():
            evaluations=CampNodeEvaluation.query.filter_by(milestone_id=node.id).populate_existing().with_for_update().all()
            submissions=CampSubmissionVersion.query.filter_by(milestone_id=node.id).populate_existing().with_for_update().all()
            state=progress(node,unit,roster,evaluations,submissions)
            nodes.append({'id':node.id,'title':node.title,'due_date':node.due_date.isoformat() if node.due_date else None,'node_complete':state['node_complete'],
                'evaluations':[{'member_user_id':e.member_user_id,'name':db.session.get(UserModel,e.member_user_id).username,'leader_user_id':e.leader_user_id,'score':e.score,'comment':e.comment,'evaluated_at':e.updated_at.isoformat() if e.updated_at else None} for e in evaluations],
                'submissions':[{'id':s.id,'submitted_by':s.submitted_by,'name':db.session.get(UserModel,s.submitted_by).username,'version':s.version,'content':s.content,'status':s.status,
                    'attachments':[{'id':a.id,'filename':a.filename} for a in CampSubmissionAttachment.query.filter_by(submission_id=s.id).populate_existing().with_for_update()]} for s in state['latest_submissions'].values()]})
        data['assessment_nodes']=nodes
        data['camp_outcomes']=[{'title':o.title,'description':o.description,'status':o.status} for o in CampOutcome.query.filter_by(unit_id=unit.id).populate_existing().with_for_update()]
        data['camp_meetings']=[{'id':m.id,'title':m.title,'date':m.meeting_date.isoformat(),'content':m.content} for m in CampMeeting.query.filter_by(unit_id=unit.id,scope='unit').populate_existing().with_for_update()]
    db.session.add(ProjectBindingSnapshot(binding_id=binding.id,snapshot=data,frozen_by=actor_id))


def save(user,pid,data,files,uploaded,rid=None):
    project=core.lock(core.get_project(pid,user,write=True),user)
    core.get_project(pid,user,write=True,fresh=True)
    digests=[];total=0
    for f in files:
        h=hashlib.sha256();size=0
        while True:
            chunk=f.stream.read(1024*128)
            if not chunk:break
            size+=len(chunk);total+=len(chunk);h.update(chunk)
            if total>48*1024*1024:raise ProjectError('附件合计不能超过48MB',status=413)
        f.stream.seek(0);digests.append({'filename':f.filename,'size':size,'sha256':h.hexdigest()})
    if len(files)>20:raise ProjectError('附件最多20个',status=413)
    data=dict(data,file_digests=digests)
    cid,digest,replay=core.command(project,user,data,'record:'+str(rid or 'create'))
    if replay:return replay
    row=db.session.get(ProjectRecord,rid) if rid else ProjectRecord(project_id=pid,created_by=user.id)
    if row is None or row.project_id!=pid:raise ProjectError('记录不属于此项目',status=404)
    if rid and row.created_by!=user.id and core.role(project,user) not in ('admin','leader'):raise ProjectError('只能修改自己的记录',status=403)
    old=serialize(row) if rid else None
    kind=data.get('kind',row.kind)
    if kind not in ('meeting','record','outcome'):raise ProjectError('记录类型无效',status=400)
    if rid and kind!=row.kind:raise ProjectError('不能变更已有记录的类型',status=400)
    if kind=='outcome' and core.role(project,user) not in ('leader','admin'):raise ProjectError('成果由负责人登记',status=403)
    row.kind=kind;row.title=core.text(data.get('title',row.title),'记录标题',200)
    row.content=core.text(data.get('content',row.content),'内容',64000,True)
    if row.content and len(row.content.encode('utf-8'))>64*1024:raise ProjectError('文字内容不能超过64KB',status=413)
    row.occurred_on=core.day(data.get('occurred_on',row.occurred_on.isoformat() if row.occurred_on else None))
    if row.occurred_on is None or row.occurred_on>calendar.today(project):raise ProjectError('请填写不晚于今天的实际日期',status=400)
    binding_id=data.get('binding_id',row.binding_id)
    if binding_id:
        binding,_,_=camps.get_binding(pid,core.number(binding_id),user)
        if binding.status!='active' and (not rid or row.binding_id!=binding_id):raise ProjectError('仅可向有效本期关联共享新记录')
    row.binding_id=binding_id or None;row.status='pending' if kind=='outcome' else 'published';row.verified_by=None
    existing=ProjectRecordFile.query.filter_by(record_id=rid).all() if rid else []
    if len(existing)+len(files)>20 or sum(f.size for f in existing)+total>48*1024*1024:raise ProjectError('此记录附件最多20个、合计48MB',status=413)
    db.session.add(row);db.session.flush()
    for f,meta in zip(files,digests):
        key=f'project-core/{pid}/records/{row.id}/{uuid.uuid4().hex}{os.path.splitext(f.filename or "")[1].lower()[:20]}'
        uploaded.append(key);storage.put_object(key,f.stream,content_type=f.mimetype or 'application/octet-stream')
        db.session.add(ProjectRecordFile(record_id=row.id,object_key=key,filename=(f.filename or '附件')[:200],size=meta['size'],sha256=meta['sha256'],content_type=(f.mimetype or 'application/octet-stream')[:100]))
    db.session.flush();project.version+=1
    after=serialize(row,user,project)
    change=core.event(project,user,'record:save',core.text(data.get('reason'),'记录原因',500),old,{'record':after})
    notify(project,user,change)
    return core.save_command(project,user,cid,digest,{'record':after,'project':core.serialize(project,user)})


def notify(project,user,change):
    from blueprints.notification import create_notification
    db.session.flush()
    for member in core.members(project):
        if member['user_id']!=user.id and member['role']:
            create_notification(member['user_id'],'项目有新记录',f'「{project.title}」有项目记录更新。',category='system',source_type='project_core',source_id=change.id)


def verify(user,pid,rid,data):
    project=core.lock(core.get_project(pid,user,write=True),user)
    if not user.is_admin():raise ProjectError('项目成果由平台管理员核验',status=403)
    cid,digest,replay=core.command(project,user,data,'record:verify:'+str(rid))
    if replay:return replay
    row=db.session.get(ProjectRecord,rid)
    if not row or row.project_id!=pid or row.kind!='outcome':raise ProjectError('项目成果不存在',status=404)
    if data.get('status') not in ('verified','rejected'):raise ProjectError('核验结果无效',status=400)
    row.status=data['status'];row.verified_by=user.id;project.version+=1
    core.event(project,user,'record:verify',core.text(data.get('reason'),'核验说明',500),after={'record_id':rid,'status':row.status})
    return core.save_command(project,user,cid,digest,{'record':serialize(row,user,project),'project':core.serialize(project,user)})


def file_access(aid,user,bid=None):
    attachment=db.session.get(ProjectRecordFile,aid)
    if not attachment:raise ProjectError('附件不存在',status=404)
    row=db.session.get(ProjectRecord,attachment.record_id);project=db.session.get(Project,row.project_id)
    if core.role(project,user):return attachment
    if not bid:raise ProjectError('无权下载项目附件',status=403)
    binding,_,_=camps.get_binding(project.id,bid,user)
    records=read(project,user,binding,record_id=row.id)
    if not any(r['id']==row.id and any(f['id']==aid for f in r['files']) for r in records):raise ProjectError('无权下载此附件',status=403)
    return attachment


def extra_calendar(project,user,binding=None,start=None,end=None,force_binding=False):
    items=[]
    basis=core.day(camps.project_view(project,user,binding,force_binding)['zero_date']) if binding else project.zero_date
    start,end=calendar.bounds(start,end)
    for r in read(project,user,binding,start,end,force_binding):
        happened=core.day(r['occurred_on'])
        items.append({'id':r['id'],'key':f'record:{r["id"]}','source':'record','kind':r['kind'],'title':r['title'],'content':r['content'],
          'due_on':r['occurred_on'],'actual_on':r['occurred_on'],'schedule_mode':'absolute','offset_days':None,
          'status':'planned' if r['status']=='pending' else 'cancelled' if r['status']=='rejected' else 'done',
          'day_number':(happened-basis).days if basis else None,'actual_day_number':(happened-basis).days if basis else None,'overdue':False,'can_edit':False})
    from models import CampMilestone
    bindings=ProjectCampBinding.query.filter_by(project_id=project.id).all() if not binding else [binding]
    for b in bindings:
        visible=camps.staff(user,b.camp_session_id) or db.session.query(camps.ProjectCampParticipant.id).filter_by(binding_id=b.id,user_id=user.id).first()
        if not visible or not b.legacy_unit_id:continue
        query=CampMilestone.query.filter_by(unit_id=b.legacy_unit_id)
        if start:query=query.filter(CampMilestone.due_date.between(start,end))
        for node in query.limit(501):
            items.append({'id':node.id,'key':f'assessment:{node.id}','source':'assessment','kind':'assessment','title':node.title+' · '+db.session.get(camps.CampSession,b.camp_session_id).name,
                'content':'本期考核记录；研发完成状态单独管理。','due_on':node.due_date.isoformat() if node.due_date else None,'actual_on':None,'schedule_mode':'absolute','offset_days':None,'status':'done' if node.status=='approved' else 'planned','day_number':(node.due_date-basis).days if node.due_date and basis else None,'actual_day_number':None,'overdue':False,'can_edit':False,'binding_id':b.id,'unit_id':b.legacy_unit_id})
    return items

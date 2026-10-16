"""项目计划日期与实际事实；日历读取不写事件或积分。"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from sqlalchemy import or_, and_
from exts import db
from models import ProjectMilestone, ProjectCalendarEvent
from . import core, ProjectError

SOURCES = {'milestone':ProjectMilestone,'event':ProjectCalendarEvent}


def shift(base, offset):
    try:return base+timedelta(days=offset)
    except OverflowError:raise ProjectError('相对日期超出支持的范围',status=400)


def today(project):
    return datetime.now(ZoneInfo(project.timezone)).date()


def due(row, project):
    if row.frozen_due_on is not None:return row.frozen_due_on
    if row.schedule_mode=='absolute':return row.due_on
    return shift(project.zero_date,row.offset_days) if project.zero_date is not None else None


def item(row, project, source):
    planned=due(row,project)
    return {'id':row.id,'key':f'{source}:{row.id}','source':source,'kind':getattr(row,'kind','milestone'),
            'title':row.title,'content':row.content,'schedule_mode':row.schedule_mode,
            'offset_days':row.offset_days,'due_on':planned.isoformat() if planned else None,
            'actual_on':row.actual_on.isoformat() if row.actual_on else None,'status':row.status,
            'day_number':(planned-project.zero_date).days if planned and project.zero_date else None,
            'actual_day_number':(row.actual_on-project.zero_date).days if row.actual_on and project.zero_date else None,
            'overdue':bool(planned and planned<today(project) and row.status=='planned'),
            'created_by':row.created_by,'accepted_by':row.accepted_by,'can_edit':True}


def rows(project):
    for source, model in SOURCES.items():
        for row in model.query.filter_by(project_id=project.id).order_by(model.id):yield source,row


def bounds(start=None,end=None):
    start,end=core.day(start),core.day(end)
    if bool(start)!=bool(end) or (start and (end<start or (end-start).days>366)):
        raise ProjectError('查询须提供不超过366天的起止日期',status=400)
    return start,end


def list_items(project, start=None, end=None):
    start,end=bounds(start,end)
    result=[];truncated=False
    for source,model in SOURCES.items():
        query=model.query.filter_by(project_id=project.id)
        if start:
            limits=[model.due_on.between(start,end),model.actual_on.between(start,end),model.frozen_due_on.between(start,end)]
            if project.zero_date:
                limits.append(and_(model.schedule_mode=='relative',model.frozen_due_on.is_(None),model.offset_days.between((start-project.zero_date).days,(end-project.zero_date).days)))
            query=query.filter(or_(*limits))
        data=query.order_by(model.id).limit(501).all()
        truncated=truncated or len(data)>500
        result.extend(item(r,project,source) for r in data[:500])
    return {'items':sorted(result,key=lambda r:(r['due_on'] or '9999',r['key'])),'truncated':truncated}


def preview_zero(project, value):
    new=core.day(value)
    if new is None:raise ProjectError('请指定正式启动日',status=400)
    changes=[]
    for source,row in rows(project):
        old=due(row,project)
        if row.schedule_mode=='relative' and row.frozen_due_on is None and row.status=='planned' and (old is None or old>=today(project)):
            target=shift(new,row.offset_days)
            if target!=old:changes.append({'key':f'{source}:{row.id}','title':row.title,'before':old.isoformat() if old else None,'after':target.isoformat()})
    return {'project_version':project.version,'zero_date':new.isoformat(),'changes':changes}


def change_zero(user,pid,data):
    project=core.lock(core.get_project(pid,user,manage=True,write=True),user)
    core.get_project(pid,user,manage=True,write=True,fresh=True)
    cid,digest,replay=core.command(project,user,data,'zero-date')
    if replay:return replay
    change=preview_zero(project,data.get('zero_date'))
    if data.get('confirm') is not True:raise ProjectError('请先预览并确认日期调整',status=400)
    reason=core.text(data.get('reason'),'修改原因',500)
    before=core.snapshot(project)
    for _,row in rows(project):
        previous=due(row,project)
        if row.schedule_mode=='relative' and previous and (row.status!='planned' or previous<today(project)):
            row.frozen_due_on=previous
    project.zero_date=core.day(data['zero_date']);project.zero_date_confirmed_at=datetime.now();project.version+=1
    core.event(project,user,'zero-date',reason,before,{'project':core.snapshot(project),'changes':change['changes']})
    return core.save_command(project,user,cid,digest,{'project':core.serialize(project,user),'changes':change['changes']})


def save_item(user,pid,source,data,eid=None):
    model=SOURCES.get(source)
    if model is None:raise ProjectError('日历来源无效',status=400)
    project=core.lock(core.get_project(pid,user,manage=True,write=True),user)
    core.get_project(pid,user,manage=True,write=True,fresh=True)
    cid,digest,replay=core.command(project,user,data,f'{source}:{eid or "create"}')
    if replay:return replay
    row=db.session.get(model,eid) if eid else model(project_id=pid,created_by=user.id)
    if row is None or row.project_id!=pid:raise ProjectError('记录不属于此项目',status=404)
    before=item(row,project,source) if eid else None
    reason=core.text(data.get('reason'),'变更原因',500)
    row.title=core.text(data.get('title',row.title),'标题',200)
    row.content=core.text(data.get('content',row.content),'说明',64000,True)
    if row.content and len(row.content.encode('utf-8'))>64*1024:raise ProjectError('文字内容不能超过64KB',status=413)
    status=data.get('status',row.status or 'planned')
    if not eid or any(k in data for k in ('schedule_mode','offset_days','due_on')):
        mode=data.get('schedule_mode',row.schedule_mode or 'absolute')
        if mode not in ('absolute','relative'):raise ProjectError('时间模式无效',status=400)
        old_basis=(row.schedule_mode,row.due_on,row.offset_days)
        if mode=='absolute':new_due=core.day(data.get('due_on',row.due_on.isoformat() if row.due_on else None));new_offset=None
        else:
            offset=data.get('offset_days',row.offset_days)
            if type(offset) is not int or abs(offset)>36500:raise ProjectError('相对天数须为-36500至36500的整数',status=400)
            new_offset=offset;new_due=None
        changed=old_basis!=(mode,new_due,new_offset)
        if eid and row.status=='done' and status=='done' and changed:raise ProjectError('已完成计划须先重开，再调整计划日期',status=400)
        row.schedule_mode=mode;row.due_on=new_due;row.offset_days=new_offset
        if changed or (row.status=='done' and status=='planned'):row.frozen_due_on=None
    if status not in ('planned','done','cancelled'):raise ProjectError('记录状态无效',status=400)
    actual=core.day(data.get('actual_on',row.actual_on.isoformat() if row.actual_on else None))
    if status=='done':
        if actual is None:raise ProjectError('完成记录需填写实际日期',status=400)
        if actual>today(project):raise ProjectError('实际发生日期不能晚于今天',status=400)
        row.frozen_due_on=due(row,project);row.accepted_by=user.id
    elif actual is not None:raise ProjectError('未完成记录不能填写实际日期',status=400)
    row.status=status;row.actual_on=actual
    if source=='event':
        kind=data.get('kind',row.kind or 'event')
        if kind not in ('event','meeting','record','outcome'):raise ProjectError('事件类型无效',status=400)
        row.kind=kind
    db.session.add(row);db.session.flush();project.version+=1
    after=item(row,project,source)
    change=core.event(project,user,source+':save',reason,before,after)
    from .records import notify
    notify(project,user,change)
    return core.save_command(project,user,cid,digest,{'item':after,'project':core.serialize(project,user)})

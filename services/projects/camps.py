"""项目参营与人员编排；本期关系不拥有长期团队。"""
from datetime import datetime
from exts import db
from models import (Project,ProjectCampBinding,ProjectCampParticipant,ProjectLegacyMap,ProjectAssessmentLink,
                    CampSession,CampUnit,CampMember,CampGroup,GroupMembership,UserModel)
from services.camp_groups.projects import ensure_camp_participant
from . import core,ProjectError


def staff(user,sid):
    from blueprints.camp_staff import has_camp_access
    return has_camp_access(user,sid,'member.manage')


def ready(binding):
    row=db.session.get(ProjectLegacyMap,binding.legacy_unit_id) if binding.legacy_unit_id else None
    return not row or row.write_model=='project'


def get_binding(pid,bid,user,write=False):
    row=ProjectCampBinding.query.filter_by(id=bid).populate_existing().with_for_update().first() if write else db.session.get(ProjectCampBinding,bid)
    if row is None or row.project_id!=pid:raise ProjectError('本期关联不存在',status=404)
    camp=CampSession.query.filter_by(id=row.camp_session_id).populate_existing().with_for_update().first() if write else db.session.get(CampSession,row.camp_session_id)
    project=db.session.get(Project,pid)
    if not camp or not project:raise ProjectError('关联对象不存在',status=404)
    if not ready(row):raise ProjectError('项目迁移尚未切换，请继续使用原营期入口',machine='PROJECT_MIGRATION_PENDING')
    if not core.role(project,user) and not staff(user,camp.id):
        from models import ProjectBindingSnapshot
        frozen=db.session.get(ProjectBindingSnapshot,bid)
        history=(frozen.snapshot.get('participation_history',frozen.snapshot.get('participants',[])) if frozen and row.status=='ended' else [])
        if not any(p['user_id']==user.id for p in history) or not CampMember.query.filter_by(camp_session_id=camp.id,user_id=user.id,status='active').first():raise ProjectError('无权访问本期项目',status=403)
    if write and (row.status!='active' or camp.status in ('archived','deleted') or project.status!='active'):
        raise ProjectError('本期关联或项目只读',machine='PROJECT_BINDING_READ_ONLY')
    return row,camp,project


def summary(binding,user):
    camp=db.session.get(CampSession,binding.camp_session_id)
    result={'id':binding.id,'project_id':binding.project_id,'camp_id':binding.camp_session_id,
            'camp_name':camp.name,'camp_status':camp.status,'status':binding.status,
            'unit_id':binding.legacy_unit_id,'version':binding.version,'leader_user_id':binding.leader_user_id,
            'can_manage':staff(user,camp.id),'participants':[{'user_id':r.user_id,'name':db.session.get(UserModel,r.user_id).username,'role':'leader' if r.user_id==binding.leader_user_id else 'member'} for r in ProjectCampParticipant.query.filter_by(binding_id=binding.id,status='active').all()]}
    if binding.status=='ended':
        from models import ProjectBindingSnapshot
        frozen=db.session.get(ProjectBindingSnapshot,binding.id)
        if frozen:result['participants']=frozen.snapshot.get('participants',[])
    allowed=staff(user,camp.id) or core.role(db.session.get(Project,binding.project_id),user) in ('leader','admin')
    result['requested_users']=[{'user_id':uid,'name':db.session.get(UserModel,uid).username} for uid in (binding.requested_users or [])] if allowed else []
    # 迁入关系没有人员调整申请；真实申请不允许空名单。
    result['change_pending']=binding.status=='active' and bool(binding.requested_users) and sorted(binding.requested_users)!=sorted(p['user_id'] for p in result['participants'])
    return result


def context_summary(binding,user,project,camp,*,camp_view=False):
    """工作台只读能力投影；长期角色与本期角色分别返回，写端点仍独立鉴权。"""
    from blueprints.camp_staff import has_camp_access
    from services.camp_groups.projects import unit_role
    from .legacy import historical_reader

    result=summary(binding,user)
    unit=db.session.get(CampUnit,binding.legacy_unit_id) if binding.legacy_unit_id else None
    long_role=core.role(project,user)
    period_role=unit_role(unit,user) if unit else None
    participant=ProjectCampParticipant.query.filter_by(binding_id=binding.id,user_id=user.id,status='active').first()
    active_member=CampMember.query.filter_by(camp_session_id=camp.id,user_id=user.id,status='active').first()
    is_participant=bool(binding.status=='active' and participant and active_member and long_role and period_role)
    period_open=binding.status=='active' and project.status=='active' and camp.status not in ('archived','deleted')
    historical=bool(unit and binding.status=='ended' and (
        historical_reader(unit.id,user) or (long_role and binding.leader_user_id==user.id)))
    result.update(
        my_role=period_role if is_participant else None,
        is_participant=is_participant,
        can_read_work=bool(unit and binding.status in ('active','ended') and (
            period_role or historical or has_camp_access(user,camp.id,'project.read_all'))),
        can_write_work=bool(period_open and unit and unit.status=='active' and period_role and not camp_view),
        can_manage_participants=bool(result['can_manage'] and project.status=='active'
            and binding.status in ('requested','active') and camp.status not in ('archived','deleted')),
        can_request_participants=bool(period_open and long_role in ('leader','admin') and not camp_view),
    )
    return result


def project_view(project,user,binding=None,camp_view=False):
    result=core.serialize(project,user)
    if binding and (camp_view or not core.role(project,user)):
        from models import ProjectBindingSnapshot
        frozen=db.session.get(ProjectBindingSnapshot,binding.id)
        if binding.status=='ended' and frozen:
            result.update(frozen.snapshot['project'])
            result['member_count']=len(frozen.snapshot.get('participants',[]))
        result['can_write']=False;result['can_manage']=False
        result.pop('research_summary',None)
    return result


def request_binding(user,pid,data):
    project=core.lock(core.get_project(pid,user,manage=True,write=True),user)
    core.get_project(pid,user,manage=True,write=True,fresh=True)
    cid,digest,replay=core.command(project,user,data,'camp:request')
    if replay:return replay
    sid=core.number(data.get('camp_id'),'营期ID');camp=CampSession.query.filter_by(id=sid).populate_existing().with_for_update().first()
    if not camp or camp.category!='project' or camp.status in ('archived','deleted','draft'):
        raise ProjectError('该营期当前不能加入项目',status=400)
    if not staff(user,sid) and camp.status not in ('upcoming','selecting'):
        raise ProjectError('营期已开营，新增关联需管理员处理',status=403)
    if not staff(user,sid) and not camp.is_featured and not CampMember.query.filter_by(camp_session_id=sid,user_id=user.id,status='active').first():
        raise ProjectError('该营期当前未向你开放招募',status=403)
    group=db.session.get(CampGroup,project.group_id)
    users=data.get('user_ids')
    if not isinstance(users,list) or not users or len(users)>200:raise ProjectError('请选择本期参与人员（最多200人）',status=400)
    selected=sorted({core.number(uid) for uid in users})
    if group.owner_user_id not in selected:raise ProjectError('本期名单须包含项目负责人',status=400)
    for uid in selected:
        target=db.session.get(UserModel,uid)
        if not target or core.role(project,target) not in ('leader','member'):raise ProjectError('本期人员须为有效长期项目成员',status=400)
    row=ProjectCampBinding.query.filter_by(project_id=pid,camp_session_id=sid).first()
    if row and row.status=='ended':raise ProjectError('本期关联已结束，请选择新的营期，已冻结的记录不能覆盖',machine='PROJECT_BINDING_READ_ONLY')
    if row and row.status in ('requested','active'):raise ProjectError('该项目已有本期关联或待审申请')
    if row is None:row=ProjectCampBinding(project_id=pid,camp_session_id=sid,leader_user_id=group.owner_user_id,requested_by=user.id)
    row.status='requested';row.requested_users=selected;row.requested_by=user.id;row.leader_user_id=group.owner_user_id
    db.session.add(row);db.session.flush();project.version+=1
    core.event(project,user,'camp:request',core.text(data.get('reason'),'申请原因',500),after={'binding_id':row.id,'camp_id':sid,'user_ids':selected})
    return core.save_command(project,user,cid,digest,{'binding':summary(row,user),'project':core.serialize(project,user)})


def operate(user,pid,bid,data):
    binding,camp,project=get_binding(pid,bid,user)
    if not staff(user,camp.id):raise ProjectError('需要本营人员管理权限',status=403)
    if camp.status in ('archived','deleted'):raise ProjectError('营期已结束，只读')
    users=data.get('user_ids',binding.requested_users or [])
    ids=sorted({core.number(uid) for uid in users}) if isinstance(users,list) else []
    if len(ids)>200:raise ProjectError('本期名单上限200人',status=400)
    project=core.lock(project,user,additional_user_ids=ids)
    binding=ProjectCampBinding.query.filter_by(id=bid).populate_existing().with_for_update().one()
    camp=CampSession.query.filter_by(id=camp.id).populate_existing().with_for_update().one()
    if camp.status in ('archived','deleted'):raise ProjectError('营期已结束，只读')
    cid,digest,replay=core.command(project,user,data,'camp:'+str(bid))
    if replay:return replay
    if type(data.get('expected_binding_version')) is not int or data['expected_binding_version']!=binding.version:raise ProjectError('本期关联已变化，请刷新',machine='PROJECT_BINDING_VERSION_CONFLICT')
    reason=core.text(data.get('reason'),'调整原因',500);action=data.get('action')
    if action in ('approve','participants'):
        if project.status!='active':raise ProjectError('项目当前不可参营')
        if (action=='approve' and binding.status!='requested') or (action=='participants' and binding.status!='active'):raise ProjectError('本期关联状态不允许此操作')
        leader=core.number(data.get('leader_user_id',binding.leader_user_id),'负责人ID')
        if leader not in ids:raise ProjectError('本期负责人须在参与名单中',status=400)
        for uid in ids:
            target=db.session.get(UserModel,uid)
            if target is None or core.role(project,target) not in ('leader','member'):raise ProjectError('人员不是有效长期项目成员',status=400)
            if uid==leader:
                from services.groups.identity import require_appointment
                from services.groups import GovernanceDecision
                require_appointment(target,GovernanceDecision('project:camp_leader','project_join',pid))
            member=ensure_camp_participant(camp.id,uid,user.id,reason)
            long_member=GroupMembership.query.filter_by(group_id=project.group_id,user_id=uid,status='active').one()
            part=ProjectCampParticipant.query.filter_by(binding_id=bid,user_id=uid).first()
            if part is None:part=ProjectCampParticipant(binding_id=bid,user_id=uid,camp_member_id=member.id)
            part.project_membership_id=long_member.id;part.camp_member_id=member.id;part.status='active';part.ended_at=None
            db.session.add(part)
        for part in ProjectCampParticipant.query.filter_by(binding_id=bid,status='active').all():
            if part.user_id not in ids:part.status='ended';part.ended_at=datetime.now()
        binding.leader_user_id=leader;binding.status='active';binding.approved_by=user.id;binding.started_at=binding.started_at or datetime.now();binding.ended_at=None
        binding.requested_users=ids
        if not binding.legacy_unit_id:
            # unit只提供旧考核与组会的来源定位，不另建长期团队。
            unit=CampUnit(camp_session_id=camp.id,unit_type='project',name=project.title[:70]+' · '+project.code,owner_user_id=leader,status='active',visibility='camp')
            db.session.add(unit);db.session.flush();binding.legacy_unit_id=unit.id
            db.session.add(ProjectLegacyMap(unit_id=unit.id,project_id=pid,binding_id=bid,write_model='project',source_digest=core._digest(['new-binding',bid])))
    elif action in ('end','reject'):
        if action=='reject' and binding.status!='requested':raise ProjectError('只能拒绝待审申请')
        if action=='end':
            if binding.status!='active':raise ProjectError('只能结束进行中的本期关联')
            from .records import freeze
            freeze(binding,user.id)
        binding.status='ended' if action=='end' else 'rejected';binding.ended_at=datetime.now()
        for part in ProjectCampParticipant.query.filter_by(binding_id=bid,status='active').all():part.status='ended';part.ended_at=datetime.now()
    else:raise ProjectError('关联操作无效',status=400)
    binding.version+=1;project.version+=1
    core.event(project,user,'camp:'+action,reason,after={'binding_id':bid,'user_ids':ids,'status':binding.status})
    db.session.flush()
    return core.save_command(project,user,cid,digest,{'binding':summary(binding,user),'project':core.serialize(project,user)})


def end_project_participations(project,uid,user,reason):
    parts=ProjectCampParticipant.query.join(ProjectCampBinding).filter(ProjectCampBinding.project_id==project.id,ProjectCampParticipant.user_id==uid,ProjectCampParticipant.status=='active').all()
    for part in parts:
        binding=db.session.get(ProjectCampBinding,part.binding_id)
        if not ready(binding):continue
        if binding.status=='active' and binding.leader_user_id==uid:raise ProjectError('请先交接该人员担任的本期负责人',machine='PROJECT_BINDING_HANDOFF_REQUIRED')
    for part in parts:
        if ready(db.session.get(ProjectCampBinding,part.binding_id)):part.status='ended';part.ended_at=datetime.now()


def lock_camp_projects(sid,actor_id):
    camp=db.session.get(CampSession,sid)
    if not camp or camp.category!='project':return None
    bindings=ProjectCampBinding.query.filter_by(camp_session_id=sid).all()
    units=CampUnit.query.filter_by(camp_session_id=sid,unit_type='project').all()
    members=CampMember.query.filter_by(camp_session_id=sid).all()
    pids=[r.project_id for r in bindings]
    projects=Project.query.filter(Project.id.in_(pids)).all()
    gids={p.group_id for p in projects}|{u.group_id for u in units if u.group_id}
    groups=CampGroup.query.filter(CampGroup.id.in_(gids)).all()
    versions={g.id:g.version for g in groups}
    ids={actor_id}|{m.user_id for m in members}|{g.owner_user_id for g in groups}|{u.owner_user_id for u in units}
    ids.update(m.user_id for m in GroupMembership.query.filter(GroupMembership.group_id.in_(gids),GroupMembership.status=='active'))
    core.lock_accounts(ids)
    locked=CampGroup.query.filter(CampGroup.id.in_(gids)).order_by(CampGroup.id).populate_existing().with_for_update().all()
    if any(g.version!=versions[g.id] for g in locked):raise ProjectError('项目名单已变化，请刷新后结营',machine='PROJECT_VERSION_CONFLICT')
    Project.query.filter(Project.id.in_(pids)).order_by(Project.id).populate_existing().with_for_update().all()
    ProjectCampBinding.query.filter_by(camp_session_id=sid).order_by(ProjectCampBinding.id).populate_existing().with_for_update().all()
    CampUnit.query.filter(CampUnit.id.in_([u.id for u in units])).order_by(CampUnit.id).populate_existing().with_for_update().all()
    return {'bindings':{b.id for b in bindings},'units':{u.id for u in units},'members':{m.id for m in members}}


def check_camp_catalog(sid,expected):
    if expected is None:return
    actual={key:{r.id for r in query.populate_existing().with_for_update()} for key,query in (
        ('bindings',ProjectCampBinding.query.filter_by(camp_session_id=sid)),
        ('units',CampUnit.query.filter_by(camp_session_id=sid,unit_type='project')),
        ('members',CampMember.query.filter_by(camp_session_id=sid)))}
    if actual!=expected:raise ProjectError('参营项目或人员刚刚变化，请刷新后结营',machine='PROJECT_VERSION_CONFLICT')


def close_camp_bindings(sid,actor_id):
    actor=db.session.get(UserModel,actor_id)
    for binding in ProjectCampBinding.query.filter(ProjectCampBinding.camp_session_id==sid,ProjectCampBinding.status.in_(('active','requested'))):
        if not ready(binding):continue
        if binding.status=='active':
            from .records import freeze
            freeze(binding,actor_id)
            binding.status='ended'
        else:binding.status='rejected'
        binding.ended_at=datetime.now();binding.version+=1
        for part in ProjectCampParticipant.query.filter_by(binding_id=binding.id,status='active'):part.status='ended';part.ended_at=datetime.now()
        core.event(db.session.get(Project,binding.project_id),actor,'camp:closed','本期营结营，长期项目保留',after={'binding_id':binding.id,'camp_id':sid})


def request_participants(user,pid,bid,data):
    project=core.lock(core.get_project(pid,user,manage=True,write=True),user)
    binding,_,_=get_binding(pid,bid,user,write=True)
    cid,digest,replay=core.command(project,user,data,'camp:participants-request:'+str(bid))
    if replay:return replay
    if data.get('expected_binding_version')!=binding.version:raise ProjectError('本期名单已变化，请刷新')
    ids=data.get('user_ids')
    if not isinstance(ids,list) or not 1<=len(ids)<=200:raise ProjectError('请选择本期名单',status=400)
    ids=sorted({core.number(uid) for uid in ids})
    if binding.leader_user_id not in ids:raise ProjectError('本期负责人须在申请名单中',status=400)
    for uid in ids:
        user_row=db.session.get(UserModel,uid)
        if not user_row or core.role(project,user_row) not in ('leader','member'):raise ProjectError('申请人员须在长期团队中',status=400)
    binding.requested_users=ids;binding.version+=1;project.version+=1
    core.event(project,user,'camp:participants-request',core.text(data.get('reason'),'变更原因',500),after={'binding_id':bid,'user_ids':ids})
    return core.save_command(project,user,cid,digest,{'binding':summary(binding,user),'project':core.serialize(project,user)})

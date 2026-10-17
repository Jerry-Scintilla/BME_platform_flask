"""旧单元到独立项目的显式映射与唯一写入护栏。"""
from datetime import datetime
from flask import jsonify,request
from exts import db
from models import (Project,ProjectLegacyMap,ProjectCampBinding,ProjectCampParticipant,ProjectAssessmentLink,
                    ProjectProfile,CampUnit,CampSession,CampMember,CampGroup,CampMilestone,CampSubmissionVersion,
                    GroupMembership,GroupRoleAssignment,UserModel,member_history_scope)
from . import core,ProjectError


def mapping(uid):
    unit=db.session.get(CampUnit,uid)
    row=unit.project_link if unit else None
    return row if row and row.write_model=='project' else None


def binding_for(uid):
    row=mapping(uid)
    return db.session.get(ProjectCampBinding,row.binding_id) if row else None


def preview(uid):
    unit=db.session.get(CampUnit,uid)
    if unit is None or unit.unit_type!='project':raise ProjectError('项目单元不存在',status=404)
    group=db.session.get(CampGroup,unit.group_id) if unit.group_id else None
    members=GroupMembership.query.filter_by(group_id=unit.group_id).order_by(GroupMembership.id).all() if group else []
    roles=GroupRoleAssignment.query.filter_by(group_id=unit.group_id).order_by(GroupRoleAssignment.id).all() if group else []
    profile=ProjectProfile.query.filter_by(unit_id=uid).first()
    raw={'unit_id':uid,'camp_id':unit.camp_session_id,'legacy_name':unit.legacy_name,'group_id':unit.group_id,
         'camp_status':db.session.get(CampSession,unit.camp_session_id).status,
         'profile':{k:getattr(profile,k) for k in ('background','goal','required_abilities','recruit_note')} if profile else None,
         'group_version':group.version if group else None,'owner_id':group.owner_user_id if group else unit.legacy_owner_user_id,
         'status':unit.status,'members':[{'id':m.id,'user_id':m.user_id,'person_id':m.person_id,'status':m.status,'started_at':m.started_at.isoformat(),'ended_at':m.ended_at.isoformat() if m.ended_at else None} for m in members],
         'roles':[(r.id,r.role,r.active_slot,r.expires_at.isoformat() if r.expires_at else None) for r in roles]}
    raw['source_digest']=core._digest(raw)
    raw['milestone_count']=CampMilestone.query.filter_by(unit_id=uid).count()
    raw['submission_count']=CampSubmissionVersion.query.join(CampMilestone).filter(CampMilestone.unit_id==uid).count()
    raw['ready']=bool(group and group.status=='active' and raw['camp_status'] not in ('archived','deleted'))
    return raw


def migrate(user,uid,data):
    if not user.is_admin():raise ProjectError('迁移仅限平台管理员',status=403)
    source=preview(uid)
    if not source['ready'] and not (data.get('action')=='cancel' and source['group_id']):raise ProjectError('请先处理旧组织映射；已结营历史继续从原营期档案查看',machine='PROJECT_LEGACY_MAPPING_REQUIRED')
    core.GroupService._lock_users(user.id,source['owner_id'])
    group=CampGroup.query.filter_by(id=source['group_id']).populate_existing().with_for_update().one()
    unit=CampUnit.query.filter_by(id=uid).populate_existing().with_for_update().one()
    camp=CampSession.query.filter_by(id=unit.camp_session_id).populate_existing().with_for_update().one()
    source=preview(uid)
    if data.get('source_digest')!=source['source_digest']:raise ProjectError('源项目已变化，请重新预览',machine='PROJECT_MIGRATION_STALE')
    action=data.get('action');reason=core.text(data.get('reason'),'迁移原因',500)
    row=db.session.get(ProjectLegacyMap,uid)
    if action=='prepare':
        if row and row.write_model!='legacy':return {'project_id':row.project_id,'binding_id':row.binding_id,'write_model':row.write_model}
        target=data.get('project_id')
        if row:
            target=target or row.project_id
            old_bid=row.binding_id
            ProjectAssessmentLink.query.filter_by(binding_id=old_bid).delete()
            ProjectCampParticipant.query.filter_by(binding_id=old_bid).delete()
            old_binding=db.session.get(ProjectCampBinding,old_bid)
            db.session.delete(row);db.session.flush();db.session.delete(old_binding);db.session.flush()
        project=db.session.get(Project,core.number(target)) if target else None
        if target and project is None:raise ProjectError('指定长期项目不存在',status=404)
        if not project:
            if Project.query.filter_by(group_id=group.id).first():raise ProjectError('该核心组已关联长期项目，请显式指定项目')
            profile=ProjectProfile.query.filter_by(unit_id=uid).first()
            import uuid
            project=Project(code='PJ-'+uuid.uuid4().hex[:16].upper(),group_id=group.id,title=group.name,
                            background=profile.background if profile else None,goal=profile.goal if profile else None,
                            status='paused' if unit.status=='paused' else 'active',created_by=user.id,migration_ready=False)
            db.session.add(project);db.session.flush()
        existing=ProjectCampBinding.query.filter_by(project_id=project.id,camp_session_id=camp.id).first()
        if existing:raise ProjectError('该项目已有本营关联，不能按名字自动合并')
        binding=ProjectCampBinding(project_id=project.id,camp_session_id=camp.id,legacy_unit_id=uid,
                    leader_user_id=source['owner_id'],status='ended' if camp.status in ('archived','deleted') else 'active',requested_by=user.id,approved_by=user.id,requested_users=[],started_at=unit.created_at)
        db.session.add(binding);db.session.flush()
        for member in source['members']:
            with member_history_scope():camp_member=CampMember.query.filter_by(camp_session_id=camp.id,user_id=member['user_id']).first()
            if not camp_member:raise ProjectError('旧项目存在无入营记录人员，需人工处理',machine='PROJECT_MIGRATION_CONFLICT')
            canonical=GroupMembership.query.filter_by(group_id=project.group_id,user_id=member['user_id']).first()
            active=member['status']=='active' and binding.status=='active' and camp_member.status=='active'
            if active and (canonical is None or canonical.status!='active'):raise ProjectError('当前参营人员不在指定长期团队，请先显式处理成员归属',machine='PROJECT_MIGRATION_CONFLICT')
            db.session.add(ProjectCampParticipant(binding_id=binding.id,user_id=member['user_id'],camp_member_id=camp_member.id,
                project_membership_id=canonical.id if canonical else None,status='active' if active else 'ended',
                started_at=datetime.fromisoformat(member['started_at']),ended_at=datetime.fromisoformat(member['ended_at']) if member['ended_at'] else None))
        row=ProjectLegacyMap(unit_id=uid,project_id=project.id,binding_id=binding.id,write_model='shadow',source_digest=source['source_digest'])
        db.session.add(row)
        for milestone in CampMilestone.query.filter_by(unit_id=uid):db.session.add(ProjectAssessmentLink(camp_milestone_id=milestone.id,binding_id=binding.id))
        core.event(project,user,'migration:prepare',reason,after={'unit_id':uid,'source_digest':source['source_digest']})
    elif action=='switch':
        if not row:raise ProjectError('请先准备迁移映射')
        if row.source_digest!=source['source_digest']:raise ProjectError('准备后源数据已变化，需撤销未切换映射并重新准备',machine='PROJECT_MIGRATION_STALE')
        if row.write_model!='project':
            project=db.session.get(Project,row.project_id)
            for part in ProjectCampParticipant.query.filter_by(binding_id=row.binding_id,status='active'):
                canonical=db.session.get(GroupMembership,part.project_membership_id) if part.project_membership_id else None
                if not canonical or canonical.group_id!=project.group_id or canonical.user_id!=part.user_id or canonical.status!='active':raise ProjectError('准备期间长期团队已变化，请重新核对参营名单',machine='PROJECT_MIGRATION_STALE')
            row.write_model='project'
            project=db.session.get(Project,row.project_id);project.version+=1;project.migration_ready=True
            core.event(project,user,'migration:switch',reason,after={'unit_id':uid})
    elif action=='cancel':
        if not row or row.write_model=='project':raise ProjectError('只能撤销尚未切换的准备映射')
        row.write_model='legacy'
        project=db.session.get(Project,row.project_id)
        core.event(project,user,'migration:cancel',reason,after={'unit_id':uid})
        return {'project_id':project.id,'write_model':'cancelled'}
    else:raise ProjectError('迁移操作无效',status=400)
    db.session.flush()
    return {'project_id':row.project_id,'binding_id':row.binding_id,'write_model':row.write_model}


def assert_legacy_team_write(uid):
    row=ProjectLegacyMap.query.filter_by(unit_id=uid).populate_existing().with_for_update().first()
    if row and row.write_model=='project':raise ProjectError('长期项目已独立管理，请从项目工作区调整长期团队或本期人员',machine='PROJECT_CORE_WRITE_REQUIRED')


def historical_reader(uid,user,submission=None):
    binding=binding_for(uid)
    if not binding or binding.status!='ended':return False
    from models import ProjectBindingSnapshot
    snapshot=db.session.get(ProjectBindingSnapshot,binding.id)
    if not snapshot or not CampMember.query.filter_by(camp_session_id=binding.camp_session_id,user_id=user.id,status='active').first():return False
    people={p['user_id'] for p in snapshot.snapshot.get('participation_history',snapshot.snapshot.get('participants',[]))}
    if user.id not in people:return False
    if submission:
        return any(s['id']==submission.id for n in snapshot.snapshot.get('assessment_nodes',[]) for s in n['submissions']) and (binding.leader_user_id==user.id or submission.submitted_by==user.id)
    return True


def install(app):
    @app.before_request
    def protect_legacy_writes():
        if request.method in ('GET','HEAD','OPTIONS') or request.blueprint not in ('camp_project','camp_delivery','camp_meeting'):return
        from flask_jwt_extended import verify_jwt_in_request
        verify_jwt_in_request()
        args=request.view_args or {};uid=args.get('uid')
        if not uid and args.get('mid') and request.blueprint=='camp_delivery':
            node=db.session.get(CampMilestone,args['mid']);uid=node.unit_id if node else None
        if not uid and request.blueprint=='camp_meeting':
            from models import CampMeeting,CampMeetingTask,CampMeetingAttachment,CampMeetingTaskAttachment,CampMeetingTaskSubmission
            mid=args.get('mid')
            if not mid and args.get('tid'):
                task=db.session.get(CampMeetingTask,args['tid']);mid=task.meeting_id if task else None
            if not mid and args.get('aid'):
                if request.endpoint=='camp_meeting.meeting_attachment_delete':
                    attachment=db.session.get(CampMeetingAttachment,args['aid']);mid=attachment.meeting_id if attachment else None
                elif request.endpoint=='camp_meeting.meeting_task_attachment_delete':
                    attachment=db.session.get(CampMeetingTaskAttachment,args['aid'])
                    submission=db.session.get(CampMeetingTaskSubmission,attachment.submission_id) if attachment else None
                    task=db.session.get(CampMeetingTask,submission.task_id) if submission else None;mid=task.meeting_id if task else None
            meeting=db.session.get(CampMeeting,mid) if mid else None;uid=meeting.unit_id if meeting and meeting.scope=='unit' else None
        if not uid and request.endpoint=='camp_delivery.submission_review':
            submission=db.session.get(CampSubmissionVersion,args.get('sid'));node=db.session.get(CampMilestone,submission.milestone_id) if submission else None;uid=node.unit_id if node else None
        if not uid and args.get('oid') and request.blueprint=='camp_delivery':
            from models import CampOutcome
            outcome=db.session.get(CampOutcome,args['oid']);uid=outcome.unit_id if outcome else None
        if not uid and args.get('aid') and request.blueprint=='camp_project':
            from models import CampUnitActivity
            activity=db.session.get(CampUnitActivity,args['aid']);uid=activity.unit_id if activity else None
        if not uid:return
        row=mapping(uid)
        if not row:return
        from blueprints import _current_user
        from services.camp_groups.projects import unit_role
        from .camps import staff
        unit=db.session.get(CampUnit,uid);actor=_current_user()
        if not actor or (not unit_role(unit,actor) and not historical_reader(uid,actor) and not staff(actor,unit.camp_session_id)):return
        project=db.session.get(Project,row.project_id);binding=db.session.get(ProjectCampBinding,row.binding_id)
        blocked=request.endpoint in {'camp_project.member_selection','camp_project.member_end','camp_project.leader_change','camp_project.unit_status','camp_delivery.unit_template_create','camp_delivery.unit_template_update'}
        if blocked or project.status!='active' or binding.status!='active':
            return jsonify(code=409,machine='PROJECT_CORE_WRITE_REQUIRED',message='请在独立项目工作区操作；本期考核仅在有效关联期间可写'),409

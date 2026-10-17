"""独立项目API；旧营期接口及公开项目广场保留。"""
from flask import Blueprint, jsonify, request,Response,current_app
from flask_jwt_extended import jwt_required
from sqlalchemy.exc import IntegrityError
from exts import db
from models import Project, GroupMembership, UserModel
from . import _current_user
from services.projects import enabled, require_enabled
from services.projects import core
from services.projects import calendar
from services.projects import camps
from services.projects import records
from services.groups import GroupError

bp = Blueprint('project_core', __name__, url_prefix='/project-core')


@bp.before_request
def gate():
    if request.method != 'OPTIONS' and request.endpoint != 'project_core.status': require_enabled()


@bp.errorhandler(GroupError)
def domain_error(exc):
    db.session.rollback()
    return jsonify(code=exc.status, machine=exc.machine, message=str(exc)), exc.status


@bp.errorhandler(IntegrityError)
def conflict(_exc):
    db.session.rollback()
    return jsonify(code=409, machine='PROJECT_CONCURRENT_CONFLICT', message='数据已变化，请刷新后重试'), 409


def body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict): raise core.ProjectError('需要JSON对象', status=400)
    return data


def binding_arg():
    if 'binding' not in request.args:return None
    values=request.args.getlist('binding')
    if len(values)!=1 or not values[0].isdigit() or int(values[0])<1:raise core.ProjectError('营期关联参数无效',status=400)
    return int(values[0])


def result(data, *, commit=False):
    if commit: db.session.commit()
    return jsonify(code=200, **data)


@bp.get('/status')
@jwt_required()
def status():
    return result({'enabled':enabled()})


@bp.route('/projects', methods=['GET','POST'])
@jwt_required()
def project_list():
    user = _current_user()
    if request.method == 'POST': return result(core.create(user, body()), commit=True)
    q = Project.query
    if not user.is_admin():
        q = q.filter(Project.group_id.in_(db.session.query(GroupMembership.group_id).filter_by(user_id=user.id,status='active')))
    rows = q.order_by(Project.id.desc()).limit(100).all()
    return result({'projects':[core.serialize(p,user) for p in rows if core.role(p,user)]})


@bp.route('/projects/<int:pid>', methods=['GET','PATCH'])
@jwt_required()
def project_detail(pid):
    user = _current_user()
    if request.method == 'PATCH': return result(core.update(user,pid,body()),commit=True)
    return result({'project':core.serialize(core.get_project(pid,user),user)})


@bp.route('/projects/<int:pid>/members', methods=['GET','POST'])
@jwt_required()
def project_members(pid):
    user = _current_user()
    if request.method == 'POST': return result(core.change_member(user,pid,body()),commit=True)
    return result({'members':core.members(core.get_project(pid,user))})


@bp.get('/projects/<int:pid>/candidates')
@jwt_required()
def candidates(pid):
    core.get_project(pid,_current_user(),manage=True)
    keyword = (request.args.get('keyword') or '').strip()
    if len(keyword)<1: return result({'users':[]})
    escaped = keyword.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')
    users=UserModel.query.filter(UserModel.role!='super_admin',UserModel.status=='active',UserModel.lifecycle=='active',UserModel.username.like('%'+escaped+'%',escape='\\')).order_by(UserModel.id).limit(20).all()
    return result({'users':[{'user_id':u.id,'name':u.username} for u in users]})


@bp.route('/projects/<int:pid>/zero-date',methods=['PATCH'])
@jwt_required()
def zero_date(pid):
    return result(calendar.change_zero(_current_user(),pid,body()),commit=True)


@bp.post('/projects/<int:pid>/zero-date/preview')
@jwt_required()
def zero_preview(pid):
    project=core.get_project(pid,_current_user(),manage=True)
    return result(calendar.preview_zero(project,body().get('zero_date')))


@bp.get('/projects/<int:pid>/calendar')
@jwt_required()
def project_calendar(pid):
    user=_current_user();bid=binding_arg();binding=None
    if bid:binding,_,project=camps.get_binding(pid,bid,user)
    else:project=core.get_project(pid,user)
    calendar.bounds(request.args.get('from'),request.args.get('to'))
    camp_view=request.args.get('camp_view')=='1'
    if camp_view and (not binding or (not camps.staff(user,binding.camp_session_id) and core.role(project,user))):raise core.ProjectError('需要本期查看权限',status=403)
    data=calendar.list_items(project,request.args.get('from'),request.args.get('to')) if core.role(project,user) and not camp_view else {'items':[],'truncated':False}
    for entry in data['items']:entry['can_edit']=core.role(project,_current_user()) in ('leader','admin') and project.status=='active'
    start,end=request.args.get('from'),request.args.get('to')
    extra=records.extra_calendar(project,user,binding,start,end,camp_view)
    data['truncated']=data['truncated'] or len(extra)>500
    data['items'].extend(extra[:500])
    data['items'].sort(key=lambda r:(r['due_on'] or '9999',r['key']))
    return result(dict(data,project=camps.project_view(project,user,binding,camp_view)))


@bp.route('/projects/<int:pid>/schedule/<source>',methods=['POST'])
@bp.route('/projects/<int:pid>/schedule/<source>/<int:eid>',methods=['PATCH'])
@jwt_required()
def schedule_save(pid,source,eid=None):
    return result(calendar.save_item(_current_user(),pid,source,body(),eid),commit=True)


@bp.route('/projects/<int:pid>/camps',methods=['GET','POST'])
@jwt_required()
def project_camps(pid):
    user=_current_user()
    if request.method=='POST':return result(camps.request_binding(user,pid,body()),commit=True)
    core.get_project(pid,user)
    return result({'bindings':[camps.summary(b,user) for b in camps.ProjectCampBinding.query.filter_by(project_id=pid).all() if camps.ready(b)]})


@bp.post('/projects/<int:pid>/camps/<int:bid>')
@jwt_required()
def camp_operation(pid,bid):
    return result(camps.operate(_current_user(),pid,bid,body()),commit=True)


@bp.post('/projects/<int:pid>/camps/<int:bid>/participants-request')
@jwt_required()
def participants_request(pid,bid):
    return result(camps.request_participants(_current_user(),pid,bid,body()),commit=True)


@bp.get('/camps/available')
@jwt_required()
def available_camps():
    from models import CampSession,CampStaff,CampMember
    user=_current_user();q=CampSession.query.filter(CampSession.category=='project',CampSession.status.notin_(('draft','archived','deleted')))
    if not user.is_admin():q=q.filter(db.or_(CampSession.is_featured.is_(True),CampSession.id.in_(db.session.query(CampStaff.camp_session_id).filter_by(user_id=user.id,status='active')),CampSession.id.in_(db.session.query(CampMember.camp_session_id).filter_by(user_id=user.id,status='active'))))
    return result({'camps':[{'id':c.id,'name':c.name,'status':c.status} for c in q.order_by(CampSession.id.desc()).limit(100)]})


@bp.get('/camps/<int:sid>/projects')
@jwt_required()
def camp_projects(sid):
    user=_current_user()
    if not camps.staff(user,sid):raise core.ProjectError('需要本营管理权限',status=403)
    rows=camps.ProjectCampBinding.query.filter_by(camp_session_id=sid).all()
    return result({'projects':[{'project':core.serialize(db.session.get(Project,b.project_id),user),'binding':camps.summary(b,user)} for b in rows if camps.ready(b)]})


@bp.get('/projects/<int:pid>/context')
@jwt_required()
def project_context(pid):
    user=_current_user();bid=binding_arg()
    if bid:
        binding,camp,project=camps.get_binding(pid,bid,user)
        projected=camps.project_view(project,user,binding,request.args.get('view')=='camp')
        return result({'project':projected,'binding':camps.summary(binding,user)})
    return result({'project':core.serialize(core.get_project(pid,user),user),'binding':None})


@bp.get('/legacy/<int:uid>/preview')
@jwt_required()
def legacy_preview(uid):
    if not _current_user().is_admin():raise core.ProjectError('仅管理员可预览迁移',status=403)
    from services.projects.legacy import preview
    return result({'preview':preview(uid)})


@bp.post('/legacy/<int:uid>/migrate')
@jwt_required()
def legacy_migrate(uid):
    from services.projects.legacy import migrate
    return result(migrate(_current_user(),uid,body()),commit=True)


@bp.get('/legacy/<int:uid>/resolve')
@jwt_required()
def legacy_resolve(uid):
    from services.projects.legacy import mapping
    from models import CampUnit
    from services.camp_groups.projects import unit_role
    unit=db.session.get(CampUnit,uid)
    if not unit:raise core.ProjectError('项目单元不存在',status=404)
    if not unit_role(unit,_current_user()) and not camps.staff(_current_user(),unit.camp_session_id):raise core.ProjectError('无权访问本期项目',status=403)
    row=mapping(uid)
    return result({'mapped':bool(row),'project_id':row.project_id if row else None,'binding_id':row.binding_id if row else None})


@bp.route('/projects/<int:pid>/records',methods=['GET','POST'])
@bp.route('/projects/<int:pid>/records/<int:rid>',methods=['PATCH'])
@jwt_required()
def project_records(pid,rid=None):
    user=_current_user()
    if request.method=='GET':
        bid=binding_arg();binding=None
        if bid:binding,_,project=camps.get_binding(pid,bid,user)
        else:project=core.get_project(pid,user)
        record_id=core.number(request.args.get('record_id',type=int),'记录ID') if 'record_id' in request.args else None
        return result({'records':records.read(project,user,binding,force_binding=request.args.get('camp_view')=='1',record_id=record_id),'project':camps.project_view(project,user,binding,request.args.get('camp_view')=='1')})
    uploaded=[]
    try:
        if request.content_length and request.content_length>49*1024*1024:raise core.ProjectError('请求总量不能超过49MB',status=413)
        if request.mimetype=='multipart/form-data':
            import json
            try:data=json.loads(request.form.get('metadata',''))
            except (ValueError,TypeError):raise core.ProjectError('记录内容格式无效',status=400)
            if not isinstance(data,dict):raise core.ProjectError('记录内容格式无效',status=400)
            files=request.files.getlist('files')
        else:data=body();files=[]
        response=records.save(user,pid,data,files,uploaded,rid)
        db.session.commit();return result(response)
    except Exception as exc:
        db.session.rollback()
        for key in uploaded:
            try:records.storage.remove_object(key)
            except Exception:current_app.logger.exception('项目记录失败后的附件清理失败')
        if isinstance(exc,(GroupError,IntegrityError)):raise
        current_app.logger.exception('项目记录保存失败')
        return jsonify(code=500,message='记录保存失败，请稍后重试'),500


@bp.post('/projects/<int:pid>/records/<int:rid>/verify')
@jwt_required()
def verify_record(pid,rid):
    return result(records.verify(_current_user(),pid,rid,body()),commit=True)


@bp.get('/files/<int:aid>/token')
@jwt_required()
def file_token(aid):
    from blueprints.media_sign import media_signed_url,MEDIA_TOKEN_TTL
    bid=binding_arg();records.file_access(aid,_current_user(),bid)
    url=media_signed_url('project_file',aid,_current_user().id,path=f'/project-core/files/{aid}')
    if bid:url+='&binding='+str(bid)
    return result({'url':url,'expires_in':MEDIA_TOKEN_TTL})


@bp.get('/files/<int:aid>')
def file_download(aid):
    from blueprints.media_sign import resolve_media_request
    from urllib.parse import quote
    user,error=resolve_media_request('project_file',aid)
    if error:return error
    attachment=records.file_access(aid,user,binding_arg())
    try:content=records.storage.get_object(attachment.object_key)
    except Exception:return jsonify(code=503,message='附件存储暂不可用'),503
    response=Response(content,mimetype=attachment.content_type or 'application/octet-stream')
    response.headers['Content-Disposition']="attachment; filename*=UTF-8''"+quote(attachment.filename)
    response.headers['X-Content-Type-Options']='nosniff'
    return response


@bp.get('/changes/<int:eid>/target')
@jwt_required()
def change_target(eid):
    from models import ProjectChange,ProjectRecord
    change=db.session.get(ProjectChange,eid)
    if not change:raise core.ProjectError('项目通知不存在',status=404)
    core.get_project(change.project_id,_current_user())
    data=change.after or {};record=data.get('record');entry=data.get('key')
    return result({'project_id':change.project_id,'tab':'records' if record else 'calendar','record_id':record.get('id') if record else None,'entry':entry,'month':(data.get('due_on') or data.get('actual_on') or '')[:7] or None})


@bp.post('/projects/<int:pid>/publish')
@jwt_required()
def publish_project(pid):
    from models import ShowcaseProject,CampGroup
    user=_current_user();project=core.lock(core.get_project(pid,user,manage=True),user)
    if project.status not in ('active','completed'):raise core.ProjectError('当前项目状态不允许发布')
    data=body();cid,digest,replay=core.command(project,user,data,'publish')
    if replay:return result(replay)
    if data.get('confirm_public') is not True:raise core.ProjectError('请确认公开范围',status=400)
    from services.identity.gates import verify_reject_reason
    why=verify_reject_reason(user,'publish',scope_id=pid)
    if why:raise core.ProjectError(why,status=403)
    row=ShowcaseProject.query.filter_by(source='project',source_ref=pid).first()
    if not row:row=ShowcaseProject(source='project',source_ref=pid,owner_user_id=db.session.get(CampGroup,project.group_id).owner_user_id,title=project.title)
    row.owner_user_id=db.session.get(CampGroup,project.group_id).owner_user_id
    row.title=project.title;row.summary=core.text(data.get('summary'),'公开简介',300)
    row.description=core.text(data.get('description'),'公开详情',20000,True)
    row.project_status='done' if project.status=='completed' else 'ongoing';row.status='visible'
    db.session.add(row);db.session.flush();project.version+=1
    core.event(project,user,'publish',core.text(data.get('reason'),'发布原因',500),after={'showcase_id':row.id})
    return result(core.save_command(project,user,cid,digest,{'showcase_id':row.id,'project':core.serialize(project,user)}),commit=True)


@bp.get('/projects/<int:pid>/camps/<int:bid>/archive')
@jwt_required()
def binding_archive(pid,bid):
    import copy
    from models import ProjectBindingSnapshot,CampMember
    user=_current_user();binding,_,_=camps.get_binding(pid,bid,user)
    snapshot=db.session.get(ProjectBindingSnapshot,bid)
    if not snapshot:raise core.ProjectError('本期尚无冻结记录',status=404)
    data=copy.deepcopy(snapshot.snapshot)
    privileged=user.is_admin() or camps.staff(user,binding.camp_session_id) or binding.leader_user_id==user.id
    if not privileged:
        if not any(p['user_id']==user.id for p in data.get('participation_history',data.get('participants',[]))) or not CampMember.query.filter_by(camp_session_id=binding.camp_session_id,user_id=user.id,status='active').first():raise core.ProjectError('无权读取本期历史',status=403)
        for node in data.get('assessment_nodes',[]):
            node['evaluations']=[e for e in node['evaluations'] if e['member_user_id']==user.id]
            node['submissions']=[s for s in node['submissions'] if s['submitted_by']==user.id]
        if not any(p['user_id']==user.id for p in data.get('participants',[])):
            for key in ('records','camp_meetings','camp_outcomes'):data[key]=[]
    return result({'archive':data})


@bp.get('/legacy-candidates')
@jwt_required()
def legacy_candidates():
    from models import CampUnit,CampSession,ProjectLegacyMap
    if not _current_user().is_admin():raise core.ProjectError('仅管理员可查看迁移候选',status=403)
    rows=db.session.query(CampUnit,CampSession).join(CampSession,CampSession.id==CampUnit.camp_session_id).filter(CampUnit.unit_type=='project').order_by(CampUnit.id).limit(500).all()
    return result({'units':[{'unit_id':u.id,'name':u.name,'camp_name':c.name,'write_model':(db.session.get(ProjectLegacyMap,u.id).write_model if db.session.get(ProjectLegacyMap,u.id) else 'legacy')} for u,c in rows]})


@bp.get('/projects/<int:pid>/history')
@jwt_required()
def project_history(pid):
    from models import ProjectChange,UserModel
    core.get_project(pid,_current_user(),manage=True)
    rows=ProjectChange.query.filter_by(project_id=pid).order_by(ProjectChange.id.desc()).limit(100).all()
    return result({'events':[{'id':r.id,'action':r.action,'reason':r.reason,'actor':db.session.get(UserModel,r.actor_id).username,'occurred_at':r.occurred_at.isoformat()} for r in rows]})

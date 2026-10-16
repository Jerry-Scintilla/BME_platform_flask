"""项目主档及长期团队；命令在调用方事务内完成。"""
import uuid
from datetime import date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from sqlalchemy import or_
from exts import db
from models import Project, ProjectChange, ProjectCommand, CampGroup, GroupMembership, GroupRoleAssignment, UserModel,ProjectMilestone
from services.groups import GroupService, GovernanceContext, GovernanceDecision, GovernancePolicy,GroupError
from services.groups.identity import require_live_account, eligible_now
from services.groups.service import _digest
from . import ProjectError


def text(value, label, limit=100, optional=False):
    if optional and value in (None, ''):
        return None
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
        raise ProjectError(label+'不能为空或超长', machine='PROJECT_INVALID_INPUT', status=400)
    return value.strip()


def number(value, label='ID'):
    if type(value) is not int or value <= 0:
        raise ProjectError(label+'须为正整数', machine='PROJECT_INVALID_INPUT', status=400)
    return value


def day(value):
    if value in (None, ''):
        return None
    try:
        if not isinstance(value, str) or len(value) != 10:
            raise ValueError()
        return date.fromisoformat(value)
    except ValueError:
        raise ProjectError('日期须为YYYY-MM-DD', machine='PROJECT_INVALID_DATE', status=400)


def role(project, user, *, fresh=False):
    try:require_live_account(user)
    except GroupError:return None
    if not project.migration_ready:return None
    if user.is_admin():return 'admin'
    if user.account_kind!='standard':return None
    decision = GovernanceDecision('project:member', 'project_join', project.id)
    if not eligible_now(user, decision):
        return None
    member_query=GroupMembership.query.filter_by(group_id=project.group_id,user_id=user.id,status='active')
    member=(member_query.populate_existing().with_for_update() if fresh else member_query).first()
    if member is None or member.person_id != user.person_id:
        return None
    now = datetime.now()
    role_query=GroupRoleAssignment.query.filter_by(membership_id=member.id, active_slot=1, scope_type='group', scope_id=0).filter(
        GroupRoleAssignment.started_at <= now, or_(GroupRoleAssignment.expires_at.is_(None), GroupRoleAssignment.expires_at > now))
    roles=(role_query.populate_existing().with_for_update() if fresh else role_query).all()
    if any(r.role == 'project_leader' for r in roles) and eligible_now(user, decision, appointment=True):
        return 'leader'
    return 'member' if any(r.role == 'project_member' for r in roles) else None


def get_project(pid, user, *, manage=False, write=False, fresh=False):
    project = Project.query.filter_by(id=pid).populate_existing().with_for_update().first() if fresh else db.session.get(Project,pid)
    if project is None:
        raise ProjectError('项目不存在', machine='PROJECT_NOT_FOUND', status=404)
    current = role(project,user,fresh=fresh)
    if current is None or (manage and current not in ('admin', 'leader')):
        raise ProjectError('无权访问或管理此项目', machine='PROJECT_FORBIDDEN', status=403)
    if write and project.status != 'active':
        raise ProjectError('项目当前只读，请先恢复项目状态', machine='PROJECT_READ_ONLY')
    return project


class ProjectPolicy(GovernancePolicy):
    def authorize(self, *, context, actor, group, action, payload):
        project = Project.query.filter_by(id=context.scope_id).populate_existing().with_for_update().first()
        if project is None or context.kind != 'project_core':
            return None
        if not project.migration_ready:return None
        if group is None:
            allowed = action == 'create_group' and project.group_id is None and (actor.is_admin() or project.created_by == actor.id == payload.get('user_id'))
        elif project.group_id != group.id:
            return None
        else:
            current = role(project,actor,fresh=not action.startswith('read_'))
            bootstrap = action == 'grant_role' and group.owner_user_id == actor.id == payload.get('user_id') and project.created_by == actor.id
            allowed = current in ('admin', 'leader') or bootstrap or (action.startswith('read_') and current == 'member')
        if allowed:
            if not action.startswith('read_') and project.status != 'active':
                raise ProjectError('项目当前只读', machine='PROJECT_READ_ONLY')
            return GovernanceDecision('project:governance', 'project_join', project.id)

    def participant_allowed(self, *, context, group, user):
        return not user.is_admin() and user.account_kind == 'standard'

    def role_allowed(self, *, context, group, user, role, scope_type, scope_id):
        return role in ('project_leader', 'project_member') and scope_type == 'group' and scope_id == 0 and self.participant_allowed(context=context, group=group, user=user)


groups = GroupService({'project_core': ProjectPolicy()})


def snapshot(project):
    return {key: (getattr(project, key).isoformat() if key == 'zero_date' and project.zero_date else getattr(project, key))
            for key in ('id', 'code', 'title', 'background', 'goal', 'status', 'zero_date', 'timezone', 'version')}


def serialize(project, user):
    result = snapshot(project)
    current = role(project, user)
    members = GroupMembership.query.filter_by(group_id=project.group_id, status='active').count()
    owner = db.session.get(CampGroup, project.group_id)
    owner_user = db.session.get(UserModel, owner.owner_user_id) if owner else None
    total=ProjectMilestone.query.filter(ProjectMilestone.project_id==project.id,ProjectMilestone.status!='cancelled').count()
    done=ProjectMilestone.query.filter_by(project_id=project.id,status='done').count()
    return dict(result, my_role=current, member_count=members, owner_name=owner_user.username if owner_user else None,
                research_summary={'total':total,'done':done,'rate':round(100*done/total,1) if total else None},
                zero_date_confirmed=bool(project.zero_date_confirmed_at), can_manage=current in ('admin','leader'),
                can_write=project.status == 'active' and current is not None,
                group_version=owner.version if owner else None)


def event(project, user, action, reason, before=None, after=None):
    row=ProjectChange(project_id=project.id, actor_id=user.id, action=action, reason=reason, before=before, after=after)
    db.session.add(row)
    return row


def command(project, user, data, action):
    """重放先复核授权；版本冲突不绕过幂等结果。"""
    key = text(data.get('idempotency_key'), '幂等键', 128)
    digest = _digest([project.id if project else None, action, data])
    cid = _digest(['project', user.id, key])
    existing = ProjectCommand.query.filter_by(id=cid).populate_existing().with_for_update().first()
    if existing:
        if existing.digest != digest:
            raise ProjectError('同一幂等键对应了不同请求', machine='PROJECT_IDEMPOTENCY_CONFLICT')
        return cid, digest, existing.result
    if project and getattr(project,'_snapshot_stale',False):
        raise ProjectError('项目在等待期间已变化，请刷新重试',machine='PROJECT_VERSION_CONFLICT')
    if project and (type(data.get('expected_version')) is not int or data['expected_version'] != project.version):
        raise ProjectError('项目已变化，请刷新重试', machine='PROJECT_VERSION_CONFLICT')
    return cid, digest, None


def save_command(project, user, cid, digest, result):
    db.session.add(ProjectCommand(id=cid, project_id=project.id, actor_id=user.id, digest=digest, result=result))
    return result


def lock(project, user, target_id=None, additional_user_ids=()):
    seen_version=project.version
    group=db.session.get(CampGroup,project.group_id)
    ids={m.user_id for m in GroupMembership.query.filter_by(group_id=project.group_id,status='active')}
    ids.update([user.id,group.owner_user_id]);ids.update(additional_user_ids)
    if target_id:ids.add(target_id)
    lock_accounts(ids)
    CampGroup.query.filter_by(id=project.group_id).populate_existing().with_for_update().first()
    project=Project.query.filter_by(id=project.id).populate_existing().with_for_update().one()
    project._snapshot_stale=project.version!=seen_version
    return project


def lock_accounts(ids):
    from models import PersonModel
    rows=UserModel.query.filter(UserModel.id.in_(sorted(set(ids)))).order_by(UserModel.id).populate_existing().with_for_update().all()
    people=sorted({u.person_id for u in rows if u.person_id})
    if people:PersonModel.query.filter(PersonModel.id.in_(people)).order_by(PersonModel.id).populate_existing().with_for_update().all()


def create(user, data):
    require_live_account(user)
    GroupService._lock_users(user.id, data.get('owner_user_id') or user.id)
    cid, digest, replay = command(None, user, data, 'create')
    if replay:
        get_project(replay['project']['id'],user,fresh=True)
        return replay
    owner = number(data.get('owner_user_id') or user.id)
    if not user.is_admin() and owner != user.id:
        raise ProjectError('只能为自己创建项目', machine='PROJECT_FORBIDDEN', status=403)
    project = Project(code='PJ-'+uuid.uuid4().hex[:16].upper(), title=text(data.get('title'), '项目名称'),
                      background=text(data.get('background'), '项目背景', 20000, True), goal=text(data.get('goal'), '项目目标', 20000, True), created_by=user.id)
    db.session.add(project); db.session.flush()
    args = dict(context=GovernanceContext('project_core', project.id), actor_user_id=user.id, reason='创建独立项目')
    group = groups.create_group(name=project.title, owner_user_id=owner, idempotency_key=cid+':group', **args)['group']
    project.group_id = group['id']; db.session.flush()
    groups.grant_role(user_id=owner, role='project_leader', group_id=group['id'], expected_version=group['version'], idempotency_key=cid+':leader', **args)
    event(project, user, 'create', '创建独立项目', after=snapshot(project))
    return save_command(project, user, cid, digest, {'project': serialize(project, user)})


def update(user, pid, data):
    project = lock(get_project(pid, user, manage=True), user)
    get_project(pid,user,manage=True,fresh=True)
    cid, digest, replay = command(project, user, data, 'update')
    if replay:
        return replay
    before = snapshot(project)
    reason = text(data.get('reason'), '变更原因', 500)
    for key, label, limit in [('title','项目名称',100),('background','项目背景',20000),('goal','项目目标',20000)]:
        if key in data:
            setattr(project, key, text(data[key], label, limit, key != 'title'))
    if 'timezone' in data:
        zone = text(data['timezone'], '时区', 64)
        try: ZoneInfo(zone)
        except (ZoneInfoNotFoundError, ValueError): raise ProjectError('时区无效', status=400)
        project.timezone = zone
    if 'status' in data:
        if data['status'] not in ('active','paused','completed','terminated','archived'):
            raise ProjectError('项目状态无效', status=400)
        if data['status'] in ('completed','terminated','archived'):
            from models import ProjectCampBinding
            from .camps import ready
            if any(ready(b) for b in ProjectCampBinding.query.filter_by(project_id=pid,status='active')):raise ProjectError('请先处理仍进行中的参营关联',machine='PROJECT_ACTIVE_BINDING')
        project.status = data['status']
    project.version += 1
    if 'status' in data:
        from .publication import synchronize_status
        synchronize_status(project)
    event(project, user, 'update', reason, before, snapshot(project))
    return save_command(project, user, cid, digest, {'project': serialize(project, user)})


def members(project):
    rows = db.session.query(GroupMembership, UserModel).join(UserModel, UserModel.id == GroupMembership.user_id).filter(GroupMembership.group_id == project.group_id, GroupMembership.status == 'active').all()
    return [{'user_id': u.id, 'name': u.username, 'role': role(project, u)} for m,u in rows]


def change_member(user, pid, data):
    initial=get_project(pid,user,manage=True,write=True)
    target=number(data.get('user_id'))
    project = lock(initial,user,target)
    get_project(pid,user,manage=True,write=True,fresh=True)
    cid, digest, replay = command(project, user, data, 'member')
    if replay: return replay
    action, uid = data.get('action'), data['user_id']
    reason = text(data.get('reason'), '变更原因', 500)
    def run(method, suffix, **kw):
        group = db.session.get(CampGroup, project.group_id)
        return method(context=GovernanceContext('project_core', project.id), actor_user_id=user.id, group_id=group.id,
                      expected_version=group.version, idempotency_key=cid+suffix, reason=reason, **kw)
    if action == 'add':
        run(groups.add_member, ':join', user_id=uid)
        run(groups.grant_role, ':role', user_id=uid, role='project_member')
    elif action == 'remove':
        from .camps import end_project_participations
        end_project_participations(project,uid,user,reason)
        run(groups.end_membership, ':exit', user_id=uid)
    elif action == 'transfer':
        group = db.session.get(CampGroup, project.group_id); old = group.owner_user_id
        if uid == old: raise ProjectError('新负责人须与现任不同', status=400)
        from models import ProjectCampBinding
        from .camps import ready
        if any(ready(b) for b in ProjectCampBinding.query.filter_by(project_id=pid,status='active',leader_user_id=old)):
            raise ProjectError('请先交接仍有效的本期负责人，再转交长期项目',machine='PROJECT_BINDING_HANDOFF_REQUIRED')
        run(groups.transfer_owner, ':owner', user_id=uid)
        run(groups.grant_role, ':leader', user_id=uid, role='project_leader')
        old_user=db.session.get(UserModel,old)
        if old_user and old_user.account_kind=='standard' and eligible_now(old_user,GovernanceDecision('project:member','project_join',pid)):
            run(groups.grant_role, ':old-member', user_id=old, role='project_member')
            run(groups.end_role, ':end-leader', user_id=old, role='project_leader')
        else:run(groups.end_membership, ':end-old-member',user_id=old)
    else: raise ProjectError('成员操作无效', status=400)
    project.version += 1
    event(project, user, 'member:'+action, reason, after={'user_id':uid})
    return save_command(project, user, cid, digest, {'project':serialize(project,user),'members':members(project)})

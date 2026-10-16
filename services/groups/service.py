"""共用小组服务：显式治理策略、同事务事件、幂等命令与组级并发控制。

调用方管理外层事务；本服务只 flush/保存点，不 commit，不发送通知。
禁止调用方绕过服务直接修改核心表。由培训/项目应用服务装配治理策略；未装配的上下文默认拒绝访问。
"""
from datetime import datetime
import hashlib
import json

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError

from exts import db
from models import (CampGroup, GroupMembership, GroupRoleAssignment,
                    GroupMembershipEvent, GroupOperation, UserModel, PersonModel)
from .access import GovernanceContext, GovernanceDecision, GovernancePolicy
from .errors import GroupError
from . import identity


ROLES = {
    'training': frozenset({'learner', 'mentor'}),
    'project': frozenset({'project_leader', 'project_member'}),
    'project_core': frozenset({'project_leader', 'project_member'}),
}
APPOINTED_ROLES = frozenset({'mentor', 'project_leader'})


def _positive(value, label):
    if type(value) is not int or value < 1:
        raise GroupError(label + '必须为正整数', machine='GROUP_INVALID_INPUT', status=400)
    return value


def _text(value, label, limit):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
        raise GroupError(label + '不能为空或超长', machine='GROUP_INVALID_INPUT', status=400)
    return value.strip()


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode()).hexdigest()


def _snapshot(row):
    fields = {
        CampGroup: ('id', 'name', 'status', 'owner_user_id', 'version'),
        GroupMembership: ('id', 'group_id', 'user_id', 'person_id', 'status',
                          'started_at', 'ended_at'),
        GroupRoleAssignment: ('id', 'membership_id', 'group_id', 'role', 'scope_type',
                              'scope_id', 'active_slot', 'granted_by', 'started_at',
                              'expires_at', 'ended_at'),
    }
    result = {}
    for field in fields[type(row)]:
        value = getattr(row, field)
        result[field] = value.isoformat() if isinstance(value, datetime) else value
    return result


class GroupService:
    def __init__(self, policies=None):
        self._policies = dict(policies or {})
        for kind, policy in self._policies.items():
            if kind not in ROLES or not isinstance(policy, GovernancePolicy):
                raise ValueError('仅接受已注册业务类型的 GovernancePolicy 实现')

    def _policy(self, context):
        if not isinstance(context, GovernanceContext) or not isinstance(context.kind, str):
            raise GroupError('缺少治理上下文', machine='GROUP_CONTEXT_REQUIRED', status=403)
        _positive(context.scope_id, '治理对象 ID')
        policy = self._policies.get(context.kind)
        if policy is None:
            raise GroupError('该治理入口尚未接入', machine='GROUP_CONTEXT_REQUIRED', status=403)
        return policy

    def _authorize(self, policy, context, actor, group, action, payload):
        identity.require_live_account(actor)
        decision = policy.authorize(context=context, actor=actor, group=group,
                                    action=action, payload=dict(payload))
        if not isinstance(decision, GovernanceDecision):
            raise GroupError('无权执行此小组操作', machine='GROUP_FORBIDDEN', status=403)
        _text(decision.authorization_source, '授权来源', 100)
        _text(decision.participation_scope, '参与宽限范围', 50)
        _positive(decision.participation_scope_id, '参与宽限对象 ID')
        return decision

    def _participant(self, policy, context, group, user, decision, *, appointed=False):
        identity.require_participant_identity(user, decision)
        if policy.participant_allowed(context=context, group=group, user=user) is not True:
            raise GroupError('目标人员不具备当前业务参与资格', machine='GROUP_PARTICIPATION_REQUIRED')
        if appointed:
            identity.require_appointment(user, decision)

    def create_group(self, *, name, owner_user_id, **command):
        return self._write('create_group', {'name': _text(name, '组名', 100),
                           'user_id': _positive(owner_user_id, '负责人 ID')}, **command)

    def add_member(self, *, user_id, **command):
        return self._write('add_member', {'user_id': _positive(user_id, '用户 ID')}, **command)

    def end_membership(self, *, user_id, **command):
        return self._write('end_membership', {'user_id': _positive(user_id, '用户 ID')}, **command)

    def transfer_owner(self, *, user_id, **command):
        return self._write('transfer_owner', {'user_id': _positive(user_id, '负责人 ID')}, **command)

    def rename_group(self, *, name, **command):
        return self._write('rename_group', {'name': _text(name, '组名', 100)}, **command)

    def archive_group(self, *, end_memberships=False, **command):
        if type(end_memberships) is not bool:
            raise GroupError('结束成员参数必须为布尔值', status=400)
        return self._write('archive_group', {'end_memberships': end_memberships}, **command)

    def grant_role(self, *, user_id, role, scope_type='group', scope_id=0,
                   expires_at=None, **command):
        if expires_at is not None and (not isinstance(expires_at, datetime)
                                       or expires_at.tzinfo is not None):
            raise GroupError('职责有效期须为本地无时区 datetime', machine='GROUP_INVALID_INPUT', status=400)
        return self._write('grant_role', {
            'user_id': _positive(user_id, '用户 ID'), 'role': role,
            'scope_type': scope_type, 'scope_id': scope_id,
            'expires_at': expires_at.isoformat() if expires_at else None,
        }, **command)

    def end_role(self, *, user_id, role, scope_type='group', scope_id=0, **command):
        return self._write('end_role', {
            'user_id': _positive(user_id, '用户 ID'), 'role': role,
            'scope_type': scope_type, 'scope_id': scope_id,
        }, **command)

    def _write(self, action, payload, *, context, actor_user_id, idempotency_key,
               reason, group_id=None, expected_version=None):
        policy = self._policy(context)
        _positive(actor_user_id, '操作者 ID')
        key = _text(idempotency_key, '幂等键', 128)
        reason = _text(reason, '变更原因', 500)
        if action == 'create_group':
            if group_id is not None or expected_version is not None:
                raise GroupError('创建不能指定已有组或版本', machine='GROUP_INVALID_INPUT', status=400)
        else:
            _positive(group_id, '组 ID')
            _positive(expected_version, '预期版本')
        if action in ('grant_role', 'end_role'):
            self._validate_role(context, payload)
        request_digest = _digest({'action': action, 'payload': payload, 'group_id': group_id,
                                  'version': expected_version, 'reason': reason,
                                  'context': [context.kind, context.scope_id]})
        operation_id = _digest([actor_user_id, key])
        try:
            # 保存点前 SQLAlchemy 会 flush 调用方待写对象；外层事务仍由应用服务负责。
            with db.session.begin_nested():
                users = self._lock_users(actor_user_id, payload.get('user_id'))
                actor = users.get(actor_user_id)
                identity.require_live_account(actor)
                operation = GroupOperation.query.filter_by(id=operation_id).populate_existing().with_for_update().first()
                if operation is not None and operation.request_digest != request_digest:
                    raise GroupError('同一幂等键不能用于不同请求', machine='GROUP_IDEMPOTENCY_CONFLICT')
                lookup_id = operation.group_id if operation else group_id
                group = None
                if lookup_id is not None:
                    group = CampGroup.query.filter_by(id=lookup_id).populate_existing().with_for_update().first()
                    if group is None:
                        raise GroupError('小组不存在', machine='GROUP_NOT_FOUND', status=404)
                decision = self._authorize(
                    policy, context, actor, group,
                    'read_group' if operation and action == 'create_group' else action, payload)
                if operation is not None:
                    return dict(operation.result)
                if group is not None:
                    if group.status != 'active':
                        raise GroupError('小组已归档，只能读取', machine='GROUP_READ_ONLY')
                    if group.version != expected_version:
                        raise GroupError('小组已变更，请刷新后重试', machine='GROUP_VERSION_CONFLICT')
                before = {'group': _snapshot(group)} if group else None
                target = users.get(payload.get('user_id'))
                group, member, detail = self._apply(
                    action, payload, group, target, actor, policy, context, decision)
                if action != 'create_group':
                    group.version += 1
                db.session.flush()
                result = {'group': _snapshot(group), 'detail': detail}
                if member is not None:
                    result['membership'] = _snapshot(member)
                event = GroupMembershipEvent(
                    group_id=group.id, membership_id=member.id if member else None,
                    actor_user_id=actor.id, action=action, context_kind=context.kind,
                    context_id=context.scope_id, authorization_source=decision.authorization_source,
                    reason=reason, before=before, after=result, group_version=group.version)
                db.session.add(event)
                db.session.flush()
                result = dict(result, event_id=event.id)
                db.session.add(GroupOperation(
                    id=operation_id, actor_user_id=actor.id, group_id=group.id,
                    request_digest=request_digest, result=result))
                db.session.flush()
                return result
        except (IntegrityError, StaleDataError) as exc:
            raise GroupError('成员、职责或版本发生冲突，请刷新后重试',
                             machine='GROUP_CONCURRENT_CONFLICT') from exc

    @staticmethod
    def _lock_users(actor_id, target_id):
        ids = sorted({uid for uid in (actor_id, target_id) if uid is not None})
        users = UserModel.query.filter(UserModel.id.in_(ids)).order_by(
            UserModel.id).populate_existing().with_for_update().all()
        person_ids = sorted({user.person_id for user in users if user.person_id})
        if person_ids:
            PersonModel.query.filter(PersonModel.id.in_(person_ids)).order_by(
                PersonModel.id).populate_existing().with_for_update().all()
        return {user.id: user for user in users}

    @staticmethod
    def _validate_role(context, payload):
        role = payload['role']
        scope_type, scope_id = payload['scope_type'], payload['scope_id']
        if not isinstance(role, str) or role not in ROLES[context.kind]:
            raise GroupError('职责不属于此教学或项目上下文', machine='GROUP_INVALID_ROLE', status=400)
        if type(scope_id) is not int or not (
                (scope_type == 'group' and scope_id == 0) or
                (context.kind == 'training' and scope_type == 'course' and scope_id > 0)):
            raise GroupError('职责作用范围无效', machine='GROUP_INVALID_SCOPE', status=400)

    @staticmethod
    def _member(group, user_id, *, active=False):
        row = GroupMembership.query.filter_by(group_id=group.id, user_id=user_id).populate_existing().first()
        if active and (row is None or row.status != 'active'):
            raise GroupError('目标不是有效小组成员', machine='GROUP_MEMBER_REQUIRED')
        return row

    def _join(self, group, user):
        member = self._member(group, user.id)
        if user.person_id:
            other = GroupMembership.query.filter_by(group_id=group.id, person_id=user.person_id).first()
            if other is not None and other.user_id != user.id:
                raise GroupError('该人员已通过另一账号加入本组，请先处理账号参与关系',
                                 machine='GROUP_DUPLICATE_PERSON')
        if member is None:
            member = GroupMembership(group_id=group.id, user_id=user.id,
                                     person_id=user.person_id, status='active')
            db.session.add(member)
        elif member.person_id is not None and member.person_id != user.person_id:
            raise GroupError('成员实名关联已变化，需要先处理映射', machine='GROUP_IDENTITY_CONFLICT')
        else:
            member.person_id = user.person_id
            if member.status == 'ended':
                member.status, member.started_at, member.ended_at = 'active', datetime.now(), None
        db.session.flush()
        return member

    def _apply(self, action, payload, group, target, actor, policy, context, decision):
        if action in ('create_group', 'add_member', 'transfer_owner', 'grant_role'):
            self._participant(policy, context, group, target, decision,
                              appointed=(action in ('create_group', 'transfer_owner') or
                                         payload.get('role') in APPOINTED_ROLES))
        if action == 'create_group':
            group = CampGroup(name=payload['name'], owner_user_id=target.id,
                                      status='active', version=1)
            db.session.add(group)
            db.session.flush()
            member = self._join(group, target)
            return group, member, {}
        if action == 'add_member':
            previous = self._member(group, target.id)
            before = _snapshot(previous) if previous else None
            return group, self._join(group, target), {'previous_membership': before}
        if action == 'end_membership':
            member = self._member(group, payload['user_id'])
            if member is None:
                raise GroupError('小组成员不存在', machine='GROUP_MEMBER_NOT_FOUND', status=404)
            if member.user_id == group.owner_user_id:
                raise GroupError('请先转交治理负责人，再结束其成员关系', machine='GROUP_OWNER_REQUIRED')
            before = _snapshot(member)
            now = datetime.now()
            roles = GroupRoleAssignment.query.filter_by(membership_id=member.id, active_slot=1).all()
            previous_roles = [_snapshot(role) for role in roles]
            for role in roles:
                self._close_role(role, now)
            if member.status == 'active':
                member.status, member.ended_at = 'ended', now
            return group, member, {'previous_membership': before, 'ended_roles': previous_roles}
        if action == 'transfer_owner':
            member = self._member(group, target.id, active=True)
            if member.person_id != target.person_id:
                raise GroupError('请先校准成员的人员关联', machine='GROUP_IDENTITY_CONFLICT')
            group.owner_user_id = target.id
            return group, member, {}
        if action == 'rename_group':
            group.name = payload['name']
            return group, None, {}
        if action == 'archive_group':
            ended = []
            if payload['end_memberships']:
                now = datetime.now()
                for member in GroupMembership.query.filter_by(group_id=group.id, status='active').all():
                    ended.append(_snapshot(member))
                    for role in GroupRoleAssignment.query.filter_by(membership_id=member.id, active_slot=1).all():
                        self._close_role(role, now)
                    member.status, member.ended_at = 'ended', now
            group.status = 'archived'
            return group, None, {'ended_memberships': ended}
        member = self._member(group, payload['user_id'], active=True)
        if action == 'grant_role' and member.person_id != target.person_id:
            raise GroupError('请先校准成员的人员关联', machine='GROUP_IDENTITY_CONFLICT')
        if action == 'grant_role' and policy.role_allowed(
                context=context, group=group, user=target, role=payload['role'],
                scope_type=payload['scope_type'], scope_id=payload['scope_id']) is not True:
            raise GroupError('职责资格或业务对象范围不匹配', machine='GROUP_ROLE_FORBIDDEN', status=403)
        role = GroupRoleAssignment.query.filter_by(
            membership_id=member.id, role=payload['role'], scope_type=payload['scope_type'],
            scope_id=payload['scope_id'], active_slot=1).first()
        previous = _snapshot(role) if role else None
        now = datetime.now()
        if action == 'end_role':
            if role:
                self._close_role(role, now)
            return group, member, {'previous_role': previous, 'role': _snapshot(role) if role else None}
        expires_at = datetime.fromisoformat(payload['expires_at']) if payload['expires_at'] else None
        if expires_at is not None and expires_at <= now:
            raise GroupError('职责有效期必须晚于当前时间', machine='GROUP_INVALID_INPUT', status=400)
        if role and role.expires_at is not None and role.expires_at <= now:
            self._close_role(role, now)
            db.session.flush()  # 先释放唯一有效槽，再插入新的任命记录。
            role = None
        if role is not None and role.expires_at != expires_at:
            raise GroupError('已有有效职责，调整期限须先结束原任命', machine='GROUP_ROLE_CONFLICT')
        if role is None:
            role = GroupRoleAssignment(
                group_id=group.id, membership_id=member.id, role=payload['role'],
                scope_type=payload['scope_type'], scope_id=payload['scope_id'],
                active_slot=1, granted_by=actor.id, started_at=now, expires_at=expires_at)
            db.session.add(role)
            db.session.flush()
        return group, member, {'previous_role': previous, 'role': _snapshot(role)}

    @staticmethod
    def _close_role(role, now):
        role.active_slot = None
        role.ended_at = min(role.expires_at, now) if role.expires_at else now

    def _read(self, context, actor_user_id, group_id, action):
        policy = self._policy(context)
        _positive(group_id, '组 ID')
        actor = db.session.get(UserModel, _positive(actor_user_id, '操作者 ID'))
        identity.require_live_account(actor)
        group = db.session.get(CampGroup, group_id)
        if group is None:
            raise GroupError('小组不存在', machine='GROUP_NOT_FOUND', status=404)
        decision = self._authorize(policy, context, actor, group, action, {})
        return policy, group, decision

    def get_group(self, *, context, actor_user_id, group_id):
        _, group, _ = self._read(context, actor_user_id, group_id, 'read_group')
        return _snapshot(group)

    def list_members(self, *, context, actor_user_id, group_id, include_ended=False,
                     after_id=0, limit=100):
        _, group, _ = self._read(context, actor_user_id, group_id, 'read_members')
        self._page(after_id, limit)
        query = GroupMembership.query.filter(GroupMembership.group_id == group.id,
                                             GroupMembership.id > after_id)
        if not include_ended:
            query = query.filter_by(status='active')
        return [_snapshot(row) for row in query.order_by(GroupMembership.id).limit(limit).all()]

    def effective_roles(self, *, context, actor_user_id, group_id, user_id):
        """返回经过当前参与、核验和业务范围过滤的职责；业务仍须另行判断自评等限制。"""
        policy, group, decision = self._read(context, actor_user_id, group_id, 'read_roles')
        user = db.session.get(UserModel, _positive(user_id, '用户 ID'))
        member = self._member(group, user_id)
        if (group.status != 'active' or member is None or member.status != 'active' or
                not identity.eligible_now(user, decision) or
                member.person_id != user.person_id or
                policy.participant_allowed(context=context, group=group, user=user) is not True):
            return []
        now = datetime.now()
        roles = GroupRoleAssignment.query.filter_by(membership_id=member.id, active_slot=1).filter(
            GroupRoleAssignment.started_at <= now,
            db.or_(GroupRoleAssignment.expires_at.is_(None), GroupRoleAssignment.expires_at > now)
        ).order_by(GroupRoleAssignment.id).all()
        return [_snapshot(role) for role in roles
                if role.role in ROLES[context.kind]
                and identity.eligible_now(user, decision, appointment=role.role in APPOINTED_ROLES)
                and policy.role_allowed(context=context, group=group, user=user, role=role.role,
                                        scope_type=role.scope_type, scope_id=role.scope_id) is True]

    def list_events(self, *, context, actor_user_id, group_id, after_id=0, limit=100):
        _, group, _ = self._read(context, actor_user_id, group_id, 'read_events')
        self._page(after_id, limit)
        rows = GroupMembershipEvent.query.filter(
            GroupMembershipEvent.group_id == group.id, GroupMembershipEvent.id > after_id
        ).order_by(GroupMembershipEvent.id).limit(limit).all()
        return [{field: (getattr(row, field).isoformat() if field == 'occurred_at'
                          else getattr(row, field)) for field in (
            'id', 'group_id', 'membership_id', 'actor_user_id', 'action', 'context_kind',
            'context_id', 'authorization_source', 'reason', 'before', 'after',
            'group_version', 'occurred_at')} for row in rows]

    @staticmethod
    def _page(after_id, limit):
        if type(after_id) is not int or after_id < 0 or type(limit) is not int or not 1 <= limit <= 200:
            raise GroupError('分页参数无效（limit 为 1 至 200）', machine='GROUP_INVALID_INPUT', status=400)

"""旧项目成员映射；培训预览统一使用 training_migration.preview。不连接数据库、不自行提交。"""
from datetime import datetime

from exts import db
from models import (CampSession, CampUnit, CampUnitMember, CampMember,
                    UnitPersonParticipationModel,
                    UserModel, CampGroup, GroupMembership, GroupRoleAssignment,
                    GroupMembershipEvent, LegacyUnitMembershipMap)
from services.groups import GroupError
from services.groups.service import _digest
from .projects import lock_project


def preview_project(unit_id):
    unit = CampUnit.query.filter_by(id=unit_id).populate_existing().first()
    if unit is None or unit.unit_type != 'project':
        raise GroupError('项目单元不存在', status=404)
    camp = db.session.get(CampSession, unit.camp_session_id)
    if camp is None or camp.category != 'project':
        raise GroupError('项目营期不存在', status=404)
    members = CampUnitMember.query.filter_by(unit_id=unit.id).order_by(CampUnitMember.id).populate_existing().all()
    report = dict(unit_id=unit.id, camp_session_id=camp.id, group_id=unit.group_id,
                  state='mapped' if unit.group_id else 'pending', conflicts=[], members=[],
                  unit_status=unit.status, camp_status=camp.status, owner_user_id=unit.owner_user_id,
                  name=unit.name, created_at=unit.created_at.isoformat() if unit.created_at else None)
    people = {}
    leader_rows = []
    for row in members:
        user = db.session.get(UserModel, row.user_id)
        anchor = UnitPersonParticipationModel.query.filter_by(unit_member_id=row.id).first()
        mapping = db.session.get(LegacyUnitMembershipMap, row.id)
        record = dict(legacy_member_id=row.id, user_id=row.user_id,
                      person_id=user.person_id if user else None,
                      role=row.role, status=row.status,
                      started_at=row.started_at.isoformat() if row.started_at else None,
                      ended_at=row.ended_at.isoformat() if row.ended_at else None,
                      membership_id=mapping.membership_id if mapping else None,
                      legacy_anchor_person_id=anchor.person_id if anchor else None)
        report['members'].append(record)
        if user is None:
            report['conflicts'].append(f'user_missing:{row.id}')
            continue
        if user.person_id:
            if user.person_id in people:
                report['conflicts'].append(f'duplicate_person:{row.id}:{people[user.person_id]}')
            people[user.person_id] = row.id
        if anchor and (anchor.unit_id != unit.id or anchor.user_id != row.user_id or anchor.person_id != user.person_id):
            report['conflicts'].append(f'anchor_mismatch:{row.id}')
        if row.role not in ('leader', 'member') or row.status not in ('active', 'ended'):
            report['conflicts'].append(f'unknown_member_state:{row.id}')
        if row.started_at is None or (row.status == 'ended' and row.ended_at is None):
            report['conflicts'].append(f'missing_member_time:{row.id}')
        if row.status == 'active' and row.ended_at is not None:
            report['conflicts'].append(f'active_member_has_end_time:{row.id}')
        if row.ended_at and row.started_at and row.ended_at < row.started_at:
            report['conflicts'].append(f'invalid_member_time:{row.id}')
        if row.status == 'active':
            if unit.status == 'terminated':
                report['conflicts'].append(f'terminated_unit_active_member:{row.id}')
            if row.role == 'leader':
                leader_rows.append(row.user_id)
            if not unit.group_id and camp.status not in ('archived', 'deleted'):
                if not CampMember.query.filter_by(camp_session_id=camp.id, user_id=row.user_id, status='active').first():
                    report['conflicts'].append(f'camp_participation_missing:{row.id}')
                if user.status != 'active' or user.lifecycle != 'active':
                    report['conflicts'].append(f'account_inactive:{row.id}')
        if unit.group_id:
            target = db.session.get(GroupMembership, mapping.membership_id) if mapping else None
            if target is None or target.group_id != unit.group_id or target.user_id != row.user_id:
                report['conflicts'].append(f'mapping_missing_or_mismatched:{row.id}')
        elif mapping:
            report['conflicts'].append(f'unbound_mapping:{row.id}')
    if unit.status != 'terminated' and not unit.group_id and leader_rows != [unit.owner_user_id]:
        report['conflicts'].append('owner_membership_mismatch')
    if unit.status == 'terminated' and unit.owner_user_id not in [row.user_id for row in members]:
        report['conflicts'].append('historical_owner_missing')
    report['digest'] = _digest(report)
    return report


def map_project(unit_id, *, actor_user_id, expected_digest):
    """显式应用预览结果；仅后台编排/隔离检查调用，命令行不提供写入选项。"""
    actor = db.session.get(UserModel, actor_user_id)
    if actor is None or not actor.is_admin() or actor.status != 'active' or actor.lifecycle != 'active':
        raise GroupError('成员迁移需要有效管理员', status=403)
    unit = db.session.get(CampUnit, unit_id)
    if unit is None:
        raise GroupError('单元不存在', status=404)
    with db.session.begin_nested():
        members = CampUnitMember.query.filter_by(unit_id=unit.id).order_by(CampUnitMember.id).all()
        lock_project(unit, actor_user_id, [row.user_id for row in members])
        report = preview_project(unit.id)
        if report['conflicts']:
            raise GroupError('成员映射存在冲突：' + ', '.join(report['conflicts']), machine='GROUP_MIGRATION_CONFLICT')
        if unit.group_id:
            return report  # 已完成映射不覆盖之后的新业务状态。
        if report['digest'] != expected_digest:
            raise GroupError('预览后数据已变化，请重新预览', machine='GROUP_MIGRATION_STALE')
        group = CampGroup(name=unit.name, owner_user_id=unit.owner_user_id,
                                  status='archived' if unit.status == 'terminated' else 'active',
                                  version=1, created_at=unit.created_at)
        db.session.add(group)
        db.session.flush()
        for record in report['members']:
            started = datetime.fromisoformat(record['started_at'])
            ended = datetime.fromisoformat(record['ended_at']) if record['ended_at'] else None
            member = GroupMembership(group_id=group.id, user_id=record['user_id'],
                                     person_id=record['person_id'], status=record['status'],
                                     started_at=started, ended_at=ended)
            db.session.add(member)
            db.session.flush()
            # 旧表没有任命时间/任命人：不把迁移操作人伪装成历史任命人。
            # 新职责从本次承接时开始，原角色和原始参与时间保留在旧行与迁移事件中。
            if member.status == 'active':
                db.session.add(GroupRoleAssignment(group_id=group.id, membership_id=member.id,
                    role='project_leader' if record['role'] == 'leader' else 'project_member',
                    scope_type='group', scope_id=0, active_slot=1, granted_by=actor_user_id))
            db.session.add(LegacyUnitMembershipMap(legacy_member_id=record['legacy_member_id'],
                                                  membership_id=member.id, unit_id=unit.id))
        unit.group_id = group.id
        db.session.add(GroupMembershipEvent(group_id=group.id, actor_user_id=actor_user_id,
            action='legacy_mapping', context_kind='project', context_id=unit.id,
            authorization_source='migration:admin', reason='旧项目成员映射，原始关系与时间保留',
            before={'legacy_members': report['members'], 'preview_digest': expected_digest},
            after={'unit_id': unit.id, 'group_id': group.id}, group_version=1))
        db.session.flush()
        return preview_project(unit.id)

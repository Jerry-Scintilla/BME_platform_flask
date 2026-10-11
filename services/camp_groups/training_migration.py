"""旧培训营映射与恢复。服务不提交；调用方须持 maintenance.lease 至提交完成。"""
from datetime import date, datetime, time
from decimal import Decimal
import json
from sqlalchemy import select, or_
from exts import db
import models as m
from services.groups import GroupError, GovernanceDecision
from services.groups.identity import eligible_now, require_live_account
from services.groups.service import _digest
from . import maintenance

EXCLUDED = {'training_migration_run', 'training_group_profile', 'camp_teaching_event', 'camp_maintenance_fence'}


def snapshot_tables():
    tables = {t.name: t for t in db.metadata.tables.values()
              if 'camp_session_id' in t.c and t.name not in EXCLUDED}
    tables['camp_session'] = m.CampSession.__table__
    # 只沿营期专属子表补任务/材料附件；不向 User/Course/通知等全局表扩散。
    changed = True
    while changed:
        changed = False
        for table in db.metadata.tables.values():
            if table.name in tables or not table.name.startswith('camp_') or table.name in EXCLUDED:
                continue
            if any(fk.column.table.name in tables for fk in table.foreign_keys):
                tables[table.name] = table
                changed = True
    return tables


def serial(value):
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(k): serial(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [serial(v) for v in value]
    return value


def rows(table, clause):
    return [serial(dict(row)) for row in db.session.execute(
        select(table).where(clause).order_by(*table.primary_key.columns)).mappings()]


def snapshot(sid):
    tables = snapshot_tables()
    result = {}
    for name, table in tables.items():
        if name == 'camp_session':
            result[name] = rows(table, table.c.id == sid)
        elif 'camp_session_id' in table.c:
            result[name] = rows(table, table.c.camp_session_id == sid)
    pending = set(tables) - set(result)
    while pending:
        progress = False
        for name in sorted(pending):
            table = tables[name]
            fks = [fk for fk in table.foreign_keys if fk.column.table.name in result]
            if not fks:
                continue
            clauses = [fk.parent.in_([r[fk.column.name] for r in result[fk.column.table.name]]) for fk in fks]
            result[name] = rows(table, or_(*clauses))
            pending.remove(name)
            progress = True
        if not progress:
            raise GroupError('无法解析营期快照子表', machine='MIGRATION_SNAPSHOT_INCOMPLETE')
    camp = db.session.get(m.CampSession, sid)
    result['camp_policy'] = rows(m.CampPolicy.__table__, m.CampPolicy.id == camp.policy_id) if camp.policy_id else []
    return result


def facts(data, mapping=None):
    data = json.loads(json.dumps(data))
    units = set((mapping or {}).get('unit_ids', []))
    data['camp_unit'] = [r for r in data.get('camp_unit', []) if r['id'] not in units]
    for row in data['camp_session']:
        for key in ('training_groups_ready', 'training_migration_paused', 'updated_at'):
            row.pop(key, None)
    for row in data.get('camp_member', []):
        row.pop('recruitment_unit_id', None)
    for row in data.get('camp_course_assignment', []):
        row.pop('unit_id', None)
    for row in data.get('camp_meeting', []):
        row.pop('unit_id', None)
    return data


def _camp(sid, *, lock=False):
    q = m.CampSession.query.filter_by(id=sid)
    camp = q.populate_existing().with_for_update().first() if lock else q.first()
    if not camp or camp.category != 'learning' or camp.status == 'deleted':
        raise GroupError('培训营不存在或已删除', status=404)
    return camp


def _actor(uid):
    actor = db.session.get(m.UserModel, uid)
    require_live_account(actor)
    if not actor.is_admin():
        raise GroupError('旧名单迁移仅限有效管理员', status=403)
    return actor


def _lock_people(sid, actor):
    from .training import lock_users
    with m.member_history_scope():
        ids = [row.user_id for row in m.CampMember.query.filter_by(camp_session_id=sid)]
    lock_users([actor, *ids])


def _journal(run, action, actor, detail=None):
    run.journal = [*(run.journal or []), dict(action=action, actor_user_id=actor,
        at=datetime.now().isoformat(), detail=detail)]


def _run(sid, run_id, *, writing=False):
    run = db.session.get(m.TrainingMigrationRun, run_id)
    if not run or run.camp_session_id != sid:
        raise GroupError('迁移批次与营期不匹配', status=404)
    if writing:
        current = m.TrainingMigrationRun.query.filter_by(camp_session_id=sid, active_slot=1).first()
        if current and current.id != run.id:
            raise GroupError('已有更新的迁移批次，旧批次不能改变维护状态', machine='MIGRATION_RUN_STALE')
    return run


def _identity_report(sid, members):
    records, conflicts, warnings, people = [], [], [], {}
    decision = GovernanceDecision('migration:training', 'camp_join', sid)
    for member in members:
        uid = member['user_id']
        user = db.session.get(m.UserModel, uid)
        pid = user.person_id if user else None
        person = db.session.get(m.PersonModel, pid) if pid else None
        primary = db.session.get(m.PersonPrimaryAccountModel, pid) if pid else None
        record = dict(user_id=uid, person_id=pid, status=user.status if user else None,
                      lifecycle=user.lifecycle if user else None,
                      primary_user_id=primary.user_id if primary else None,
                      person_status=person.record_status if person else None,
                      verification=person.verification_status if person else None,
                      eligible=eligible_now(user, decision, appointment=member['role'] == 'mentor'))
        records.append(record)
        if member['status'] != 'active':
            continue
        if not record['eligible']:
            conflicts.append(f'identity_ineligible:{member["id"]}')
        if pid is None:
            warnings.append(f'identity_shadow_without_person:{member["id"]}')
        elif pid in people:
            conflicts.append(f'duplicate_person:{member["id"]}:{people[pid]}')
        else:
            people[pid] = member['id']
        if pid and (not primary or primary.user_id != uid):
            warnings.append(f'nonprimary_participation:{member["id"]}')
        anchor = m.CampPersonParticipationModel.query.filter_by(camp_member_id=member['id']).first()
        if anchor and (anchor.camp_session_id != sid or anchor.user_id != uid or anchor.person_id != pid):
            conflicts.append(f'person_anchor_mismatch:{member["id"]}')
    return records, conflicts, warnings


def preview(sid, decisions=None):
    """无写入：所有无法推断的责任都明确列出；decisions 仅裁定课程责任，不重写历史名单。"""
    camp = _camp(sid)
    decisions = decisions or {}
    if not isinstance(decisions, dict) or set(decisions) - {'assignment_targets'} or not isinstance(decisions.get('assignment_targets', {}), dict):
        raise GroupError('裁定格式须为 assignment_targets 对象', status=400)
    data = snapshot(sid)
    members = data['camp_member']
    identities, conflicts, warnings = _identity_report(sid, members)
    active = {r['user_id']: r for r in members if r['status'] == 'active'}
    mentors = {r['user_id']: r for r in members if r['role'] == 'mentor'}
    groups = {}
    try:
        catalog = json.loads(camp.ms_tags or '[]')
        if not isinstance(catalog, list):
            raise ValueError()
        directions = {d['name']: d.get('course_ids') or ([d['course_id']] if d.get('course_id') else []) for d in catalog if isinstance(d, dict)}
        if any(not isinstance(ids, list) or any(type(cid) is not int or cid <= 0 for cid in ids) for ids in directions.values()):
            raise ValueError()
    except (ValueError, TypeError, KeyError):
        directions = {}
        conflicts.append('invalid_direction_catalog')
    profiles = {r['user_id']: r for r in data['camp_mentor_profile']}
    for uid, member in mentors.items():
        profile = profiles.get(uid, {})
        try:
            tags = json.loads(profile.get('tags') or '[]')
            if not isinstance(tags, list) or len(tags) > 1 or any(t not in directions for t in tags):
                raise ValueError()
        except (ValueError, TypeError):
            tags = []
            conflicts.append(f'ambiguous_direction:{uid}')
        cap = profile.get('capacity')
        if cap is not None and (type(cap) is not int or not 1 <= cap <= 30):
            conflicts.append(f'invalid_capacity:{uid}')
        if member['status'] != 'active' and not any(r.get('mentor_id') == uid for r in data['camp_meeting']):
            continue
        groups[uid] = dict(mentor_id=uid, source_key=f'{sid}:{uid}', direction=tags[0] if tags else None,
                          course_ids=sorted(set(directions.get(tags[0], []) if tags else [])), capacity=cap,
                          historical=member['status'] != 'active', members=[])
        if member['status'] == 'active':
            groups[uid]['members'].append(dict(camp_member_id=member['id'], user_id=uid, role='mentor'))
    unassigned = []
    for member in active.values():
        if member['role'] not in ('mentor', 'student'):
            conflicts.append(f'unknown_legacy_role:{member["id"]}')
            continue
        if member['role'] == 'student' and member.get('learning_enabled') is False:
            conflicts.append(f'learner_flag_conflict:{member["id"]}')
        if member.get('recruitment_unit_id'):
            conflicts.append(f'already_bound_member:{member["id"]}')
        if member['role'] != 'student':
            continue
        mentor = member.get('team_mentor_id')
        if mentor is None:
            unassigned.append(member['id'])
        elif mentor not in active or mentor not in groups or mentor == member['user_id']:
            conflicts.append(f'invalid_mentor:{member["id"]}:{mentor}')
        else:
            groups[mentor]['members'].append(dict(camp_member_id=member['id'], user_id=member['user_id'], role='learner'))
    for group in groups.values():
        count = sum(r['role'] == 'learner' for r in group['members'])
        if group['capacity'] is not None and count > group['capacity']:
            warnings.append(f'legacy_over_capacity:{group["mentor_id"]}:{count}')
    for match in data['camp_mentor_match']:
        member = active.get(match['student_user_id'])
        if member and member.get('team_mentor_id') != match['mentor_user_id']:
            conflicts.append(f'match_mismatch:{match["id"]}')
    for table in ('camp_mentor_preference', 'camp_mentor_favorite'):
        for row in data[table]:
            if row['student_user_id'] in active and (row['mentor_user_id'] not in active or row['mentor_user_id'] not in groups):
                conflicts.append(f'unmapped_recruitment_target:{table}:{row["id"]}')
    assignments = []
    targets = decisions.get('assignment_targets', {})
    valid_ids = {str(row['id']) for row in data['camp_course_assignment']}
    if set(targets) - valid_ids:
        conflicts.append('unknown_assignment_decision')
    for row in data['camp_course_assignment']:
        if row.get('unit_id') is not None:
            conflicts.append(f'already_bound_assignment:{row["id"]}')
        member = active.get(row['student_user_id'])
        target = member.get('team_mentor_id') if member and member['role'] == 'student' else None
        decision = targets.get(str(row['id']))
        explicit = str(row['id']) in targets
        if explicit:
            if not isinstance(decision, dict) or set(decision) != {'mentor_id', 'reason'} or not isinstance(decision.get('reason'), str) or not decision['reason'].strip():
                conflicts.append(f'decision_reason_required:{row["id"]}')
                continue
            target = decision.get('mentor_id')
            if target is not None and type(target) is not int:
                conflicts.append(f'invalid_assignment_target:{row["id"]}')
                continue
        if row['status'] != 'active':
            target = None
        elif target is None:
            if not explicit:
                conflicts.append(f'assignment_responsibility_unknown:{row["id"]}')
        elif target not in groups or groups[target]['historical'] or not member or member.get('team_mentor_id') != target:
            conflicts.append(f'assignment_target_not_learning_group:{row["id"]}')
        elif row['course_id'] not in groups[target]['course_ids']:
            if explicit:
                groups[target]['course_ids'] = sorted(set(groups[target]['course_ids'] + [row['course_id']]))
            else:
                conflicts.append(f'assignment_course_outside_direction:{row["id"]}')
        assignments.append(dict(id=row['id'], mentor_id=target, reason=decision.get('reason') if isinstance(decision, dict) else None))
    meetings = []
    for row in data['camp_meeting']:
        if row['scope'] != 'team':
            continue
        if row.get('unit_id') is not None or row['mentor_id'] not in groups:
            conflicts.append(f'meeting_mapping_unknown:{row["id"]}')
        else:
            meetings.append(dict(id=row['id'], mentor_id=row['mentor_id']))
    if any(r['unit_type'] == 'mentor_team' and r['status'] != 'terminated' for r in data['camp_unit']):
        conflicts.append('existing_training_units')
    if camp.training_groups_ready:
        conflicts.append('already_using_training_groups')
    report = dict(camp_session_id=sid, mode='preview', groups=list(groups.values()), assignments=assignments,
        meetings=meetings, decisions=decisions, unassigned_member_ids=unassigned,
        historical_member_ids=[r['id'] for r in members if r['status'] != 'active'],
        identities=identities, conflicts=sorted(set(conflicts)), warnings=sorted(set(warnings)),
        baseline={name: {'count': len(value), 'digest': _digest(value)} for name, value in data.items()},
        facts_digest=_digest(facts(data)))
    # 维护开关和其更新时间不让暂停前后的同一份预览失效。
    report['baseline']['camp_session']['digest'] = _digest(facts(data)['camp_session'])
    report['digest'] = _digest(report)
    return report


def freeze(sid, actor_user_id):
    _actor(actor_user_id)
    camp = _camp(sid, lock=True)
    run = m.TrainingMigrationRun.query.filter_by(camp_session_id=sid, active_slot=1).first()
    if run:
        if not camp.training_migration_paused:
            camp.training_migration_paused = True
            _journal(run, 'freeze', actor_user_id)
        return run
    if camp.training_groups_ready:
        raise GroupError('新营或已切换营不能另建旧名单批次')
    run = m.TrainingMigrationRun(camp_session_id=sid, actor_user_id=actor_user_id, state='frozen')
    db.session.add(run)
    camp.training_migration_paused = True
    _journal(run, 'freeze', actor_user_id)
    db.session.flush()
    return run


def state_document(sid):
    current = snapshot(sid)
    # 只有维护状态及其更新时间不属于业务变化；ready 单独通过状态机验证。
    for row in current['camp_session']:
        for key in ('training_migration_paused', 'training_groups_ready', 'updated_at'):
            row.pop(key, None)
    gids = [row['group_id'] for row in current.get('camp_unit', []) if row.get('group_id')]
    for model in (m.CampGroup, m.GroupMembership, m.GroupRoleAssignment, m.GroupMembershipEvent, m.GroupOperation):
        table = model.__table__
        current[table.name] = rows(table, (table.c.id if model == m.CampGroup else table.c.group_id).in_(gids))
    current['training_group_profile'] = rows(m.TrainingGroupProfile.__table__, m.TrainingGroupProfile.camp_session_id == sid)
    current['camp_teaching_event'] = rows(m.CampTeachingEvent.__table__, m.CampTeachingEvent.camp_session_id == sid)
    return current


def state_digest(sid, mapping):
    return _digest(state_document(sid))


def apply_mapping(sid, run_id, *, actor_user_id, expected_digest, decisions=None):
    _actor(actor_user_id)
    _lock_people(sid, actor_user_id)
    camp = _camp(sid, lock=True)
    run = _run(sid, run_id, writing=True)
    if not camp.training_migration_paused:
        raise GroupError('须先暂停该营操作', machine='MIGRATION_NOT_FROZEN')
    if run.state in ('mapped', 'switched'):
        if run.preview_digest != expected_digest or run.mapping['decisions'] != (decisions or {}):
            raise GroupError('重试参数与本批次不一致', machine='MIGRATION_RETRY_MISMATCH')
        return run
    if run.state != 'frozen':
        raise GroupError('此批次不能执行映射')
    report = preview(sid, decisions)
    if report['conflicts']:
        raise GroupError('映射存在冲突：' + ', '.join(report['conflicts']), machine='GROUP_MIGRATION_CONFLICT')
    if report['digest'] != expected_digest:
        raise GroupError('预览后数据已变化，请重新预览', machine='GROUP_MIGRATION_STALE')
    with db.session.begin_nested():
        baseline = snapshot(sid)
        units, gids, now = {}, [], datetime.now()
        for spec in report['groups']:
            uid = spec['mentor_id']
            group = m.CampGroup(name=f'旧营导生组 #{uid}', owner_user_id=uid,
                                status='archived' if spec['historical'] else 'active', version=1)
            db.session.add(group); db.session.flush()
            unit = m.CampUnit(camp_session_id=sid, unit_type='mentor_team',
                             legacy_name=group.name, legacy_owner_user_id=uid, group_id=group.id,
                             status='terminated' if spec['historical'] else 'active')
            db.session.add(unit); db.session.flush()
            units[uid] = unit.id; gids.append(group.id)
            db.session.add(m.TrainingGroupProfile(unit_id=unit.id, camp_session_id=sid,
                direction=spec['direction'], course_ids=spec['course_ids'], capacity=spec['capacity'],
                recruitment_mentor_id=None if spec['historical'] else uid))
            if spec['historical']:
                user = db.session.get(m.UserModel, uid)
                db.session.add(m.GroupMembership(group_id=group.id, user_id=uid, person_id=user.person_id,
                    status='ended', started_at=now, ended_at=now))
            for record in spec['members']:
                user = db.session.get(m.UserModel, record['user_id'])
                membership = m.GroupMembership(group_id=group.id, user_id=user.id, person_id=user.person_id,
                                                status='active', started_at=now)
                db.session.add(membership); db.session.flush()
                db.session.add(m.GroupRoleAssignment(group_id=group.id, membership_id=membership.id,
                    role=record['role'], scope_type='group', scope_id=0, active_slot=1,
                    granted_by=actor_user_id, started_at=now))
                if record['role'] == 'learner':
                    db.session.get(m.CampMember, record['camp_member_id']).recruitment_unit_id = unit.id
            db.session.add(m.GroupMembershipEvent(group_id=group.id, actor_user_id=actor_user_id,
                action='legacy_mapping', context_kind='training', context_id=unit.id,
                authorization_source='migration:admin', reason='承接旧培训名单，旧参与时间与事实保持不变',
                before={'run_id':run.id,'source':spec}, after={'unit_id':unit.id}, group_version=1))
        for record in report['assignments']:
            db.session.get(m.CampCourseAssignment, record['id']).unit_id = units.get(record['mentor_id'])
        for record in report['meetings']:
            meeting = db.session.get(m.CampMeeting, record['id'])
            db.session.execute(m.CampMeeting.__table__.update().where(m.CampMeeting.id == meeting.id).values(
                unit_id=units[record['mentor_id']], updated_at=meeting.updated_at))
            db.session.expire(meeting)
        db.session.flush()
        mapping = dict(unit_ids=list(units.values()), group_ids=gids, mentor_units={str(k):v for k,v in units.items()},
                       decisions=decisions or {}, report=report)
        if _digest(facts(snapshot(sid), mapping)) != _digest(facts(baseline)):
            raise GroupError('映射改变了受保护的旧教学事实', machine='MIGRATION_BASELINE_CHANGED')
        run.baseline, run.mapping, run.preview_digest = baseline, mapping, expected_digest
        run.state = 'mapped'
        _journal(run, 'map', actor_user_id, {'digest':expected_digest})
        db.session.flush()
        run.mapped_digest = state_digest(sid, mapping)
        db.session.flush()
    return run


def verify(sid, run_id):
    camp = _camp(sid)
    run = _run(sid, run_id)
    if not run.mapping:
        return dict(run_id=run.id, state=run.state, paused=camp.training_migration_paused, mapped=False)
    current = snapshot(sid)
    baseline_equal = _digest(facts(current, run.mapping)) == _digest(facts(run.baseline))
    unchanged = state_digest(sid, run.mapping) == run.mapped_digest
    _, identity_conflicts, _ = _identity_report(sid, current['camp_member'])
    return dict(run_id=run.id, state=run.state, paused=camp.training_migration_paused,
                ready=camp.training_groups_ready, mapped=True, baseline_equal=baseline_equal,
                unchanged_since_mapping=unchanged, identity_conflicts=identity_conflicts,
                recovery_allowed=unchanged and baseline_equal and run.state in ('mapped', 'switched')
                    and camp.training_groups_ready == (run.state == 'switched'))


def switch(sid, run_id, actor_user_id):
    _actor(actor_user_id)
    _lock_people(sid, actor_user_id)
    camp = _camp(sid, lock=True); run = _run(sid, run_id, writing=True)
    if run.state == 'switched' and camp.training_groups_ready:
        return run
    if run.state != 'mapped' or not camp.training_migration_paused or camp.training_groups_ready:
        raise GroupError('切换要求映射完成且仍处于暂停状态')
    check = verify(sid, run_id)
    if not check['recovery_allowed'] or check['identity_conflicts']:
        raise GroupError('映射核对失败，不允许切换', machine='MIGRATION_VERIFICATION_FAILED')
    camp.training_groups_ready = True
    run.state = 'switched'; _journal(run, 'switch', actor_user_id)
    db.session.flush()
    return run


def resume(sid, run_id, actor_user_id):
    _actor(actor_user_id)
    camp = _camp(sid, lock=True); run = _run(sid, run_id, writing=True)
    if run.state == 'frozen':
        run.state, run.active_slot = 'cancelled', None
    elif run.state not in ('switched', 'restored', 'cancelled'):
        raise GroupError('映射完成后必须先切换或恢复，不能带着半完成状态开放写入')
    if camp.training_migration_paused:
        camp.training_migration_paused = False
        _journal(run, 'resume', actor_user_id)
    db.session.flush()
    return run


def restore(sid, run_id, actor_user_id):
    _actor(actor_user_id)
    camp = _camp(sid, lock=True); run = _run(sid, run_id, writing=True)
    if run.state == 'restored':
        return run
    if not camp.training_migration_paused:
        raise GroupError('恢复前须再次暂停该营', machine='MIGRATION_NOT_FROZEN')
    if not verify(sid, run_id).get('recovery_allowed'):
        raise GroupError('映射后已有业务变化；保留现场，须修复前进或另行制定数据恢复方案', machine='MIGRATION_FORWARD_ONLY')
    with db.session.begin_nested():
        for record in run.baseline['camp_member']:
            with m.member_history_scope():
                member = db.session.get(m.CampMember, record['id'])
            member.recruitment_unit_id = record['recruitment_unit_id']
        for record in run.baseline['camp_course_assignment']:
            db.session.get(m.CampCourseAssignment, record['id']).unit_id = record['unit_id']
        for record in run.baseline['camp_meeting']:
            meeting = db.session.get(m.CampMeeting, record['id'])
            db.session.execute(m.CampMeeting.__table__.update().where(m.CampMeeting.id == meeting.id).values(
                unit_id=record['unit_id'], updated_at=datetime.fromisoformat(record['updated_at']) if record['updated_at'] else None))
            db.session.expire(meeting)
        now = datetime.now()
        for gid in run.mapping['group_ids']:
            group = db.session.get(m.CampGroup, gid)
            group.status = 'archived'; group.version += 1
            for role in m.GroupRoleAssignment.query.filter_by(group_id=gid, active_slot=1):
                role.active_slot, role.ended_at = None, now
            for member in m.GroupMembership.query.filter_by(group_id=gid, status='active'):
                member.status, member.ended_at = 'ended', now
            db.session.add(m.GroupMembershipEvent(group_id=gid, actor_user_id=actor_user_id,
                action='mapping_restore', context_kind='training', context_id=sid,
                authorization_source='migration:admin', reason='业务变化前撤回旧营映射，保留迁移档案',
                before={'run_id':run.id}, after={'status':'archived'}, group_version=group.version))
        for uid in run.mapping['unit_ids']:
            db.session.get(m.CampUnit, uid).status = 'terminated'
            profile = db.session.get(m.TrainingGroupProfile, uid)
            db.session.delete(profile)
        camp.training_groups_ready = False
        run.state, run.active_slot = 'restored', None
        _journal(run, 'restore', actor_user_id)
        db.session.flush()
    return run

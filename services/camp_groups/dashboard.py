"""管理老师的项目营只读投影；完成状态复用交付服务，归档营读取冻结事实。"""
import hashlib
import json
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo
from sqlalchemy.orm import joinedload

from exts import db
from models import (CampUnit, CampMember, CampMilestone, CampNodeEvaluation,
                    CampSubmissionVersion, CampSubmissionAttachment, CampOutcome,
                    CampArchive, ProjectTemplate, UserModel)
from .reads import UnitMembershipRead
from .delivery import progress, pending_reviews


def _iso(value):
    return value.isoformat() if value else None


def _group(rows, key):
    out = defaultdict(list)
    for row in rows:
        out[getattr(row, key)].append(row)
    return out


def _node(milestone, unit, members, evaluations, submissions, review_ids, today):
    state = progress(milestone, unit, members, evaluations, submissions)
    targets = ({unit.owner_user_id} if state['completion_mode'] == 'leader_review' else
               {r.user_id for r in members if r.user_id != unit.owner_user_id})
    latest = state['latest_submissions']
    evaluated = {e.member_user_id for e in state['evaluations']}
    submitted = {uid for uid in targets if uid in latest and latest[uid].status in ('submitted', 'approved')}
    live = unit.status == 'active'
    return {
        'id': milestone.id, 'title': milestone.title, 'order_no': milestone.order_no,
        'due_date': _iso(milestone.due_date), 'status': state['status'],
        'node_complete': state['node_complete'], 'completion_mode': state['completion_mode'],
        'overdue': bool(live and not state['node_complete'] and milestone.due_date and milestone.due_date < today),
        'required_count': len(targets), 'submitted_count': len(submitted),
        'evaluated_count': state['evaluated_count'],
        'pending_evaluations': len(submitted - evaluated) if live and state['completion_mode'] == 'member_evaluation' else 0,
        'pending_reviews': sum(s.id in review_ids for s in latest.values()) if live else 0,
        'returned_count': sum(uid in latest and latest[uid].status == 'returned' for uid in targets),
    }, state


def _finish_project(project, today, archived=False):
    nodes = project['nodes']
    total = len(nodes)
    completed = sum(n['node_complete'] for n in nodes)
    project.update(node_count=total, completed_nodes=completed,
                   completion_rate=round(100 * completed / total, 1) if total else None,
                   overdue_nodes=sum(n['overdue'] for n in nodes),
                   pending_reviews=sum(n['pending_reviews'] for n in nodes),
                   pending_evaluations=sum(n['pending_evaluations'] for n in nodes),
                   configured=bool(total))
    remaining = [n for n in nodes if not n['node_complete']]
    project['current_node'] = remaining[0]['title'] if remaining else ('全部完成' if total else '未配置节点')
    deadlines = [n['due_date'] for n in remaining if n['due_date']]
    project['next_due_date'] = min(deadlines) if deadlines else None
    project['due_soon'] = bool(not archived and project['status'] == 'active' and deadlines and
                               0 <= (datetime.fromisoformat(min(deadlines)).date() - today).days <= 7)
    project['risks'] = []
    if not total:
        project['risks'].append('unconfigured')
    if not project['leader_active']:
        project['risks'].append('leader_missing')
    if project['overdue_nodes']:
        project['risks'].append('overdue')
    if project['pending_reviews'] or project['pending_evaluations']:
        project['risks'].append('pending')
    if project['due_soon']:
        project['risks'].append('due_soon')
    if project['pending_outcomes'] and not archived and project['status'] == 'active':
        project['risks'].append('outcome_pending')


def _columns(projects):
    columns = {}
    for project in projects:
        occurrences = defaultdict(int)
        for node in project['nodes']:
            title = node['title'].strip()
            occurrences[title] += 1
            occurrence = occurrences[title]
            key = hashlib.sha256(f'{title}\0{occurrence}'.encode()).hexdigest()[:20]
            node['stage_key'] = key
            if key not in columns:
                columns[key] = {'key': key, 'title': title if occurrence == 1 else f'{title}（{occurrence}）',
                                'order': node['order_no']}
    return sorted(columns.values(), key=lambda c: c['order'])


def _live_projects(camp, today, detail_key):
    units = (CampUnit.query.options(joinedload(CampUnit.camp_group))
             .filter_by(camp_session_id=camp.id, unit_type='project').order_by(CampUnit.id).all())
    ids = [u.id for u in units]
    if detail_key is not None and str(detail_key) not in {str(uid) for uid in ids}:
        raise LookupError('项目不属于本营期')
    if detail_key is not None:
        units = [u for u in units if str(u.id) == detail_key]
        ids = [u.id for u in units]
    if not ids:
        return [], CampMember.query.filter_by(camp_session_id=camp.id, status='active').count()
    members = UnitMembershipRead.query.filter(UnitMembershipRead.unit_id.in_(ids), UnitMembershipRead.status == 'active').all()
    members_by_unit = _group(members, 'unit_id')
    milestones = CampMilestone.query.filter(CampMilestone.unit_id.in_(ids)).order_by(CampMilestone.order_no, CampMilestone.id).all()
    ms_by_unit = _group(milestones, 'unit_id')
    ms_ids = [m.id for m in milestones]
    evaluations = CampNodeEvaluation.query.filter(CampNodeEvaluation.milestone_id.in_(ms_ids)).all() if ms_ids else []
    # SQL 排除旧版本，矩阵不会加载历史版本链或正文。
    from sqlalchemy.orm import aliased
    newer = aliased(CampSubmissionVersion)
    submissions_query = CampSubmissionVersion.query.filter(
        CampSubmissionVersion.milestone_id.in_(ms_ids),
        ~db.session.query(newer.id).filter(newer.milestone_id == CampSubmissionVersion.milestone_id,
            newer.submitted_by == CampSubmissionVersion.submitted_by,
            newer.version > CampSubmissionVersion.version).exists())
    if detail_key is None:
        from sqlalchemy.orm import load_only
        submissions_query = submissions_query.options(load_only(CampSubmissionVersion.id, CampSubmissionVersion.milestone_id,
            CampSubmissionVersion.submitted_by, CampSubmissionVersion.version, CampSubmissionVersion.status,
            CampSubmissionVersion.created_at))
    submissions = submissions_query.all() if ms_ids else []
    evaluations_by_ms = _group(evaluations, 'milestone_id')
    submissions_by_ms = _group(submissions, 'milestone_id')
    outcomes = CampOutcome.query.filter(CampOutcome.unit_id.in_(ids)).all()
    outcomes_by_unit = _group(outcomes, 'unit_id')
    templates = {t.unit_id: t for t in ProjectTemplate.query.filter(ProjectTemplate.unit_id.in_(ids)).all()}
    names = {u.id: u.username for u in UserModel.query.filter(UserModel.id.in_(
        {u.owner_user_id for u in units} | {r.user_id for r in members})).all()}
    review_ids = {r[0] for r in pending_reviews().filter(CampUnit.camp_session_id == camp.id)
                  .with_entities(CampSubmissionVersion.id).all()}
    attachments = defaultdict(list)
    if detail_key is not None:
        detail_ms = {m.id for m in ms_by_unit[int(detail_key)]}
        sub_ids = [s.id for s in submissions if s.milestone_id in detail_ms]
        for a in CampSubmissionAttachment.query.filter(CampSubmissionAttachment.submission_id.in_(sub_ids)).all() if sub_ids else []:
            attachments[a.submission_id].append({'id': a.id, 'filename': a.filename, 'size': a.size})
    projects = []
    for unit in units:
        roster = members_by_unit[unit.id]
        project = {'key': str(unit.id), 'unit_id': unit.id, 'name': unit.name, 'status': unit.status,
                   'leader_user_id': unit.owner_user_id, 'leader_name': names.get(unit.owner_user_id, '未指定'),
                   'leader_active': any(r.user_id == unit.owner_user_id and r.role == 'leader' for r in roster),
                   'member_count': len(roster), 'members': [{'user_id': r.user_id, 'name': names.get(r.user_id, str(r.user_id)), 'role': r.role} for r in roster],
                   'template_name': templates[unit.id].name if unit.id in templates else None,
                   'verified_outcomes': sum(o.status == 'verified' for o in outcomes_by_unit[unit.id]),
                   'pending_outcomes': sum(o.status == 'submitted' for o in outcomes_by_unit[unit.id]),
                   'last_activity_at': _iso(max([v for m in ms_by_unit[unit.id] for v in [m.updated_at]
                        if v] + [s.created_at for m in ms_by_unit[unit.id] for s in submissions_by_ms[m.id] if s.created_at]
                        + [e.updated_at for m in ms_by_unit[unit.id] for e in evaluations_by_ms[m.id] if e.updated_at], default=None)),
                   'nodes': []}
        for milestone in ms_by_unit[unit.id]:
            node, state = _node(milestone, unit, roster, evaluations_by_ms[milestone.id], submissions_by_ms[milestone.id], review_ids, today)
            if detail_key == str(unit.id):
                node.update(description=milestone.description, requirement=milestone.requirement,
                            evaluations=[{'name': names.get(e.member_user_id, str(e.member_user_id)), 'score': e.score, 'comment': e.comment} for e in state['evaluations']],
                            submissions=[{'id': s.id, 'name': names.get(s.submitted_by, str(s.submitted_by)), 'version': s.version,
                                'status': s.status, 'content': s.content, 'created_at': _iso(s.created_at), 'review_note': s.review_note,
                                'attachments': attachments[s.id]} for s in state['latest_submissions'].values() if s.submitted_by in {r.user_id for r in roster}])
            project['nodes'].append(node)
        if detail_key == str(unit.id):
            project['outcomes'] = [{'title': o.title, 'description': o.description, 'status': o.status} for o in outcomes_by_unit[unit.id]]
        _finish_project(project, today)
        projects.append(project)
    return projects, CampMember.query.filter_by(camp_session_id=camp.id, status='active').count()


def _archived_projects(archive, today, detail_key):
    snapshot = json.loads(archive.snapshot or '{}')
    names = {member['user_id']: member['user'] for unit in snapshot.get('units', [])
             for member in unit.get('members', []) if member.get('user_id')}
    projects = []
    for index, unit in enumerate(snapshot.get('units', [])):
        roster = [m for m in unit.get('members', []) if m.get('status') == 'active']
        project = {'key': f'archive-{index}', 'unit_id': unit.get('unit_id'), 'name': unit['name'], 'status': unit.get('status', 'active'),
                   'leader_user_id': unit.get('leader_user_id'), 'leader_name': unit.get('leader', '未指定'),
                   'leader_active': any(m.get('role') == 'leader' for m in roster), 'member_count': len(roster),
                   'members': [{'name': m.get('user', '—'), 'role': m.get('role')} for m in roster],
                   'template_name': (unit.get('template') or {}).get('name'), 'last_activity_at': _iso(archive.frozen_at),
                   'verified_outcomes': len(unit.get('verified_outcomes', [])), 'pending_outcomes': 0, 'nodes': []}
        for order, node in enumerate(unit.get('milestones', []), 1):
            complete = node.get('node_complete', node.get('status') == 'approved')
            project['nodes'].append({'id': node.get('id'), 'title': node['title'], 'order_no': order,
                'due_date': node.get('due_date'), 'node_complete': complete, 'status': node.get('status', 'open'),
                'completion_mode': node.get('completion_mode'), 'overdue': False, 'pending_reviews': 0, 'pending_evaluations': 0,
                'required_count': None, 'submitted_count': None, 'evaluated_count': len(node.get('evaluations', [])),
                'returned_count': 0, 'evaluations': [dict(e, name=names.get(e.get('member_user_id'), f"成员 {e.get('member_user_id')}")) for e in node.get('evaluations', [])] if detail_key == project['key'] else [],
                'final_versions': node.get('final_versions', node.get('approved_versions', [])) if detail_key == project['key'] else []})
        if detail_key == project['key']:
            project['outcomes'] = [dict(o, status='verified') for o in unit.get('verified_outcomes', [])]
        _finish_project(project, today, archived=True)
        projects.append(project)
    if detail_key is not None and detail_key not in {p['key'] for p in projects}:
        raise LookupError('项目不属于本营期档案')
    return projects, len(snapshot.get('members', []))


def build_dashboard(camp, detail_key=None):
    now = datetime.now(ZoneInfo('Asia/Shanghai'))
    archive = CampArchive.query.filter_by(camp_session_id=camp.id).first() if camp.status == 'archived' else None
    if camp.status == 'archived' and not archive:
        raise ValueError('本营已结营但缺少冻结档案，暂时无法展示历史进度')
    projects, people = (_archived_projects(archive, now.date(), detail_key) if archive else
                        _live_projects(camp, now.date(), detail_key))
    columns = _columns(projects)
    if detail_key is not None:
        return {'project': next(p for p in projects if p['key'] == detail_key), 'archived': bool(archive)}
    total = sum(p['node_count'] for p in projects)
    completed = sum(p['completed_nodes'] for p in projects)
    assigned = sum(p['member_count'] for p in projects)
    summary = {'project_count': len(projects), 'participant_count': people, 'project_assignments': assigned,
               'total_nodes': total, 'completed_nodes': completed,
               'completion_rate': round(100 * completed / total, 1) if total else None,
               'completed_projects': sum(p['configured'] and p['completed_nodes'] == p['node_count'] for p in projects),
               'unconfigured_projects': sum(not p['configured'] for p in projects),
               'overdue_projects': sum(bool(p['overdue_nodes']) for p in projects),
               'overdue_nodes': sum(p['overdue_nodes'] for p in projects),
               'pending_reviews': sum(p['pending_reviews'] for p in projects),
               'pending_evaluations': sum(p['pending_evaluations'] for p in projects),
               'verified_outcomes': sum(p['verified_outcomes'] for p in projects),
               'pending_outcomes': sum(p['pending_outcomes'] for p in projects)}
    return {'camp_id': camp.id, 'camp_name': camp.name, 'status': camp.status, 'as_of': now.isoformat(),
            'archived': bool(archive), 'frozen_at': _iso(archive.frozen_at) if archive else None,
            'summary': summary, 'columns': columns, 'projects': projects}

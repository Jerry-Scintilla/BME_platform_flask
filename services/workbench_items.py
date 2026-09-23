"""管理工作台只读待办投影；业务状态和审批仍由原领域负责。"""
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, and_, cast, func, literal, or_, union_all

from exts import db
from models import (
    CampJoinRequest, CampLeave, CampMilestone, CampStaff, CampSubmissionVersion,
    CampUnit, CampSession, FeedbackTicket, LLMQuotaRequestModel,
    ProjectApplicationVersion, UserModel,
)


TYPES = (
    'camp_join', 'camp_leave', 'project_application', 'project_delivery',
    'quota_request', 'feedback_ticket',
)
CAMP_TYPES = TYPES[:4]


def _source(kind, model, title, camp_id, created_at, *, assignee=None, priority=None,
            variant=None):
    return db.session.query(
        literal(kind).label('type'),
        model.id.label('source_id'),
        title.label('title'),
        camp_id.label('camp_id'),
        (assignee if assignee is not None else literal(None, type_=Integer)).label('assignee_user_id'),
        created_at.label('created_at'),
        cast(literal(None), DateTime).label('due_at'),
        (priority if priority is not None else literal('normal')).label('priority'),
        (variant if variant is not None else literal(None, type_=String(20))).label('variant'),
    )


def _pending_union():
    join = _source('camp_join', CampJoinRequest, literal('营期加入申请'),
                   CampJoinRequest.camp_session_id, CampJoinRequest.created_at,
                   variant=CampJoinRequest.apply_role).filter(CampJoinRequest.status == 'pending')
    leave = _source('camp_leave', CampLeave, literal('营期请假申请'),
                    CampLeave.camp_session_id, CampLeave.created_at).filter(CampLeave.status == 'pending')
    application = _source('project_application', ProjectApplicationVersion,
                          ProjectApplicationVersion.name, ProjectApplicationVersion.camp_session_id,
                          ProjectApplicationVersion.created_at).filter(
                              ProjectApplicationVersion.status == 'pending')
    delivery = (_source('project_delivery', CampSubmissionVersion, CampMilestone.title,
                        CampUnit.camp_session_id, CampSubmissionVersion.created_at)
                .join(CampMilestone, CampSubmissionVersion.milestone_id == CampMilestone.id)
                .join(CampUnit, CampMilestone.unit_id == CampUnit.id)
                .filter(CampSubmissionVersion.status == 'submitted',
                        CampUnit.unit_type == 'project',
                        or_(CampMilestone.submit_mode == 'team',
                            and_(CampMilestone.submit_mode == 'member',
                                 CampSubmissionVersion.submitted_by == CampUnit.owner_user_id))))
    quota = _source('quota_request', LLMQuotaRequestModel, literal('API 配额申请'),
                    literal(None, type_=Integer), LLMQuotaRequestModel.created_at).filter(
                        LLMQuotaRequestModel.status == LLMQuotaRequestModel.STATUS_PENDING)
    tickets = _source('feedback_ticket', FeedbackTicket, FeedbackTicket.title,
                      literal(None, type_=Integer), FeedbackTicket.created_at,
                      assignee=FeedbackTicket.assignee_user_id,
                      priority=FeedbackTicket.priority).filter(
                          FeedbackTicket.status.in_(['new', 'triaged', 'reopened']))
    return union_all(*(q.statement for q in (join, leave, application, delivery, quota, tickets))).subquery()


def _target(row):
    sid, source_id = row.camp_id, row.source_id
    if row.type == 'camp_join':
        if row.variant == 'mentor':
            return f'/camps/{sid}/learning/mentor-matching', {'focus': source_id}
        return f'/camps/{sid}/people/applications', {'focus': source_id}
    if row.type == 'camp_leave':
        return f'/camps/{sid}/operations/leaves', {'status': 'pending', 'focus': source_id}
    if row.type == 'project_application':
        return f'/camps/{sid}/project/applications', {'focus': source_id}
    if row.type == 'project_delivery':
        return f'/camps/{sid}/project/deliveries', {'focus': source_id}
    if row.type == 'quota_request':
        return '/api-platform/quota-requests', {'status': 'pending', 'focus': source_id}
    return f'/operations/feedback-tickets/{source_id}', {}


def list_workbench_items(*, user_id, scope='all', item_type=None, camp_id=None,
                         page=1, page_size=20):
    """SQL 侧合并、筛选、分页；未知时效不制造逾期待办。"""
    items = _pending_union()
    owners = (db.session.query(CampStaff.camp_session_id.label('camp_id'),
                               func.min(CampStaff.user_id).label('owner_user_id'))
              .filter(CampStaff.status == 'active', CampStaff.role == 'owner')
              .group_by(CampStaff.camp_session_id).subquery())
    q = (db.session.query(*items.c, owners.c.owner_user_id)
         .outerjoin(owners, items.c.camp_id == owners.c.camp_id))
    if item_type:
        q = q.filter(items.c.type == item_type)
    if camp_id:
        q = q.filter(items.c.camp_id == camp_id)
    camp_item = items.c.type.in_(CAMP_TYPES)
    if scope == 'mine':
        q = q.filter(or_(and_(camp_item, owners.c.owner_user_id == user_id),
                         and_(items.c.type == 'feedback_ticket',
                              items.c.assignee_user_id == user_id)))
    elif scope == 'unassigned':
        q = q.filter(or_(and_(camp_item, owners.c.owner_user_id.is_(None)),
                         and_(items.c.type == 'feedback_ticket',
                              items.c.assignee_user_id.is_(None))))
    elif scope == 'overdue':
        q = q.filter(items.c.due_at.isnot(None), items.c.due_at < datetime.now())

    total = q.count()
    rows = (q.order_by(items.c.created_at.asc(), items.c.type.asc(), items.c.source_id.asc())
            .offset((page - 1) * page_size).limit(page_size).all())
    camp_ids = {row.camp_id for row in rows if row.camp_id is not None}
    user_ids = {uid for row in rows for uid in (row.assignee_user_id, row.owner_user_id) if uid}
    camps = {camp.id: camp.name for camp in CampSession.query.filter(CampSession.id.in_(camp_ids)).all()} if camp_ids else {}
    users = {user.id: user.username for user in UserModel.query.filter(UserModel.id.in_(user_ids)).all()} if user_ids else {}
    result = []
    for row in rows:
        route, query = _target(row)
        title = '导生报名申请' if row.type == 'camp_join' and row.variant == 'mentor' else row.title
        result.append({
            'key': f'{row.type}:{row.source_id}', 'type': row.type,
            'source_id': row.source_id, 'title': title,
            'camp_id': row.camp_id, 'camp_name': camps.get(row.camp_id),
            'owner_user_id': row.owner_user_id if row.type in CAMP_TYPES else None,
            'assignee_user_id': row.assignee_user_id,
            'responsible_name': users.get(row.assignee_user_id or row.owner_user_id),
            'created_at': row.created_at.isoformat() if row.created_at else None,
            'due_at': None, 'status': 'pending', 'priority': row.priority,
            'target_route': route, 'target_query': query,
        })
    return {'items': result, 'total': total, 'page': page, 'page_size': page_size,
            'as_of': datetime.now().isoformat(timespec='seconds'),
            'due_policy': 'not_configured'}

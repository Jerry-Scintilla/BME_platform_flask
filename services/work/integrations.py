"""内部工作台·业务关联只读投影（设计方案 §11.3，M5）。

三条规则：关联不授予原业务权限；完成内部任务不自动改变原业务；原有待办保留
原流程。source_type 白名单（course/camp_session/feedback_ticket），服务层验证
目标存在；无原对象访问权时只回「不可访问」占位，不复制标题（防越权泄露）。
"""
from exts import db
from models import WorkBusinessLink, WorkItem
from services.work import access, projections
from services.work.access import WorkApiError

# 适配器组装保证注册表就绪（blueprints/__init__ 全量加载链）
import services.work.adapters  # noqa: F401


def create_link(user, item_id, payload):
    """建立关联（作者∨协调员）：白名单 + 目标存在性校验。"""
    item = WorkItem.query.filter_by(id=item_id).first()
    item, item_access = access.require_read(user, item)
    if not (item.created_by == user.id or item_access.is_coordinator):
        raise WorkApiError(403, '仅作者或本组协调员可以建立业务关联')
    source_type = payload.get('source_type')
    provider = projections.get_provider(source_type)
    if provider is None:
        raise projections.unknown_source_error()
    try:
        source_id = int(payload.get('source_id'))
    except (TypeError, ValueError):
        raise WorkApiError(400, '缺少 source_id')
    if not provider.exists(source_id):
        raise WorkApiError(404, '关联对象不存在')
    existed = WorkBusinessLink.query.filter_by(
        item_id=item.id, source_type=source_type, source_id=source_id).first()
    if existed:
        raise WorkApiError(409, '该关联已存在')
    link = WorkBusinessLink(item_id=item.id, source_type=source_type,
                            source_id=source_id, created_by=user.id)
    db.session.add(link)
    return link


def remove_link(user, item_id, payload):
    """移除业务关联（作者∨协调员）。"""
    item = WorkItem.query.filter_by(id=item_id).first()
    item, item_access = access.require_read(user, item)
    if not (item.created_by == user.id or item_access.is_coordinator):
        raise WorkApiError(403, '仅作者或本组协调员可以移除业务关联')
    source_type = payload.get('source_type')
    try:
        source_id = int(payload.get('source_id'))
    except (TypeError, ValueError):
        raise WorkApiError(400, '缺少 source_id')
    link = WorkBusinessLink.query.filter_by(
        item_id=item.id, source_type=source_type, source_id=source_id).first()
    if not link:
        raise WorkApiError(404, '该关联不存在')
    db.session.delete(link)
    return link


def project_for(user, item_id):
    """事项的业务关联安全投影（注册表驱动，X2 通用化）。

    输出 {source_type, source_id, label, accessible, fields}：fields 为适配器
    summarize 的安全字段字典（盲渲染，核心不懂语义）；不可访问回 accessible=False
    且不带任何字段。无 provider 的历史类型按不可访问处理（防泄露）。"""
    links = WorkBusinessLink.query.filter_by(item_id=item_id).all()
    out = []
    for link in links:
        provider = projections.get_provider(link.source_type)
        entry = {'source_type': link.source_type, 'source_id': link.source_id,
                 'relation': link.relation, 'label': provider.label if provider else link.source_type,
                 'accessible': False, 'fields': None}
        if provider is not None:
            fields = provider.summarize(user, link.source_id)
            if fields is not None:
                entry['accessible'] = True
                entry['fields'] = fields
        out.append(entry)
    return out


# ── 治理视图（M5）：交接清单与紧急介入 ──────────────────────

def handover_overview(user_id):
    """交接清单（§5.4）：未完成任务/待验收/待回复/参与事项/失效授权。

    事项批量预取（#33/#43：join 取双实体 + 一次 in_，去掉逐行 get 与空模型兜底）。"""
    from models import WorkAccessGrant, WorkResponseRequest, WorkTask, WorkTransferRequest

    active_task_statuses = ('todo', 'in_progress', 'blocked', 'review')
    task_rows = (db.session.query(WorkTask, WorkItem)
                 .join(WorkItem, WorkItem.id == WorkTask.item_id)
                 .filter(WorkItem.status.in_(active_task_statuses),
                         (WorkTask.assignee_user_id == user_id)
                         | (WorkTask.reviewer_user_id == user_id)).all())
    unfinished, to_review = [], []
    for t, item in task_rows:
        row = {'item_id': t.item_id, 'title': item.title,
               'status': item.status,
               'role': 'assignee' if t.assignee_user_id == user_id else 'reviewer',
               'due_at': t.due_at.strftime('%Y-%m-%d %H:%M') if t.due_at else None}
        (to_review if row['role'] == 'reviewer' and item.status == 'review'
         else unfinished).append(row)

    extra_ids = set()
    for q in (WorkResponseRequest.query.filter_by(
                responder_user_id=user_id, status='pending'),
              WorkTransferRequest.query.filter_by(
                to_user_id=user_id, status='pending')):
        extra_ids.update(r.item_id for r in q.all())
    items_map = {i.id: i for i in WorkItem.query.filter(
        WorkItem.id.in_(list(extra_ids))).all()} if extra_ids else {}

    pending_responses = [{
        'request_id': r.id, 'item_id': r.item_id,
        'title': items_map[r.item_id].title if r.item_id in items_map else None,
        'due_at': r.due_at.strftime('%Y-%m-%d %H:%M') if r.due_at else None,
    } for r in WorkResponseRequest.query.filter_by(
        responder_user_id=user_id, status='pending').all()]

    pending_transfers = [{
        'transfer_id': t.id, 'item_id': t.item_id,
        'title': items_map[t.item_id].title if t.item_id in items_map else None,
        'expires_at': t.expires_at.strftime('%Y-%m-%d %H:%M'),
    } for t in WorkTransferRequest.query.filter_by(
        to_user_id=user_id, status='pending').all()]

    from services.work.access import grant_status_reason
    grants = WorkAccessGrant.query.filter_by(user_id=user_id).all()
    grant_rows = [{'id': g.id, 'role': g.role, 'workspace_id': g.workspace_id,
                   'effective': grant_status_reason(g)[0],
                   'reason': grant_status_reason(g)[1] or None} for g in grants]

    return {
        'unfinished_tasks': unfinished,
        'to_review': to_review,
        'pending_responses': pending_responses,
        'pending_transfers': pending_transfers,
        'grants': grant_rows,
        'counts': {
            'unfinished': len(unfinished), 'to_review': len(to_review),
            'pending_responses': len(pending_responses),
            'pending_transfers': len(pending_transfers),
        },
    }


def emergency_access(user, item_id, reason):
    """治理紧急介入读取受限事项（§5.4）：reason 必填 + emergency_access 事件留痕。"""
    from services.work.events import record_event
    if not reason or not str(reason).strip():
        raise WorkApiError(400, '紧急介入必须填写理由')
    item = (WorkItem.query.filter_by(id=item_id).with_for_update().first())
    if not item:
        raise WorkApiError(404, '事项不存在')
    record_event(item, 'emergency_access', actor_user_id=user.id,
                 diff={'by': 'governance'}, reason=str(reason).strip()[:200])
    # 读取走常规序列化，但绕过对象权限（介入已被记录）
    from services.work.items import get_item_detail_for_governance
    return get_item_detail_for_governance(user, item)

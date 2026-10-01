"""内部工作台·通用看板聚合（跨组方案 §4.3 通用化，X2）。

工作区「看板」= 按关联对象聚合本组事项态势——对工作台自身数据的聚合，
天然通用（不知道对象背后是什么业务）。行集 = 认领的对象 ∪ 本工作区事项
引用过的对象（未认领行提示协调员认领）；每行 = 适配器安全摘要（盲渲染）
+ 事项态势（未完结/逾期/最近活动）。

红线：只读聚合；安全摘要走注册表（None=受限占位）；域内统计留在业务模块。
"""
from datetime import datetime

from exts import db
from models import WorkBusinessLink, WorkItem, WorkObjectClaim, WorkTask
from services.work import access, projections
from services.work.access import WorkApiError

# 与摘要层同口径：话题进行中 + 任务未完结
ACTIVE_STATUSES = ('open', 'todo', 'in_progress', 'blocked', 'review')


def claim_object(user, payload):
    """认领/取消认领（幂等）：仅协调员；仅 stewardable 类型可认领。"""
    ws = access.require_workspace(user, payload.get('ws_id'), roles=('coordinator',))
    source_type = payload.get('source_type')
    provider = projections.get_provider(source_type)
    if provider is None:
        raise projections.unknown_source_error()
    if not provider.stewardable:
        raise WorkApiError(400, f'{provider.label}不支持认领（流程性对象无维护责任归属）')
    try:
        source_id = int(payload.get('source_id'))
    except (TypeError, ValueError):
        raise WorkApiError(400, '缺少 source_id')
    if not provider.exists(source_id):
        raise WorkApiError(404, '对象不存在')
    action = payload.get('action', 'claim')

    existed = WorkObjectClaim.query.filter_by(
        source_type=source_type, source_id=source_id, workspace_id=ws.id).first()
    if action == 'claim':
        if existed:
            return existed, False               # 幂等
        row = WorkObjectClaim(source_type=source_type, source_id=source_id,
                              workspace_id=ws.id, claimed_by=user.id)
        db.session.add(row)
        return row, True
    if action == 'unclaim':
        if existed:
            db.session.delete(existed)
        return None, existed is not None
    raise WorkApiError(400, 'action 仅支持 claim/unclaim')


def workspace_board(user, ws_id):
    """看板行集（工作区成员可见）。"""
    ws = access.require_workspace(user, ws_id)

    # 事项态势：本工作区各关联对象的 active 事项聚合（一次查询，内存归组）
    now = datetime.now()
    rows = (db.session.query(WorkBusinessLink, WorkItem, WorkTask)
            .outerjoin(WorkItem, WorkItem.id == WorkBusinessLink.item_id)
            .outerjoin(WorkTask, WorkTask.item_id == WorkItem.id)
            .filter(WorkItem.workspace_id == ws.id)
            .all())
    stats = {}          # (source_type, source_id) → {active, overdue, last_activity}
    for link, item, task in rows:
        key = (link.source_type, link.source_id)
        st = stats.setdefault(key, {'active': 0, 'overdue': 0, 'last_activity': None})
        if item.status in ACTIVE_STATUSES:
            st['active'] += 1
            if task is not None and task.due_at and task.due_at < now:
                st['overdue'] += 1
        if item.last_activity_at and (st['last_activity'] is None
                                      or item.last_activity_at > st['last_activity']):
            st['last_activity'] = item.last_activity_at

    # 认领信息：本工作区认领的对象
    claims = {(c.source_type, c.source_id): c for c in
              WorkObjectClaim.query.filter_by(workspace_id=ws.id).all()}

    # 行集 = 认领 ∪ 引用；认领但从未引用的对象也显示（active=0）
    keys = set(stats.keys()) | set(claims.keys())
    board = []
    for (st_type, sid) in keys:
        provider = projections.get_provider(st_type)
        claim = claims.get((st_type, sid))
        fields = provider.summarize(user, sid) if provider else None
        st = stats.get((st_type, sid), {'active': 0, 'overdue': 0, 'last_activity': None})
        board.append({
            'source_type': st_type,
            'label': provider.label if provider else st_type,
            'source_id': sid,
            'accessible': fields is not None,
            'title': fields.get('title') if fields else None,
            'fields': fields,
            'claimed': claim is not None,
            'claimed_by': claim.claimed_by if claim else None,
            'claimed_at': (claim.claimed_at.strftime('%Y-%m-%d %H:%M')
                           if claim and claim.claimed_at else None),
            'active_items': st['active'],
            'overdue_items': st['overdue'],
            'last_activity': (st['last_activity'].strftime('%Y-%m-%d %H:%M')
                              if st['last_activity'] else None),
        })
    board.sort(key=lambda r: (-r['active_items'], r.get('title') or ''))
    return {'workspace': {'id': ws.id}, 'objects': board,
            'stewardable_types': [t for t, p in projections.PROVIDERS.items()
                                  if p.stewardable]}

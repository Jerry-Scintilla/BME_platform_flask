"""内部工作台·跨组交付（跨组协作方案 §4.2，X1）。

组对组的显式交接：事项所有权不动，源组协调员发起 → 目标组协调员接单（同事务
在本组工作区生成关联任务）→ 目标组执行 → 任务完成时回填源事项。交付包（说明+
固定版本文件）对接单组只读，源事项正文不自动放开（与 §9.5 授权转交同构）。

kind 为用途自由填写（不承担流程语义，UI 提供快捷建议）；同一事项对同一目标组
同一用途仅一个 offered 在途（数据库唯一索引兜底，应用层先行校验给友好错误）。
全部函数只 add/update 不 commit——与业务变更同事务，由蓝图层提交。
"""
import json
from datetime import datetime

from exts import db
from models import (UserModel, WorkBusinessLink, WorkEvent, WorkFile, WorkFileVersion,
                    WorkFileLink, WorkHandoff, WorkItem, WorkTask, WorkWorkspace)
from services.work import access, reminders
from services.work.access import WorkApiError
from services.work.events import record_event, fan_out

HANDOFF_NOTIFY_TEXTS = {
    'handoff_offered': '有一项跨组交付等待你的组接单',
    'handoff_accepted': '你发起的跨组交付已被接单',
    'handoff_completed': '你发起的跨组交付已完成',
    'handoff_declined': '你发起的跨组交付被拒绝',
    'handoff_expired': '你发起的跨组交付已过期限',
}


def _latest_event(item_id):
    return (WorkEvent.query.filter_by(item_id=item_id)
            .order_by(WorkEvent.seq.desc()).first())


def _ws_label(ws_id):
    ws = WorkWorkspace.query.get(ws_id)
    if not ws or not ws.club_group_id:
        return None
    from models import ClubGroup
    g = ClubGroup.query.get(ws.club_group_id)
    return g.name if g else None


def _notify_group_coordinators(ws_id, notify_type, exclude_user_id=None):
    """通知某工作区全部有效协调员（接单/拒绝的知会对象）。"""
    event = None       # handoff 通知不挂事项事件（跨组语义在两端各自留事件）
    from blueprints.notification import create_notification
    from models import WorkAccessGrant
    sent = 0
    for g in WorkAccessGrant.query.filter_by(workspace_id=ws_id, status='active',
                                             role='coordinator').all():
        if g.user_id == exclude_user_id or not access._grant_effective(g):
            continue
        create_notification(g.user_id, '内部工作台',
                            HANDOFF_NOTIFY_TEXTS.get(notify_type, '跨组交付有新动态'),
                            category='work', source_type='work_item', source_id=None)
        sent += 1
    return sent


# ── 发起 ────────────────────────────────────────────────────

def create_handoff(user, item_id, payload):
    """发起交付：源事项协调员∨作者；kind/note 必填；绑定固定版本文件可选。"""
    item = (WorkItem.query.filter_by(id=item_id).with_for_update().first())
    item, item_access = access.require_read(user, item)
    if not (item_access.is_coordinator or item.created_by == user.id):
        raise WorkApiError(403, '仅作者或本组协调员可以发起跨组交付')
    if item.kind != 'task':
        raise WorkApiError(409, '仅任务可以跨组交付')
    if item.status == 'draft':
        raise WorkApiError(409, '草稿不能交付')

    kind = (str(payload.get('kind') or '').strip())
    if not kind or len(kind) > 30:
        raise WorkApiError(400, '交付用途必填且 ≤30 字')
    note = (str(payload.get('note') or '').strip())
    if not note or len(note) > 2000:
        raise WorkApiError(400, '交付说明必填且 ≤2000 字')

    try:
        to_ws_id = int(payload.get('to_workspace_id'))
    except (TypeError, ValueError):
        raise WorkApiError(400, '缺少 to_workspace_id')
    if to_ws_id == item.workspace_id:
        raise WorkApiError(400, '目标组不能是本组（组内请用改派）')
    to_ws = WorkWorkspace.query.get(to_ws_id)
    if not to_ws or to_ws.status != 'active':
        raise WorkApiError(404, '目标工作区不存在或未启用')

    dup = WorkHandoff.query.filter_by(item_id=item.id, to_workspace_id=to_ws_id,
                                      kind=kind, status='offered').first()
    if dup:
        raise WorkApiError(409, '该事项已有一项同用途的在途交付给此组')

    deadline = None
    if payload.get('deadline'):
        try:
            deadline = datetime.strptime(str(payload['deadline'])[:16], '%Y-%m-%dT%H:%M')
        except ValueError:
            raise WorkApiError(400, '期望完成时间格式应为 YYYY-MM-DDTHH:MM')

    handoff = WorkHandoff(
        item_id=item.id, from_workspace_id=item.workspace_id,
        to_workspace_id=to_ws_id, kind=kind, note=note,
        deadline=deadline, status='offered', created_by=user.id)
    db.session.add(handoff)
    db.session.flush()

    # 交付包绑定固定版本文件（可选；复用 files 的归属校验口径）
    vids = payload.get('file_version_ids') or []
    if vids:
        if not isinstance(vids, list) or len(vids) > 20 or len(set(map(int, vids))) != len(vids):
            raise WorkApiError(400, 'file_version_ids 参数非法')
        for vid in vids:
            version = WorkFileVersion.query.filter_by(id=vid).first()
            wf = WorkFile.query.get(version.file_id) if version else None
            if not wf or wf.item_id != item.id or wf.status != 'active' \
                    or version.format_check != 'passed':
                raise WorkApiError(400, f'文件版本 #{vid} 不属于本事项或不可用')
            db.session.add(WorkFileLink(file_id=wf.id, version_id=version.id,
                                        target_type='handoff', target_id=handoff.id,
                                        purpose='handoff_payload', created_by=user.id))
    item.last_activity_at = datetime.now()
    record_event(item, 'handoff_offered', actor_user_id=user.id,
                 diff={'to_group': _ws_label(to_ws_id), 'kind': kind})
    _notify_group_coordinators(to_ws_id, 'handoff_offered', exclude_user_id=user.id)
    return handoff


# ── 接单 / 拒绝 / 撤回 ──────────────────────────────────────

def decide_handoff(user, handoff_id, action, payload=None):
    """accept：目标组协调员接单，同事务生成本组关联任务；decline：必填原因；
    withdraw：发起人∨源组协调员撤回。"""
    payload = payload or {}
    req = WorkHandoff.query.filter_by(id=handoff_id).with_for_update().first()
    if not req:
        raise WorkApiError(404, '交付不存在')
    if req.status != 'offered':
        raise WorkApiError(409, f"该交付已结束（当前状态 {req.status}）")

    if action == 'withdraw':
        item = WorkItem.query.get(req.item_id)
        _, item_access = access.require_read(user, item)
        if not (req.created_by == user.id or item_access.is_coordinator):
            raise WorkApiError(403, '仅发起人或源组协调员可以撤回')
        req.status = 'withdrawn'
        req.decided_by = user.id
        req.decided_at = datetime.now()
        record_event(item, 'handoff_offered', actor_user_id=user.id,
                     diff={'handoff': 'withdrawn', 'kind': req.kind})
        _notify_group_coordinators(req.to_workspace_id, 'handoff_declined',
                                   exclude_user_id=user.id)
        return req

    if action == 'decline':
        access.require_workspace(user, req.to_workspace_id, roles=('coordinator',))
        reason = (str(payload.get('reason') or '').strip())
        if not reason or len(reason) > 500:
            raise WorkApiError(400, '拒绝原因必填且 ≤500 字')
        req.status = 'declined'
        req.decided_by = user.id
        req.decided_at = datetime.now()
        item = WorkItem.query.get(req.item_id)
        record_event(item, 'handoff_declined', actor_user_id=user.id,
                     diff={'kind': req.kind}, reason=reason[:200])
        _notify_group_coordinators(req.from_workspace_id, 'handoff_declined',
                                   exclude_user_id=user.id)
        return req

    if action != 'accept':
        raise WorkApiError(400, 'action 仅支持 accept/decline/withdraw')

    # ── accept：目标组协调员接单，同事务建关联任务 ──
    access.require_workspace(user, req.to_workspace_id, roles=('coordinator',))
    item = WorkItem.query.get(req.item_id)
    if not item or item.status in ('done', 'cancelled'):
        raise WorkApiError(409, '源任务已完结，无法接单')

    from services.work.items import bump_version
    target = WorkItem(
        workspace_id=req.to_workspace_id, kind='task',
        title=f'[{req.kind}] {item.title}'[:200],
        body=None, visibility='workspace', status='todo',
        created_by=user.id)
    db.session.add(target)
    db.session.flush()
    db.session.add(WorkTask(
        item_id=target.id, assignee_user_id=user.id,
        due_at=req.deadline, priority='normal',
        published_at=datetime.now()))
    # 复制源事项的课程关联（业务上下文跟随交付，不授原业务权限）
    for link in WorkBusinessLink.query.filter_by(item_id=item.id).all():
        db.session.add(WorkBusinessLink(item_id=target.id, source_type=link.source_type,
                                        source_id=link.source_id, created_by=user.id))
    req.status = 'accepted'
    req.accepted_item_id = target.id
    req.decided_by = user.id
    req.decided_at = datetime.now()
    record_event(item, 'handoff_accepted', actor_user_id=user.id,
                 diff={'kind': req.kind, 'target_item': target.id})
    target_event_diff = {'source_item': item.id, 'source_handoff': req.id}
    record_event(target, 'created', actor_user_id=user.id,
                 diff=target_event_diff, reason=f'接自 {_ws_label(req.from_workspace_id)} 的交付')
    reminders.regenerate_for_item(target)
    from services.work.items import _notify_workspace_members
    _notify_workspace_members(target, user.id, 'published')
    _notify_group_coordinators(req.from_workspace_id, 'handoff_accepted',
                               exclude_user_id=user.id)
    return req


# ── 完成回填（tasks 的 done 转移调用）───────────────────────

def on_item_done(item, actor):
    """目标任务进入 done 时回填源事项（取舍：目标任务重开不回滚交付——
    结果已通知源组，重开属目标组内部返工，需要重新交付时另起新交付）。"""
    reqs = WorkHandoff.query.filter_by(accepted_item_id=item.id,
                                       status='accepted').all()
    for req in reqs:
        source = WorkItem.query.filter_by(id=req.item_id).first()
        if not source:
            continue
        task = WorkTask.query.filter_by(item_id=item.id).first()
        result_note = None
        if task and task.last_accepted_submission_id:
            from models import WorkSubmission
            sub = WorkSubmission.query.get(task.last_accepted_submission_id)
            result_note = sub.result_note if sub else None
        req.status = 'done'
        req.decided_at = datetime.now()
        req.result_note = result_note
        record_event(source, 'handoff_completed', actor_user_id=actor.id,
                     diff={'kind': req.kind,
                           'by_group': _ws_label(req.to_workspace_id),
                           'result': (result_note or '')[:120]})
        _notify_group_coordinators(req.from_workspace_id, 'handoff_completed')
    return len(reqs)


# ── 过期扫描（调度器调用，可重入）───────────────────────────

def expire_handoffs():
    now = datetime.now()
    rows = WorkHandoff.query.filter(WorkHandoff.status == 'offered',
                                    WorkHandoff.deadline.isnot(None),
                                    WorkHandoff.deadline < now).all()
    for req in rows:
        req.status = 'expired'
        req.decided_at = now
        item = WorkItem.query.get(req.item_id)
        if item:
            record_event(item, 'handoff_offered', actor_user_id=None,
                         diff={'handoff': 'expired', 'kind': req.kind})
        _notify_group_coordinators(req.from_workspace_id, 'handoff_expired')
    return len(rows)


# ── 查询 ────────────────────────────────────────────────────

def handoff_dict(req, *, source_item=None):
    source_item = source_item or WorkItem.query.get(req.item_id)
    return {
        'id': req.id, 'item_id': req.item_id,
        'item_title': source_item.title if source_item else None,
        'from_workspace_id': req.from_workspace_id,
        'from_group_name': _ws_label(req.from_workspace_id),
        'to_workspace_id': req.to_workspace_id,
        'to_group_name': _ws_label(req.to_workspace_id),
        'kind': req.kind, 'note': req.note, 'deadline': (
            req.deadline.strftime('%Y-%m-%d %H:%M') if req.deadline else None),
        'status': req.status, 'accepted_item_id': req.accepted_item_id,
        'result_note': req.result_note,
        'created_by': req.created_by,
        'created_at': req.created_at.strftime('%Y-%m-%d %H:%M') if req.created_at else None,
    }


def my_handoff_queue(user):
    """待接单桶：我任协调员的工作区的 offered 交付。"""
    ws_map = access.workspace_access(user)
    coord_ws = [ws for ws, role in ws_map.items() if role == 'coordinator']
    if not coord_ws:
        return []
    rows = (WorkHandoff.query
            .filter(WorkHandoff.to_workspace_id.in_(coord_ws),
                    WorkHandoff.status == 'offered')
            .order_by(WorkHandoff.created_at.asc()).all())
    return [handoff_dict(r) for r in rows]

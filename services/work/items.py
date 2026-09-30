"""内部工作台·事项与话题服务（设计方案 §7，M2）。

事项（WorkItem）承载话题与任务（kind 区分）；话题流转 draft→open→closed，
任务状态机 M3 迁入 commands。本模块全部走 access 的判定与过滤，
写操作与 WorkEvent/通知回执同事务（蓝图层 commit）。

并发与幂等（§12.2）：回复 seq 在 FOR UPDATE 事项行内分配；
回复按 (author, client_request_id) 幂等（B01）；创建按 idempotency_key 幂等；
编辑/命令带 expected_version 条件更新，冲突 409。
"""
import json
from datetime import datetime

import nh3

from exts import db
from models import (UserModel, WorkItem, WorkItemParticipant, WorkReadState,
                    WorkReply, WorkResponseRequest, WorkTask, WorkEvent)
from services.work import access, reminders
from services.work.access import ItemAccess, WorkApiError
from services.work.events import record_event, fan_out

MAX_TITLE_LEN = 200
MAX_BODY_LEN = 20000
MAX_REPLY_LEN = 5000
KINDS = ('topic', 'task')
VISIBILITIES = ('workspace', 'participants')
PTYPES = ('collaborator', 'observer')
# 话题合法状态集（任务集见 tasks.py，M3）
TOPIC_STATUSES = ('draft', 'open', 'closed')
# 事项列表默认分页（§12.3：服务端分页，默认 20、最大 100）
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


def clean_body(raw):
    """正文（markdown）服务端清洗：限长 + nh3 白名单（脚本/事件属性一律剥离）。

    先截原始文本再清洗（#37）：对清洗结果做硬截可能切在 HTML 标签中间产生
    残缺标记；按比例收缩原始长度直至清洗结果不超过上限（实体转义会少量膨胀）。"""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    probe = text[:MAX_BODY_LEN]
    cleaned = nh3.clean(probe, attributes={})
    while len(cleaned) > MAX_BODY_LEN and probe:
        probe = probe[:max(1, int(len(probe) * MAX_BODY_LEN / len(cleaned)))]
        cleaned = nh3.clean(probe, attributes={})
    return cleaned or None


def _like_pattern(q):
    """检索词转 LIKE 模式并转义 %/_/\\（#38：用户输入的通配符按字面匹配）。

    返回 (pattern, escape_char)；escape_char 为单反斜杠，SQL 端 ESCAPE 子句用。"""
    escaped = ((str(q) or '').strip()[:50]
               .replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_'))
    return f"%{escaped}%", '\\'


def _usernames(uids):
    if not uids:
        return {}
    rows = UserModel.query.filter(UserModel.id.in_(list(uids))).all()
    return {u.id: u.username for u in rows}


def _item_dict(item, group_name=None, task=None, unread=None):
    data = {
        'id': item.id, 'workspace_id': item.workspace_id,
        'group_name': group_name, 'kind': item.kind, 'title': item.title,
        'visibility': item.visibility, 'status': item.status,
        'created_by': item.created_by, 'version': item.version,
        'reply_count': item.last_reply_seq,
        'last_activity_at': item.last_activity_at.strftime('%Y-%m-%d %H:%M') if item.last_activity_at else None,
        'created_at': item.created_at.strftime('%Y-%m-%d %H:%M') if item.created_at else None,
    }
    if unread is not None:
        data['unread'] = unread
    if item.kind == 'task' and task is not None:
        data['task'] = {
            'assignee_user_id': task.assignee_user_id,
            'reviewer_user_id': task.reviewer_user_id,
            'start_at': task.start_at.strftime('%Y-%m-%d %H:%M') if task.start_at else None,
            'due_at': task.due_at.strftime('%Y-%m-%d %H:%M') if task.due_at else None,
            'priority': task.priority,
            'accept_criteria': task.accept_criteria,
            # 逾期是计算值不是状态（§8.2）
            'overdue': bool(task.due_at and task.due_at < datetime.now()),
        }
    return data


# ── 创建（幂等）──────────────────────────────────────────────

def parse_due_at(raw, field='due_at'):
    """截止时间统一口径（#36）：YYYY-MM-DDTHH:MM 或仅日期；仅日期解释为
    当天 23:59:59（方案 §8.5：逾期判定取当天末尾）。任务/改期两入口共用。"""
    if not raw:
        raise WorkApiError(400, f'缺少 {field}')
    text = str(raw)
    for fmt, cut in (('%Y-%m-%dT%H:%M', 16), ('%Y-%m-%d', 10)):
        try:
            dt = datetime.strptime(text[:cut], fmt)
            if fmt == '%Y-%m-%d':
                dt = dt.replace(hour=23, minute=59, second=59)
            return dt
        except ValueError:
            continue
    raise WorkApiError(400, f'{field} 格式应为 YYYY-MM-DD 或 YYYY-MM-DDTHH:MM')


def _parse_task_params(payload):
    """任务草稿参数：负责人/时限可缺（发布前必须补齐），字段超范围即 400。"""
    task = payload.get('task') or {}
    if not isinstance(task, dict):
        raise WorkApiError(400, 'task 参数须为对象')
    params = {}
    for key in ('assignee_user_id', 'reviewer_user_id'):
        if task.get(key) is not None:
            try:
                params[key] = int(task[key])
            except (TypeError, ValueError):
                raise WorkApiError(400, f'task.{key} 须为整数')
    for key in ('start_at', 'due_at'):
        raw = task.get(key)
        if raw:
            if key == 'due_at':
                # 截止统一 23:59:59 口径（#36，与改期命令一致）
                params[key] = parse_due_at(raw, f'task.{key}')
                continue
            text = str(raw)
            try:
                params[key] = datetime.strptime(text[:16], '%Y-%m-%dT%H:%M')
            except ValueError:
                try:
                    # 开始时间仅日期取当天 00:00（起点语义，与截止的末尾口径对称）
                    params[key] = datetime.strptime(text[:10], '%Y-%m-%d')
                except ValueError:
                    raise WorkApiError(400,
                                       f'task.{key} 格式应为 YYYY-MM-DD 或 YYYY-MM-DDTHH:MM')
    if task.get('priority') is not None:
        if task['priority'] not in ('normal', 'high', 'urgent'):
            raise WorkApiError(400, 'task.priority 仅支持 normal/high/urgent')
        params['priority'] = task['priority']
    if task.get('accept_criteria') is not None:
        params['accept_criteria'] = str(task['accept_criteria'])[:2000]
    return params


def create_item(user, payload):
    """创建事项草稿（发布走 commands.publish）。草稿仅作者可见（§5.3）。"""
    workspace = access.require_workspace(user, payload.get('workspace_id'))
    kind = payload.get('kind', 'topic')
    if kind not in KINDS:
        raise WorkApiError(400, f"kind 仅支持 {'/'.join(KINDS)}")
    title = (str(payload.get('title') or '').strip())[:MAX_TITLE_LEN]
    if not title:
        raise WorkApiError(400, '标题必填且不能超过 200 字')
    visibility = payload.get('visibility', 'workspace')
    if visibility not in VISIBILITIES:
        raise WorkApiError(400, f"visibility 仅支持 {'/'.join(VISIBILITIES)}")

    # 幂等：同键返回原事项（B01 对创建同样适用）
    idem = (str(payload.get('idempotency_key') or '').strip() or None)
    if idem:
        existed = WorkItem.query.filter_by(created_by=user.id,
                                           idempotency_key=idem).first()
        if existed:
            return existed, False

    task_params = _parse_task_params(payload) if kind == 'task' else {}
    item = WorkItem(
        workspace_id=workspace.id, kind=kind, title=title,
        body=clean_body(payload.get('body')), visibility=visibility,
        status='draft', created_by=user.id, idempotency_key=idem,
    )
    db.session.add(item)
    db.session.flush()
    if kind == 'task':
        db.session.add(WorkTask(item_id=item.id, **task_params))
    record_event(item, 'created', actor_user_id=user.id,
                 diff={'kind': kind, 'visibility': visibility})
    return item, True


# ── 列表与检索（查询阶段过滤，计数同源 §5.3）──────────────────

def list_items(user, *, kind=None, status=None, workspace_id=None, mine=False,
               q=None, page=1, page_size=DEFAULT_PAGE_SIZE):
    page = max(1, access.int_or_400(page, 'page', default=1))
    page_size = min(MAX_PAGE_SIZE, max(1, access.int_or_400(
        page_size, 'page_size', default=DEFAULT_PAGE_SIZE)))
    if kind is not None and kind not in KINDS:
        raise WorkApiError(400, 'kind 参数非法')
    query = db.session.query(WorkItem)
    query = access.filter_items_query(query, user)     # 列表与 total 同源
    if workspace_id:
        # 不可读工作区与不存在统一 404，不泄露
        access.require_workspace(user, workspace_id)
        query = query.filter(WorkItem.workspace_id == int(workspace_id))
    if kind:
        query = query.filter(WorkItem.kind == kind)
    if status:
        query = query.filter(WorkItem.status == status)
    if mine:
        query = query.filter((WorkItem.created_by == user.id)
                             | WorkItem.id.in_(
                                 db.session.query(WorkItemParticipant.item_id).filter(
                                     WorkItemParticipant.user_id == user.id,
                                     WorkItemParticipant.removed_at.is_(None))))
    if q:
        like, escape_char = _like_pattern(q)
        query = query.filter(WorkItem.title.like(like, escape=escape_char)
                             | WorkItem.body.like(like, escape=escape_char))
    total = query.count()
    rows = (query.order_by(WorkItem.last_activity_at.desc(), WorkItem.id.desc())
            .offset((page - 1) * page_size).limit(page_size).all())
    if not rows:
        return {'items': [], 'total': total, 'page': page, 'page_size': page_size}

    ws_ids = list({r.workspace_id for r in rows})
    from models import WorkWorkspace
    ws_rows = WorkWorkspace.query.filter(WorkWorkspace.id.in_(ws_ids)).all()
    gnames = {}
    if ws_rows:
        from models import ClubGroup
        gids = [w.club_group_id for w in ws_rows]
        gnames = {g.id: g.name for g in ClubGroup.query.filter(ClubGroup.id.in_(gids)).all()}
    ws_group = {w.id: gnames.get(w.club_group_id) for w in ws_rows}
    task_rows = {}
    assignee_names = {}
    if any(r.kind == 'task' for r in rows):
        task_rows = {t.item_id: t for t in WorkTask.query.filter(
            WorkTask.item_id.in_([r.id for r in rows])).all()}
        assignee_names = _usernames([t.assignee_user_id for t in task_rows.values()
                                     if t.assignee_user_id])
    # 未读：本人游标落后于回复水位即未读（与待办分开计数 §6.1）
    read_map = {rs.item_id: rs.last_read_seq for rs in WorkReadState.query.filter(
        WorkReadState.user_id == user.id,
        WorkReadState.item_id.in_([r.id for r in rows])).all()}
    items = []
    for r in rows:
        last_read = read_map.get(r.id, 0)
        data = _item_dict(r, ws_group.get(r.workspace_id), task_rows.get(r.id),
                          unread=r.last_reply_seq > last_read)
        if data.get('task'):
            data['task']['assignee_name'] = assignee_names.get(
                data['task']['assignee_user_id'])
        items.append(data)
    return {'items': items, 'total': total, 'page': page, 'page_size': page_size}


# ── 详情与动作集 ─────────────────────────────────────────────

def allowed_actions(user, item, item_access):
    """详情返回 allowed_actions 供界面渲染；后端每次操作仍重新授权（§13）。"""
    is_author = item.created_by == user.id
    is_coord = item_access.is_coordinator
    actions = []
    if item.status == 'draft':
        if is_author:
            actions += ['edit', 'publish']
    elif item.kind == 'topic':
        actions.append('reply')
        if is_author or is_coord:
            actions += ['edit']
            actions.append('close' if item.status == 'open' else 'reopen')
            if item.status == 'open':
                actions.append('promote')      # 话题转任务（§7.3，tasks.promote）
    else:   # task：M3 命令集
        actions.append('reply')
        if is_author or is_coord:
            actions.append('edit')
    if item.status != 'draft' and (is_coord or is_author):
        actions.append('invite')
    return actions


def get_item_detail(user, item_id):
    """详情：正文/附件与参与者采用相同对象权限（§13）。"""
    item = WorkItem.query.filter_by(id=item_id).first()
    item, item_access = access.require_read(user, item)
    return _serialize_item(user, item, item_access)


def get_item_detail_for_governance(user, item):
    """治理紧急介入的读取入口（权限由治理层校验并留痕，§5.4）。"""
    item_access = access.ItemAccess('emergency')
    return _serialize_item(user, item, item_access)


def _serialize_item(user, item, item_access):
    from models import WorkWorkspace, ClubGroup
    ws = WorkWorkspace.query.get(item.workspace_id)
    group_name = None
    if ws:
        g = ClubGroup.query.get(ws.club_group_id)
        group_name = g.name if g else None
    participants = (WorkItemParticipant.query
                    .filter_by(item_id=item.id, removed_at=None).all())
    names = _usernames([item.created_by] + [p.user_id for p in participants])
    task = WorkTask.query.filter_by(item_id=item.id).first() if item.kind == 'task' else None
    read = WorkReadState.query.filter_by(user_id=user.id, item_id=item.id).first()
    if item.kind == 'task' and task is not None:
        # 任务动作集在 tasks.py（命令表驱动；延迟导入避免 items⇄tasks 环）
        from services.work.tasks import task_allowed_actions
        actions = task_allowed_actions(user, item, task, item_access)
    else:
        actions = allowed_actions(user, item, item_access)
    from services.work import files as files_service
    data = {
        **_item_dict(item, group_name, task),
        'body': item.body,
        'files': files_service.item_files(item.id),
        'created_by_name': names.get(item.created_by),
        'participants': [{'user_id': p.user_id, 'username': names.get(p.user_id),
                          'ptype': p.ptype} for p in participants],
        'last_read_seq': read.last_read_seq if read else 0,
        'allowed_actions': actions,
        'item_access': item_access.to_dict(),
    }
    if item.kind == 'task' and task is not None:
        # 任务上下文：负责人/验收人姓名 + 提交历史（验收绑定具体提交，§8.2）
        from models import WorkSubmission
        task_ids = {task.assignee_user_id, task.reviewer_user_id} - {None}
        if task_ids - set(names):
            extra = _usernames(list(task_ids - set(names)))
            names.update(extra)
        data['task'] = {
            **data['task'],
            'assignee_name': names.get(task.assignee_user_id),
            'reviewer_name': names.get(task.reviewer_user_id),
            'submissions': [{
                'id': sub.id, 'seq': sub.seq,
                'submitted_by': sub.submitted_by,
                'submitted_by_name': names.get(sub.submitted_by),
                'result_note': sub.result_note,
                'decision': sub.decision,
                'decision_note': sub.decision_note,
                'decided_at': sub.decided_at.strftime('%Y-%m-%d %H:%M') if sub.decided_at else None,
                'created_at': sub.created_at.strftime('%Y-%m-%d %H:%M') if sub.created_at else None,
            } for sub in WorkSubmission.query.filter_by(item_id=item.id)
                .order_by(WorkSubmission.seq.desc()).all()],
        }
    return data


# ── 编辑（乐观版本）─────────────────────────────────────────

def patch_item(user, item_id, payload):
    """编辑标题/正文/可见范围：草稿仅作者；已发布=作者∨协调员（§5.2）。

    与命令路径一致持事项行锁（#1）：版本比对-提交原子化，并发 PATCH 不再
    静默丢失更新，record_event 的 seq 分配也回到锁内串行契约。"""
    item = (WorkItem.query
            .filter_by(id=item_id)
            .with_for_update()
            .first())
    item, item_access = access.require_read(user, item)
    if item.status == 'draft':
        if item.created_by != user.id:
            raise WorkApiError(404, '事项不存在')
    elif not (item.created_by == user.id or item_access.is_coordinator):
        raise WorkApiError(403, '仅作者或本组协调员可以编辑')

    expected = payload.get('expected_version')
    try:
        expected = int(expected)
    except (TypeError, ValueError):
        raise WorkApiError(400, '缺少 expected_version')
    if item.version != expected:
        raise WorkApiError(409, '事项已被他人更新，请刷新后重试')

    diff = {}
    if payload.get('title') is not None:
        title = (str(payload['title']).strip())[:MAX_TITLE_LEN]
        if not title:
            raise WorkApiError(400, '标题不能为空')
        if title != item.title:
            diff['title'] = {'from': item.title, 'to': title}
            item.title = title
    if payload.get('body') is not None:
        body = clean_body(payload['body'])
        if body != item.body:
            diff['body'] = 'updated'
            item.body = body
    if payload.get('visibility') is not None:
        visibility = payload['visibility']
        if visibility not in VISIBILITIES:
            raise WorkApiError(400, f"visibility 仅支持 {'/'.join(VISIBILITIES)}")
        if visibility != item.visibility:
            diff['visibility'] = {'from': item.visibility, 'to': visibility}
            item.visibility = visibility
    if not diff:
        return item
    item.version += 1
    item.last_activity_at = datetime.now()
    record_event(item, 'visibility_changed' if set(diff) == {'visibility'} else 'edited',
                 actor_user_id=user.id, diff=diff)
    return item


def ensure_version(item, expected):
    """乐观版本前置校验（§12.2）：必须在任何 ORM 变更之前调用——
    版本不符直接 409，不留下半改状态（错误响应靠请求 teardown 回滚是最后防线，
    不能作为依赖）。"""
    try:
        expected = int(expected)
    except (TypeError, ValueError):
        raise WorkApiError(400, '缺少 expected_version')
    if item.version != expected:
        raise WorkApiError(409, '事项已被他人更新，请刷新后重试')


def bump_version(item, expected, actor_user_id, event_type, diff=None, reason=None):
    """命令类变更的公共段：版本+1 + 事件（版本前置校验由调用方 ensure_version 完成，
    此处 expected 传 None 跳过复检）。调用方持有事项行锁。"""
    if expected is not None:
        ensure_version(item, expected)
    item.version += 1
    item.last_activity_at = datetime.now()
    record_event(item, event_type, actor_user_id=actor_user_id, diff=diff, reason=reason)
    return item


# ── 话题命令：publish / close / reopen（任务命令 M3）────────

def command_topic(user, item_id, command, payload):
    item = (WorkItem.query
            .filter_by(id=item_id)
            .with_for_update()
            .first())
    item, item_access = access.require_read(user, item)
    is_author = item.created_by == user.id
    is_coord = item_access.is_coordinator
    ensure_version(item, (payload or {}).get('expected_version'))

    if command == 'publish':
        if not (is_author or is_coord):
            raise WorkApiError(403, '仅作者或本组协调员可以发布')
        if item.kind != 'topic':
            # 服务层兜底（#35）：任务发布走 tasks._cmd_publish（须补齐负责人与
            # 截止），防止绕过蓝图路由直调本函数跳过任务发布前置检查
            raise WorkApiError(409, '该事项不是话题')
        if item.status != 'draft':
            raise WorkApiError(409, '该事项已发布')
        item.status = 'open'
        bump_version(item, payload.get('expected_version'), user.id, 'published')
        _notify_workspace_members(item, user.id, 'published')
        return item

    if command == 'close':
        if item.kind != 'topic' or item.status != 'open':
            raise WorkApiError(409, '仅进行中的话题可以关闭')
        if not (is_author or is_coord):
            raise WorkApiError(403, '仅作者或本组协调员可以关闭')
        reason = (str(payload.get('reason') or '').strip())[:200] or None
        item.status = 'closed'
        item.closed_reason = reason
        bump_version(item, payload.get('expected_version'), user.id, 'closed', reason=reason)
        _notify_item_audience(item, user.id, 'status_changed')
        return item

    if command == 'reopen':
        if item.kind != 'topic' or item.status != 'closed':
            raise WorkApiError(409, '仅已关闭话题可以重新打开')
        if not (is_author or is_coord):
            raise WorkApiError(403, '仅作者或本组协调员可以重新打开')
        item.status = 'open'
        item.closed_reason = None
        bump_version(item, payload.get('expected_version'), user.id, 'reopened',
                     reason=(str(payload.get('reason') or '').strip())[:200] or None)
        _notify_item_audience(item, user.id, 'status_changed')
        return item

    raise WorkApiError(400, f'暂不支持命令 {command}')


def _ensure_assignee_participant(item, assignee_id, actor):
    """发布任务时负责人若无事项访问权，自动加入参与者（§7.3：不扩大可见范围）。"""
    existed = WorkItemParticipant.query.filter_by(item_id=item.id,
                                                  user_id=assignee_id).first()
    if existed and existed.removed_at is None:
        return
    if existed:
        existed.removed_at = None
        existed.invited_by = actor.id
    else:
        db.session.add(WorkItemParticipant(item_id=item.id, user_id=assignee_id,
                                           invited_by=actor.id))
    record_event(item, 'participant_added', actor_user_id=actor.id,
                 diff={'user_id': assignee_id, 'reason': 'assignee'})


def _notify_item_audience(item, actor_id, notify_type, extra_targets=None):
    """事项相关人通知：作者+有效参与者，剔除操作者，过滤失资格者（§10.2）。

    用户与资格批量预取（#33：受众逐人查询改一次 in_，语义不变）。"""
    uids = {item.created_by}
    for p in WorkItemParticipant.query.filter_by(item_id=item.id, removed_at=None).all():
        uids.add(p.user_id)
    if extra_targets:
        uids |= set(extra_targets)
    event = (WorkEvent.query.filter_by(item_id=item.id)
             .order_by(WorkEvent.seq.desc()).first())
    if not event:
        return
    uids.discard(actor_id)
    users = {u.id: u for u in UserModel.query.filter(UserModel.id.in_(list(uids))).all()} \
        if uids else {}
    eligible = access.participation_eligible_bulk(users.values())
    targets = [(uid, notify_type) for uid in uids
               if uid in users and eligible.get(uid)]
    fan_out(item, event, targets)


def _notify_workspace_members(item, actor_id, notify_type):
    """发布通知：工作区可见事项通知全组有效授权成员；参与档只通知参与者。

    授权来源行/用户/资格批量预取（#33：百人工作区发布一次不再数百次查询）。"""
    if item.visibility != 'workspace':
        return _notify_item_audience(item, actor_id, notify_type)
    event = (WorkEvent.query.filter_by(item_id=item.id)
             .order_by(WorkEvent.seq.desc()).first())
    if not event:
        return
    from models import WorkAccessGrant, WorkWorkspace
    ws = WorkWorkspace.query.get(item.workspace_id)
    if not ws:
        return
    grants = WorkAccessGrant.query.filter_by(workspace_id=ws.id, status='active').all()
    effective = access.grants_effective_bulk(grants)
    targets = []
    target_uids = set()
    for g in grants:
        if g.user_id == actor_id or not effective.get(g.id):
            continue
        targets.append((g.user_id, notify_type))
        target_uids.add(g.user_id)
    # 参与者也一并通知（含跨组被邀者；未被授权扇出覆盖的才补）
    participants = [p.user_id for p in WorkItemParticipant.query.filter_by(
        item_id=item.id, removed_at=None).all() if p.user_id != actor_id]
    extras = [uid for uid in participants if uid not in target_uids]
    if extras:
        users = {u.id: u for u in UserModel.query.filter(UserModel.id.in_(extras)).all()}
        eligible = access.participation_eligible_bulk(users.values())
        for uid in extras:
            if uid in users and eligible.get(uid):
                targets.append((uid, notify_type))
    fan_out(item, event, targets)


# ── 回复（幂等 + FOR UPDATE seq）────────────────────────────

def create_reply(user, item_id, payload):
    """留言/回信（§7）：成功保存才算发出；重试回原结果不重复（B01）。

    幂等检查在行锁与读取权之后（#14）：查询限定本事项，同一请求 ID 撞到
    其他事项的回复时按冲突拒绝，不再跨事项串扰返回、也不再跳过权限检查。"""
    body = (str(payload.get('body') or '').strip())
    if not body or len(body) > MAX_REPLY_LEN:
        raise WorkApiError(400, f'回复内容必填且 ≤{MAX_REPLY_LEN} 字')
    client_request_id = (str(payload.get('client_request_id') or '').strip())
    if not client_request_id or len(client_request_id) > 64:
        raise WorkApiError(400, '缺少 client_request_id')

    item = (WorkItem.query
            .filter_by(id=item_id)
            .with_for_update()
            .first())
    item, item_access = access.require_read(user, item)

    # 幂等复查（行锁后）：同作者同请求 ID 且同事项 → 回原回复（不产生新事件/通知）
    existed = WorkReply.query.filter_by(item_id=item.id, author_id=user.id,
                                        client_request_id=client_request_id).first()
    if existed:
        return existed, False

    if item.status == 'draft':
        raise WorkApiError(409, '草稿不能回复')
    if item.status == 'closed':
        raise WorkApiError(409, '话题已关闭，停止回复')
    # 请求 ID 为作者全局唯一（uq_work_reply_author_request）：撞到其他事项的
    # 同名 key 属客户端误用，明确 409 而非撞唯一约束 500
    dup_other = WorkReply.query.filter_by(
        author_id=user.id, client_request_id=client_request_id).first()
    if dup_other:
        raise WorkApiError(409, '该 client_request_id 已用于其他事项的回复')

    reply_to_id = payload.get('reply_to_id')
    if reply_to_id is not None:
        ref = WorkReply.query.filter_by(id=reply_to_id).first()
        if not ref or ref.item_id != item.id:
            raise WorkApiError(400, '引用的回复不属于本事项')

    # 待回应请求：本人在该事项的 pending 请求随回复自动完结（§7.2）
    response_to_request_id = payload.get('response_to_request_id')
    request_row = None
    if response_to_request_id is not None:
        request_row = WorkResponseRequest.query.filter_by(
            id=response_to_request_id, responder_user_id=user.id,
            item_id=item.id, status='pending').first()
        if not request_row:
            raise WorkApiError(400, '回应请求不存在或已完结')

    reply = WorkReply(
        item_id=item.id, seq=item.last_reply_seq + 1, author_id=user.id,
        body=body, reply_to_id=reply_to_id, client_request_id=client_request_id,
    )
    db.session.add(reply)
    item.last_reply_seq = reply.seq
    item.last_activity_at = datetime.now()
    item.version += 1
    record_event(item, 'replied', actor_user_id=user.id,
                 diff={'seq': reply.seq}, request_id=client_request_id)

    # 发起人可指定回应人（need_reply）：形成待回复记录（§7.2）
    response_spec = payload.get('response')
    if isinstance(response_spec, dict) and response_spec.get('user_id'):
        responder_id = access.int_or_400(response_spec['user_id'], 'response.user_id')
        responder = UserModel.query.get(responder_id)
        if not responder or not access.participation_eligible(responder):
            raise WorkApiError(400, '回应人不具备协作资格')
        if access.can_read_item(responder, item) is None:
            raise WorkApiError(400, '回应人无权查看本事项，须先邀请加入')
        due_at = None
        if response_spec.get('due_at'):
            try:
                due_at = datetime.strptime(str(response_spec['due_at'])[:16], '%Y-%m-%dT%H:%M')
            except ValueError:
                raise WorkApiError(400, '回应时限格式应为 YYYY-MM-DDTHH:MM')
        request_new = WorkResponseRequest(
            item_id=item.id, reply_id=reply.id, responder_user_id=responder_id,
            due_at=due_at, status='pending', created_by=user.id)
        db.session.add(request_new)
        db.session.flush()                   # 取请求行主键供提醒 object_version 用
        # 回应时限提醒（#40 接入 for_response_request：到点催办回应人）
        if due_at:
            reminders.for_response_request(request_new)

    if request_row is not None:
        request_row.status = 'responded'
        request_row.response_reply_id = reply.id
        request_row.responded_at = datetime.now()
        # 已回应：其 response_due 提醒随完结取消（#40）
        reminders.cancel_for_response_request(request_row)

    event = (WorkEvent.query.filter_by(item_id=item.id)
             .order_by(WorkEvent.seq.desc()).first())
    _notify_item_audience(item, user.id, 'reply_to_me')
    if isinstance(response_spec, dict) and response_spec.get('user_id'):
        fan_out(item, event, [(int(response_spec['user_id']), 'response_requested')])
    return reply, True


def list_replies(user, item_id, *, after_seq=0, limit=50):
    """回复按服务器序号分页；断线恢复从最后游标增量拉取（§7.5）。

    edited_at/removed 为回复编辑/撤回的预留位：首期契约——编辑 UI 与端点
    推迟（设计 §7.4），WorkReplyRevision 表预留暂无写入方，两字段暂恒空。"""
    item = WorkItem.query.filter_by(id=item_id).first()
    access.require_read(user, item)
    limit = min(100, max(1, access.int_or_400(limit, 'limit', default=50)))
    after_seq = max(0, access.int_or_400(after_seq, 'after_seq', default=0))
    rows = (WorkReply.query
            .filter(WorkReply.item_id == item.id, WorkReply.seq > after_seq)
            .order_by(WorkReply.seq.asc())
            .limit(limit + 1).all())
    has_more = len(rows) > limit
    rows = rows[:limit]
    names = _usernames([r.author_id for r in rows])
    replies = [{
        'id': r.id, 'seq': r.seq, 'author_id': r.author_id,
        'author_name': names.get(r.author_id), 'body': r.body,
        'reply_to_id': r.reply_to_id, 'edited_at': (
            r.edited_at.strftime('%Y-%m-%d %H:%M') if r.edited_at else None),
        'removed': r.removed_at is not None,
        'created_at': r.created_at.strftime('%Y-%m-%d %H:%M') if r.created_at else None,
    } for r in rows]
    return {'replies': replies, 'has_more': has_more,
            'next_after_seq': rows[-1].seq if rows else after_seq,
            'last_reply_seq': item.last_reply_seq}


# ── 参与者 ──────────────────────────────────────────────────

def add_participant(user, item_id, payload):
    """邀请协作者（独立共享能力 §5.2）：协调员∨作者；目标须有协作资格。"""
    item = (WorkItem.query
            .filter_by(id=item_id)
            .with_for_update()
            .first())
    item, item_access = access.require_read(user, item)
    if item.status == 'draft':
        raise WorkApiError(409, '草稿不能邀请参与者')
    if not (item_access.is_coordinator or item.created_by == user.id):
        raise WorkApiError(403, '仅作者或本组协调员可以邀请参与者')
    try:
        target_id = int(payload.get('user_id'))
    except (TypeError, ValueError):
        raise WorkApiError(400, '缺少 user_id')
    target = UserModel.query.get(target_id)
    if not target:
        raise WorkApiError(404, '用户不存在')
    if not access.participation_eligible(target):
        raise WorkApiError(400, '该用户不具备协作资格（须在任干事或已开通的组员）')
    ptype = payload.get('ptype', 'collaborator')
    if ptype not in PTYPES:
        raise WorkApiError(400, f"ptype 仅支持 {'/'.join(PTYPES)}")

    existed = WorkItemParticipant.query.filter_by(item_id=item.id,
                                                  user_id=target_id).first()
    if existed and existed.removed_at is None:
        raise WorkApiError(409, '该用户已是参与者')
    if existed:                       # 重邀原地复活（不越过资格，上面已校验）
        existed.removed_at = None
        existed.ptype = ptype
        existed.invited_by = user.id
    else:
        db.session.add(WorkItemParticipant(item_id=item.id, user_id=target_id,
                                           ptype=ptype, invited_by=user.id))
    item.last_activity_at = datetime.now()
    record_event(item, 'participant_added', actor_user_id=user.id,
                 diff={'user_id': target_id, 'ptype': ptype})
    event = (WorkEvent.query.filter_by(item_id=item.id)
             .order_by(WorkEvent.seq.desc()).first())
    fan_out(item, event, [(target_id, 'mentioned')])
    return item


def remove_participant(user, item_id, target_id, reason=None):
    item = (WorkItem.query
            .filter_by(id=item_id)
            .with_for_update()
            .first())
    item, item_access = access.require_read(user, item)
    if not (item_access.is_coordinator or item.created_by == user.id):
        raise WorkApiError(403, '仅作者或本组协调员可以移除参与者')
    p = WorkItemParticipant.query.filter_by(item_id=item.id,
                                            user_id=target_id).first()
    if not p or p.removed_at is not None:
        raise WorkApiError(404, '参与者不存在')
    p.removed_at = datetime.now()
    record_event(item, 'participant_removed', actor_user_id=user.id,
                 diff={'user_id': target_id},
                 reason=(str(reason or '').strip())[:200] or None)
    return item


# ── 已读游标（只进不退）─────────────────────────────────────

def advance_read(user, item_id, last_read_seq):
    item = WorkItem.query.filter_by(id=item_id).first()
    access.require_read(user, item)
    seq = access.int_or_400(last_read_seq, 'last_read_seq', default=0)
    if seq < 0 or seq > item.last_reply_seq:
        raise WorkApiError(409, '已读游标不能越过可见的最新回复')
    # 条件更新保「只进不退」（#8）：读-比-写三步无锁时，并发推进由后提交者
    # 胜可回退游标；UPDATE ... WHERE last_read_seq < :seq 让较小值永不落库。
    updated = (WorkReadState.query
               .filter(WorkReadState.user_id == user.id,
                       WorkReadState.item_id == item.id,
                       WorkReadState.last_read_seq < seq)
               .update({'last_read_seq': seq}, synchronize_session=False))
    if updated:
        return WorkReadState.query.filter_by(
            user_id=user.id, item_id=item.id).first()
    row = WorkReadState.query.filter_by(user_id=user.id, item_id=item.id).first()
    if row:
        return row                          # 已不落后（并发已推进/幂等重试）
    row = WorkReadState(user_id=user.id, item_id=item.id, last_read_seq=seq)
    db.session.add(row)                     # 首次写入竞态由 uq(user,item) 兜底
    return row


# ── 事件时间线（治理类事件过滤 §13）─────────────────────────

def list_events(user, item_id, *, page=1, page_size=50):
    item = WorkItem.query.filter_by(id=item_id).first()
    try:
        item, item_access = access.require_read(user, item)
    except WorkApiError:
        # 治理者可越过对象读取门查看时间线（§5.4 紧急介入的记录须可查；仅事件不含正文）
        if item is None or not access.is_governance(user):
            raise
        item_access = access.ItemAccess('emergency')
    page = max(1, access.int_or_400(page, 'page', default=1))
    page_size = min(100, max(1, access.int_or_400(page_size, 'page_size', default=50)))
    query = WorkEvent.query.filter_by(item_id=item.id)
    # 治理类事件仅协调员/治理身份可见（紧急介入记录不对普通成员展示）
    if not (item_access.is_coordinator or access.is_governance(user)):
        from services.work.events import GOVERNANCE_EVENT_TYPES
        query = query.filter(WorkEvent.event_type.notin_(GOVERNANCE_EVENT_TYPES))
    total = query.count()
    rows = (query.order_by(WorkEvent.seq.desc())
            .offset((page - 1) * page_size).limit(page_size).all())
    names = _usernames([e.actor_user_id for e in rows if e.actor_user_id])
    events = [{
        'seq': e.seq, 'event_type': e.event_type,
        'actor_user_id': e.actor_user_id, 'actor_name': names.get(e.actor_user_id),
        'actor_snapshot': e.actor_snapshot,
        'diff': json.loads(e.diff_json) if e.diff_json else None,
        'reason': e.reason, 'created_at': (
            e.created_at.strftime('%Y-%m-%d %H:%M') if e.created_at else None),
    } for e in rows]
    return {'events': events, 'total': total, 'page': page, 'page_size': page_size}


# ── 我的待办分桶（首页待办从 M2 起必须真实 §6.1）────────────

def my_todos(user):
    """待回复分桶（M2）；待接手/待验收/到期随 M3 任务命令补齐。"""
    rows = (WorkResponseRequest.query
            .filter_by(responder_user_id=user.id, status='pending')
            .order_by(WorkResponseRequest.created_at.asc()).all())
    if not rows:
        return {'pending_responses': []}
    item_ids = list({r.item_id for r in rows})
    items = {i.id: i for i in WorkItem.query.filter(WorkItem.id.in_(item_ids)).all()}
    names = _usernames([r.created_by for r in rows])
    pending = []
    for r in rows:
        item = items.get(r.item_id)
        if not item or access.can_read_item(user, item) is None:
            continue                      # 撤权后不再显示（也不能泄露存在性）
        pending.append({
            'request_id': r.id, 'item_id': r.item_id, 'item_title': item.title,
            'kind': item.kind, 'requested_by': names.get(r.created_by),
            'due_at': r.due_at.strftime('%Y-%m-%d %H:%M') if r.due_at else None,
            'created_at': r.created_at.strftime('%Y-%m-%d %H:%M') if r.created_at else None,
        })
    return {'pending_responses': pending}

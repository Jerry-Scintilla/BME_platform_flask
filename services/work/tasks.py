"""内部工作台·任务状态机与转交（设计方案 §8，M3）。

状态机（后端校验，前端不可直写 status）：
    draft →(publish 补齐负责人/时限) todo → in_progress ⇄ blocked
    in_progress →(submit+有验收人) review →(accept) done / (return) in_progress
    in_progress →(complete 无验收人+完成说明) done
    活跃态 →(cancel) cancelled；done →(reopen) in_progress。
逾期=due_at 与状态的计算值（§8.2），不是状态；受阻不自动暂停截止。

并发与责任（§12.2）：全部命令在 FOR UPDATE 事项行内执行，expected_version 条件
更新（B02）；负责人唯一，转交须对方确认、同事项仅一个待确认转交（§8.4，B03）；
验收绑定具体提交行（B05 前置结构，文件版本 M4 接入）。
"""
from datetime import datetime, timedelta

from exts import db
from models import (UserModel, WorkEvent, WorkItem, WorkItemParticipant,
                    WorkSubmission, WorkTask, WorkTransferRequest)
from services.work import access, reminders
from services.work.access import WorkApiError
from services.work.events import record_event, fan_out
from services.work.items import (bump_version, ensure_version, _ensure_assignee_participant,
                                 _notify_item_audience, _parse_task_params, parse_due_at)

TERMINAL_STATUSES = ('done', 'cancelled')
ACTIVE_TASK_STATUSES = ('todo', 'in_progress', 'blocked', 'review')
# 转交默认确认窗口（小时，§8.4）
TRANSFER_DEFAULT_HOURS = 72


def _load_locked_item(user, item_id):
    item = (WorkItem.query.filter_by(id=item_id).with_for_update().first())
    item, item_access = access.require_read(user, item)
    return item, item_access


def _task_of(item):
    if item.kind != 'task':
        raise WorkApiError(409, '该事项不是任务')
    task = WorkTask.query.filter_by(item_id=item.id).first()
    if not task:
        raise WorkApiError(500, '任务属性缺失')
    return task


def _latest_pending_submission(item):
    return (WorkSubmission.query.filter_by(item_id=item.id)
            .filter(WorkSubmission.decision.is_(None))
            .order_by(WorkSubmission.seq.desc()).first())


# ── 话题转任务（§7.3，B04 幂等）──────────────────────────────

def promote(user, item_id, payload):
    """话题转任务：保留原 ID/讨论/附件；重复调用返回同一任务（幂等，B04）。"""
    item = (WorkItem.query.filter_by(id=item_id).with_for_update().first())
    item, item_access = access.require_read(user, item)
    if item.kind == 'task':
        return item, _task_of(item), False          # 幂等：重复点击/重试回原任务
    if item.status == 'draft':
        raise WorkApiError(409, '话题须先发布再转任务')
    if item.status != 'open':
        raise WorkApiError(409, '仅进行中的话题可以转为任务')
    if not (item.created_by == user.id or item_access.is_coordinator):
        raise WorkApiError(403, '仅作者或本组协调员可以转为任务')

    # 接口契约 §13：promote 入参 assignee_id（映射到任务模型的 assignee_user_id）
    task_payload = dict(payload)
    if task_payload.get('assignee_id') is not None:
        task_payload['assignee_user_id'] = task_payload['assignee_id']
    params = _parse_task_params({'task': task_payload})
    if not params.get('assignee_user_id'):
        raise WorkApiError(400, '转任务须指定负责人')
    if not params.get('due_at'):
        raise WorkApiError(400, '转任务须指定截止时间')
    assignee = UserModel.query.get(params['assignee_user_id'])
    if not assignee or not access.participation_eligible(assignee):
        raise WorkApiError(400, '负责人不具备协作资格')

    task = WorkTask(item_id=item.id, **{
        **params,
        'published_at': datetime.now(),
    })
    db.session.add(task)
    item.kind = 'task'
    item.status = 'todo'
    item.promoted_at = datetime.now()
    _ensure_assignee_participant(item, assignee.id, user)
    bump_version(item, payload.get('expected_version'), user.id, 'promoted',
                 diff={'assignee': assignee.id,
                       'due_at': params['due_at'].strftime('%Y-%m-%d %H:%M')})
    reminders.regenerate_for_item(item)
    _notify_item_audience(item, user.id, 'status_changed',
                          extra_targets=[assignee.id])
    event = (WorkEvent.query.filter_by(item_id=item.id)
             .order_by(WorkEvent.seq.desc()).first())
    fan_out(item, event, [(assignee.id, 'assigned')])
    return item, task, True


# ── 任务命令表驱动（§8.2/§8.3）───────────────────────────────

def command_task(user, item_id, command, payload):
    item, item_access = _load_locked_item(user, item_id)
    task = _task_of(item)
    # 乐观版本前置校验：在任何状态变更之前拦截（不留下半改状态）
    ensure_version(item, (payload or {}).get('expected_version'))
    handler = _COMMAND_HANDLERS.get(command)
    if not handler:
        raise WorkApiError(400, f'任务暂不支持命令 {command}')
    return handler(user, item, task, item_access, payload or {})


def _cmd_start(user, item, task, access_, payload):
    if task.assignee_user_id != user.id:
        raise WorkApiError(403, '仅负责人可以开始任务')
    if item.status != 'todo':
        raise WorkApiError(409, '仅待执行任务可以开始')
    item.status = 'in_progress'
    bump_version(item, payload.get('expected_version'), user.id,
                 'status_changed', diff={'status': {'from': 'todo', 'to': 'in_progress'}})
    _notify_item_audience(item, user.id, 'status_changed')
    return item


def _cmd_block(user, item, task, access_, payload):
    if task.assignee_user_id != user.id:
        raise WorkApiError(403, '仅负责人可以标记受阻（协作者请在讨论中说明）')
    if item.status != 'in_progress':
        raise WorkApiError(409, '仅进行中任务可以标记受阻')
    reason = (str(payload.get('blocker_reason') or '').strip())
    if not reason or len(reason) > 500:
        raise WorkApiError(400, '受阻原因必填且 ≤500 字')
    follow_up_at = None
    if payload.get('follow_up_at'):
        try:
            follow_up_at = datetime.strptime(str(payload['follow_up_at'])[:16],
                                             '%Y-%m-%dT%H:%M')
        except ValueError:
            raise WorkApiError(400, '跟进时间格式应为 YYYY-MM-DDTHH:MM')
    item.status = 'blocked'
    bump_version(item, payload.get('expected_version'), user.id,
                 'status_changed', diff={'status': {'from': 'in_progress', 'to': 'blocked'},
                                         'blocker': reason})
    if follow_up_at:
        reminders.add_follow_up(item, follow_up_at)
    _notify_item_audience(item, user.id, 'status_changed')
    return item


def _is_participant(item, user_id):
    p = WorkItemParticipant.query.filter_by(item_id=item.id, user_id=user_id).first()
    return bool(p and p.removed_at is None)


def _cmd_unblock(user, item, task, access_, payload):
    if task.assignee_user_id != user.id:
        raise WorkApiError(403, '仅负责人可以解除受阻')
    if item.status != 'blocked':
        raise WorkApiError(409, '该任务未处于受阻状态')
    item.status = 'in_progress'
    bump_version(item, payload.get('expected_version'), user.id,
                 'status_changed', diff={'status': {'from': 'blocked', 'to': 'in_progress'}})
    reminders.cancel_follow_ups(item.id)
    _notify_item_audience(item, user.id, 'status_changed')
    return item


def _cmd_submit(user, item, task, access_, payload):
    """提交交付（§8.2）：需要验收的任务进 review；无验收人走 complete。"""
    if not (task.assignee_user_id == user.id or _is_participant(item, user.id)):
        raise WorkApiError(403, '仅负责人或协作者可以提交')
    if item.status != 'in_progress':
        raise WorkApiError(409, '仅进行中任务可以提交（受阻请先解除）')
    if not task.reviewer_user_id:
        raise WorkApiError(400, '该任务未指定验收人，请使用「完成」命令并填写完成说明')
    note = (str(payload.get('result_note') or '').strip()) or None
    if note and len(note) > 2000:
        raise WorkApiError(400, '结果说明 ≤2000 字')
    last_seq = (db.session.query(db.func.max(WorkSubmission.seq))
                .filter(WorkSubmission.item_id == item.id).scalar() or 0)
    submission = WorkSubmission(item_id=item.id, seq=last_seq + 1,
                                submitted_by=user.id, result_note=note)
    db.session.add(submission)
    db.session.flush()
    # 交付绑定固定文件版本（B05：验收后新版本不替换既有交付）
    from services.work import files as files_service
    files_service.bind_submission_files(item, submission,
                                        payload.get('file_version_ids') or [], user)
    item.status = 'review'
    bump_version(item, payload.get('expected_version'), user.id, 'submitted',
                 diff={'submission_seq': submission.seq})
    reviewer = UserModel.query.get(task.reviewer_user_id)
    event = (WorkEvent.query.filter_by(item_id=item.id)
             .order_by(WorkEvent.seq.desc()).first())
    if reviewer and access.participation_eligible(reviewer):
        fan_out(item, event, [(reviewer.id, 'review_requested')])
    _notify_item_audience(item, user.id, 'status_changed')
    return item


def _cmd_complete(user, item, task, access_, payload):
    """无验收人任务的完成路径（§8.2：无需验收且填写完成说明）。"""
    if task.assignee_user_id != user.id:
        raise WorkApiError(403, '仅负责人可以完成任务')
    if item.status != 'in_progress':
        raise WorkApiError(409, '仅进行中任务可以完成')
    if task.reviewer_user_id:
        raise WorkApiError(400, '该任务已指定验收人，请提交后由验收人通过')
    note = (str(payload.get('completion_note') or '').strip())
    if not note or len(note) > 2000:
        raise WorkApiError(400, '完成说明必填且 ≤2000 字')
    last_seq = (db.session.query(db.func.max(WorkSubmission.seq))
                .filter(WorkSubmission.item_id == item.id).scalar() or 0)
    submission = WorkSubmission(item_id=item.id, seq=last_seq + 1,
                                submitted_by=user.id, result_note=note,
                                decision='accepted', decided_by=user.id,
                                decided_at=datetime.now())
    db.session.add(submission)
    db.session.flush()
    # 无验收人路径的交付同样绑定固定文件版本（B05）
    from services.work import files as files_service
    files_service.bind_submission_files(item, submission,
                                        payload.get('file_version_ids') or [], user)
    task.last_accepted_submission_id = submission.id
    item.status = 'done'
    bump_version(item, payload.get('expected_version'), user.id, 'status_changed',
                 diff={'status': {'from': 'in_progress', 'to': 'done'}})
    reminders.cancel_for_item(item.id)
    _notify_item_audience(item, user.id, 'status_changed')
    from services.work import handoffs as handoffs_service
    handoffs_service.on_item_done(item, user)      # 跨组交付回填（X1）
    return item


def _reviewer_or_proxy(user, item, task, access_, payload):
    """验收人 ∨ 协调员（受控代验收须留痕，§5.2/§8.3）。"""
    if task.reviewer_user_id == user.id:
        return None
    if access_.is_coordinator:
        return {'proxy': True, 'proxy_by': user.id}
    raise WorkApiError(403, '仅指定验收人或本组协调员（代验收）可以操作')


def _cmd_review_accept(user, item, task, access_, payload):
    if item.status != 'review':
        raise WorkApiError(409, '该任务不在待验收状态')
    proxy = _reviewer_or_proxy(user, item, task, access_, payload)
    submission = _latest_pending_submission(item)
    if not submission:
        raise WorkApiError(409, '没有待验收的提交')
    note = (str(payload.get('note') or '').strip()) or None
    submission.decision = 'accepted'
    submission.decided_by = user.id
    submission.decided_at = datetime.now()
    submission.decision_note = note
    task.last_accepted_submission_id = submission.id
    item.status = 'done'
    diff = {'decision': 'accepted', 'submission_seq': submission.seq}
    if proxy:
        diff.update(proxy)
    bump_version(item, payload.get('expected_version'), user.id, 'reviewed', diff=diff)
    reminders.cancel_for_item(item.id)
    _notify_item_audience(item, user.id, 'status_changed')
    from services.work import handoffs as handoffs_service
    handoffs_service.on_item_done(item, user)      # 跨组交付回填（X1）
    return item


def _cmd_review_return(user, item, task, access_, payload):
    if item.status != 'review':
        raise WorkApiError(409, '该任务不在待验收状态')
    _reviewer_or_proxy(user, item, task, access_, payload)
    note = (str(payload.get('decision_note') or '').strip())
    if not note or len(note) > 2000:
        raise WorkApiError(400, '退回原因必填且 ≤2000 字')
    submission = _latest_pending_submission(item)
    if not submission:
        raise WorkApiError(409, '没有待验收的提交')
    submission.decision = 'returned'
    submission.decided_by = user.id
    submission.decided_at = datetime.now()
    submission.decision_note = note
    item.status = 'in_progress'
    bump_version(item, payload.get('expected_version'), user.id, 'reviewed',
                 diff={'decision': 'returned', 'submission_seq': submission.seq})
    _notify_item_audience(item, user.id, 'status_changed')
    return item


def _cmd_reschedule(user, item, task, access_, payload):
    """改期（§8.2）：记原值 diff；旧提醒失效、按新版本重建（B06）。"""
    if not access_.is_coordinator:
        raise WorkApiError(403, '改期由本组协调员决定（负责人可在讨论中申请）')
    if item.status in TERMINAL_STATUSES:
        raise WorkApiError(409, '已完结任务不能改期')
    new_due = _parse_due(payload.get('due_at'))
    reason = (str(payload.get('reason') or '').strip())
    if not reason or len(reason) > 200:
        raise WorkApiError(400, '改期原因必填且 ≤200 字')
    if task.start_at and new_due < task.start_at:
        raise WorkApiError(400, '截止时间不能早于开始时间')
    old = task.due_at
    task.due_at = new_due
    bump_version(item, payload.get('expected_version'), user.id, 'due_changed',
                 diff={'due_at': {
                     'from': old.strftime('%Y-%m-%d %H:%M') if old else None,
                     'to': new_due.strftime('%Y-%m-%d %H:%M')}},
                 reason=reason)
    reminders.regenerate_for_item(item)
    _notify_item_audience(item, user.id, 'status_changed')
    return item


def _parse_due(raw):
    """改期截止解析：统一走 items.parse_due_at（#36：与草稿创建同一 23:59:59
    口径，两种入口不再产生不同逾期判定）。"""
    return parse_due_at(raw, 'due_at')


def _cmd_reassign(user, item, task, access_, payload):
    """组内直派（§8.3）：目标须已可读本事项（跨组新负责人走转交确认，§8.4）。"""
    if not access_.is_coordinator:
        raise WorkApiError(403, '分派由本组协调员决定')
    if item.status in TERMINAL_STATUSES:
        raise WorkApiError(409, '已完结任务不能再分派')
    try:
        target_id = int(payload.get('assignee_id'))
    except (TypeError, ValueError):
        raise WorkApiError(400, '缺少 assignee_id')
    target = UserModel.query.get(target_id)
    if not target or not access.participation_eligible(target):
        raise WorkApiError(400, '目标负责人不具备协作资格')
    if target.id == task.assignee_user_id:
        raise WorkApiError(409, '该成员已是负责人')
    if access.can_read_item(target, item) is None:
        raise WorkApiError(400, '目标负责人无权查看本事项；跨组转交请走「转交确认」流程')
    reason = (str(payload.get('reason') or '').strip())
    if not reason or len(reason) > 200:
        raise WorkApiError(400, '分派原因必填且 ≤200 字')
    old = task.assignee_user_id
    task.assignee_user_id = target.id
    _ensure_assignee_participant(item, target.id, user)
    bump_version(item, payload.get('expected_version'), user.id, 'assigned',
                 diff={'assignee': {'from': old, 'to': target.id}}, reason=reason)
    reminders.regenerate_for_item(item)
    event = (WorkEvent.query.filter_by(item_id=item.id)
             .order_by(WorkEvent.seq.desc()).first())
    fan_out(item, event, [(target.id, 'assigned')])
    _notify_item_audience(item, user.id, 'status_changed')
    return item


def _cmd_cancel(user, item, task, access_, payload):
    if not (access_.is_coordinator or task.assignee_user_id == user.id):
        raise WorkApiError(403, '仅本组协调员或负责人可以取消')
    if item.status not in ACTIVE_TASK_STATUSES:
        raise WorkApiError(409, '该任务已完结')
    reason = (str(payload.get('reason') or '').strip())
    if not reason or len(reason) > 200:
        raise WorkApiError(400, '取消原因必填且 ≤200 字')
    old = item.status
    item.status = 'cancelled'
    bump_version(item, payload.get('expected_version'), user.id, 'status_changed',
                 diff={'status': {'from': old, 'to': 'cancelled'}}, reason=reason)
    reminders.cancel_for_item(item.id)
    _notify_item_audience(item, user.id, 'status_changed')
    return item


def _cmd_reopen(user, item, task, access_, payload):
    """done→in_progress（§8.2）；已取消任务不复活——重新创建关联任务。"""
    if not access_.is_coordinator:
        raise WorkApiError(403, '重新打开由本组协调员决定')
    if item.status != 'done':
        raise WorkApiError(409, '仅已完成任务可以重新打开（已取消请新建任务）')
    reason = (str(payload.get('reason') or '').strip())
    if not reason or len(reason) > 200:
        raise WorkApiError(400, '重新打开原因必填且 ≤200 字')
    item.status = 'in_progress'
    bump_version(item, payload.get('expected_version'), user.id, 'reopened',
                 diff={'status': {'from': 'done', 'to': 'in_progress'}}, reason=reason)
    reminders.regenerate_for_item(item)
    _notify_item_audience(item, user.id, 'status_changed')
    return item


def _cmd_publish(user, item, task, access_, payload):
    """任务发布（draft→todo）：发布前必须补齐负责人与截止时间（§8.1）。"""
    if not (item.created_by == user.id or access_.is_coordinator):
        raise WorkApiError(403, '仅作者或本组协调员可以发布')
    if item.status != 'draft':
        raise WorkApiError(409, '该任务已发布')
    if not task.assignee_user_id or not task.due_at:
        raise WorkApiError(409, '任务发布前须补齐负责人与截止时间')
    assignee = UserModel.query.get(task.assignee_user_id)
    if not assignee or not access.participation_eligible(assignee):
        raise WorkApiError(409, '负责人不具备协作资格')
    _ensure_assignee_participant(item, assignee.id, user)
    task.published_at = datetime.now()
    item.status = 'todo'
    bump_version(item, payload.get('expected_version'), user.id, 'published')
    reminders.regenerate_for_item(item)
    event = (WorkEvent.query.filter_by(item_id=item.id)
             .order_by(WorkEvent.seq.desc()).first())
    if event:
        fan_out(item, event, [(assignee.id, 'assigned')])
    from services.work.items import _notify_workspace_members
    _notify_workspace_members(item, user.id, 'published')
    return item


_COMMAND_HANDLERS = {
    'publish': _cmd_publish,
    'start': _cmd_start,
    'block': _cmd_block,
    'unblock': _cmd_unblock,
    'submit': _cmd_submit,
    'complete': _cmd_complete,
    'review_accept': _cmd_review_accept,
    'review_return': _cmd_review_return,
    'reschedule': _cmd_reschedule,
    'reassign': _cmd_reassign,
    'cancel': _cmd_cancel,
    'reopen': _cmd_reopen,
}


# ── 动作集（详情 allowed_actions 的任务段）───────────────────

def task_allowed_actions(user, item, task, item_access):
    actions = {'reply'}
    if item.created_by == user.id or item_access.is_coordinator:
        actions.add('edit')
    if item.status in TERMINAL_STATUSES:
        if item.status == 'done' and item_access.is_coordinator:
            actions.add('reopen')
        if item_access.is_coordinator or item.created_by == user.id:
            actions.add('invite')                # 终态仍可邀请查看（归档分享口径）
        return list(actions)
    is_assignee = task.assignee_user_id == user.id
    is_participant = _is_participant(item, user.id)
    if item.status == 'todo' and is_assignee:
        actions.add('start')
    if item.status == 'in_progress' and is_assignee:
        actions.add('block')
        actions.add('complete' if not task.reviewer_user_id else 'submit')
    if item.status == 'blocked' and is_assignee:
        actions.add('unblock')
    if item.status == 'in_progress' and task.reviewer_user_id \
            and (is_assignee or is_participant):
        actions.add('submit')
    if item.status == 'review' and (task.reviewer_user_id == user.id
                                    or item_access.is_coordinator):
        actions.update({'review_accept', 'review_return'})
    if item_access.is_coordinator:
        actions.update({'reschedule', 'reassign', 'cancel'})
    elif is_assignee:
        actions.add('cancel')
    if item_access.is_coordinator or is_assignee:
        actions.add('transfer')
    if item_access.is_coordinator or item.created_by == user.id:
        actions.add('invite')                    # 参与者邀请（与话题口径一致）
    # 跨组交付（X1）：协调员∨作者可发起；草稿/终态不加（服务层还有校验兜底）
    if (item_access.is_coordinator or item.created_by == user.id) \
            and item.status not in ('draft', 'done', 'cancelled'):
        actions.add('handoff')
    return list(actions)


# ── 转交（§8.4，B03）─────────────────────────────────────────

def create_transfer(user, item_id, payload):
    """发起负责人转交：同事项仅一个待确认（FOR UPDATE 事项行保证）。"""
    item, item_access = _load_locked_item(user, item_id)
    task = _task_of(item)
    if item.status in TERMINAL_STATUSES:
        raise WorkApiError(409, '已完结任务不能转交')
    if not (item_access.is_coordinator or task.assignee_user_id == user.id):
        raise WorkApiError(403, '仅本组协调员或当前负责人可以发起转交')
    pending = WorkTransferRequest.query.filter_by(item_id=item.id,
                                                  status='pending').first()
    if pending:
        raise WorkApiError(409, '该事项已有待确认的转交')
    try:
        target_id = int(payload.get('to_user_id'))
    except (TypeError, ValueError):
        raise WorkApiError(400, '缺少 to_user_id')
    target = UserModel.query.get(target_id)
    if not target:
        raise WorkApiError(404, '用户不存在')
    if target.id == task.assignee_user_id:
        raise WorkApiError(409, '该成员已是负责人')
    if not access.participation_eligible(target):
        raise WorkApiError(400, '目标负责人不具备协作资格')
    reason = (str(payload.get('reason') or '').strip()) or None
    expires_at = datetime.now() + timedelta(hours=TRANSFER_DEFAULT_HOURS)
    if payload.get('expires_at'):
        try:
            expires_at = datetime.strptime(str(payload['expires_at'])[:16],
                                           '%Y-%m-%dT%H:%M')
        except ValueError:
            raise WorkApiError(400, '确认期限格式应为 YYYY-MM-DDTHH:MM')
    req = WorkTransferRequest(
        item_id=item.id, from_user_id=task.assignee_user_id,
        to_user_id=target.id, status='pending', reason=reason,
        expires_at=expires_at, item_version=item.version, created_by=user.id)
    db.session.add(req)
    item.last_activity_at = datetime.now()
    record_event(item, 'transfer_requested', actor_user_id=user.id,
                 diff={'to': target.id, 'expires_at': expires_at.strftime('%Y-%m-%d %H:%M')},
                 reason=reason)
    event = (WorkEvent.query.filter_by(item_id=item.id)
             .order_by(WorkEvent.seq.desc()).first())
    fan_out(item, event, [(target.id, 'transfer_pending')])
    return req


#: 转交状态文案（错误信息用）
_TRANSFER_STATUS_TEXT = {'accepted': '接受', 'rejected': '拒绝', 'withdrawn': '撤回',
                         'expired': '过期', 'cancelled': '取消'}


def decide_transfer(user, transfer_id, action):
    """接受/拒绝/撤回。接受=本人确认+原子校验+替换负责人（§8.4）。

    服务层不自行 commit；过期转交的状态翻转由调度器 expire_transfers 负责，
    这里只做前置校验（B03：过期后当前负责人保持不变）。"""
    req = (WorkTransferRequest.query.filter_by(id=transfer_id)
           .with_for_update().first())
    if not req:
        raise WorkApiError(404, '转交请求不存在')
    if req.status != 'pending':
        raise WorkApiError(409,
                           f"该转交已{_TRANSFER_STATUS_TEXT.get(req.status, '完结')}")

    if action == 'withdraw':
        item = (WorkItem.query.filter_by(id=req.item_id).with_for_update().first())
        _, item_access = access.require_read(user, item)
        if not (req.created_by == user.id or item_access.is_coordinator):
            raise WorkApiError(403, '仅发起人或本组协调员可以撤回')
        req.status = 'withdrawn'
        req.decided_at = datetime.now()
        record_event(item, 'transfer_requested', actor_user_id=user.id,
                     diff={'transfer': 'withdrawn'})
        return req

    if user.id != req.to_user_id:
        raise WorkApiError(403, '仅转交目标本人可以确认或拒绝')

    item = (WorkItem.query.filter_by(id=req.item_id).with_for_update().first())
    task = _task_of(item)
    target = UserModel.query.get(req.to_user_id)

    if action == 'reject':
        req.status = 'rejected'
        req.decided_at = datetime.now()
        record_event(item, 'transfer_requested', actor_user_id=user.id,
                     diff={'transfer': 'rejected'})
        event = (WorkEvent.query.filter_by(item_id=item.id)
                 .order_by(WorkEvent.seq.desc()).first())
        fan_out(item, event, [(req.from_user_id, 'status_changed')])
        return req

    # accept：请求仍有效 + 事项仍活跃 + 目标仍有资格（同一事务原子校验，§12.2）
    if req.expires_at < datetime.now():
        raise WorkApiError(409, '该转交已过确认期限')
    if item.status in TERMINAL_STATUSES:
        raise WorkApiError(409, '任务已完结，转交无法接受')
    if not target or not access.participation_eligible(target):
        raise WorkApiError(409, '目标负责人已不具备协作资格')
    old = task.assignee_user_id
    task.assignee_user_id = target.id
    _ensure_assignee_participant(item, target.id, user)
    req.status = 'accepted'
    req.decided_at = datetime.now()
    record_event(item, 'transfer_accepted', actor_user_id=user.id,
                 diff={'assignee': {'from': old, 'to': target.id}})
    item.version += 1
    item.last_activity_at = datetime.now()
    reminders.regenerate_for_item(item)
    event = (WorkEvent.query.filter_by(item_id=item.id)
             .order_by(WorkEvent.seq.desc()).first())
    fan_out(item, event, [(old, 'status_changed'), (target.id, 'assigned')])
    _notify_item_audience(item, user.id, 'status_changed')
    return req


def expire_transfers():
    """过期转交扫描（调度器；B03：过期后当前负责人保持不变）。

    逐行 FOR UPDATE + 复查 status（#12）：与 accept 竞态时后到者空转，
    已接受的转交不会被覆写为 expired；事项行同样加锁，保证 record_event
    的 seq 分配串行（并发命令撞 uq_work_event_item_seq 会整轮扫描回滚）。"""
    now = datetime.now()
    rows = (WorkTransferRequest.query
            .filter(WorkTransferRequest.status == 'pending',
                    WorkTransferRequest.expires_at < now).all())
    expired = 0
    for req in rows:
        # 行锁下重读最新状态（快照可能已过期：accept 恰在本轮扫描窗口内提交）
        locked = (WorkTransferRequest.query.filter_by(id=req.id)
                  .with_for_update().populate_existing().first())
        if not locked or locked.status != 'pending':
            continue
        item = (WorkItem.query.filter_by(id=locked.item_id)
                .with_for_update().populate_existing().first())
        locked.status = 'expired'
        locked.decided_at = now
        if item:
            record_event(item, 'transfer_requested', actor_user_id=None,
                         diff={'transfer': 'expired'})
            event = (WorkEvent.query.filter_by(item_id=item.id)
                     .order_by(WorkEvent.seq.desc()).first())
            if event:
                fan_out(item, event, [(locked.from_user_id, 'status_changed')])
        expired += 1
    return expired


# ── 需接管标记（D01 标记段；治理队列视图 M5 完善）─────────────

def takeover_candidates():
    """活跃任务的负责人已失去协作资格 → 需接管（撤权即时、历史操作者不变）。

    事项/用户/资格批量预取（#33）：两次 in_ + join 取双实体，替代逐行 get。"""
    rows = (db.session.query(WorkTask, WorkItem)
            .join(WorkItem, WorkItem.id == WorkTask.item_id)
            .filter(WorkItem.status.in_(ACTIVE_TASK_STATUSES),
                    WorkTask.assignee_user_id.isnot(None)).all())
    if not rows:
        return []
    uids = list({t.assignee_user_id for t, _ in rows})
    users = {u.id: u for u in UserModel.query.filter(UserModel.id.in_(uids)).all()}
    eligible = access.participation_eligible_bulk(users.values())
    return [{'item_id': task.item_id, 'title': item.title,
             'workspace_id': item.workspace_id,
             'assignee_user_id': task.assignee_user_id,
             'status': item.status}
            for task, item in rows
            if task.assignee_user_id not in users or not eligible.get(task.assignee_user_id)]


# ── 我的待办四桶（§6.1：待接手/待回复/待验收/到期）────────────

def my_todos(user):
    """合并任务桶与回应桶（M2 items.my_todos 的待回复），全部经访问过滤。

    join 直接取 (WorkTask, WorkItem) 双实体（#33：不再逐行取事项标题）。"""
    from services.work import items as items_service
    data = items_service.my_todos(user)

    # 待接手：发给本人的待确认转交
    transfers = (WorkTransferRequest.query
                 .filter_by(to_user_id=user.id, status='pending')
                 .order_by(WorkTransferRequest.expires_at.asc()).all())
    pending_transfers = []
    if transfers:
        t_items = {i.id: i for i in WorkItem.query.filter(WorkItem.id.in_(
            [t.item_id for t in transfers])).all()}
        for t in transfers:
            item = t_items.get(t.item_id)
            if not item or access.can_read_item(user, item) is None:
                continue
            pending_transfers.append({
                'transfer_id': t.id, 'item_id': t.item_id, 'item_title': item.title,
                'from_user_id': t.from_user_id,
                'expires_at': t.expires_at.strftime('%Y-%m-%d %H:%M'),
            })
    data['pending_transfers'] = pending_transfers

    # 待验收：我是验收人且事项在 review
    review_rows = access.filter_items_query(
        db.session.query(WorkTask, WorkItem).join(
            WorkItem, WorkItem.id == WorkTask.item_id)
        .filter(WorkItem.status == 'review', WorkTask.reviewer_user_id == user.id),
        user).all()
    data['to_review'] = [{
        'item_id': item.id,
        'item_title': item.title,
        'due_at': task.due_at.strftime('%Y-%m-%d %H:%M') if task.due_at else None,
    } for task, item in review_rows]

    # 到期（含逾期标记；未来 72h 内到期 + 已逾期）
    now = datetime.now()
    due_rows = access.filter_items_query(
        db.session.query(WorkTask, WorkItem).join(
            WorkItem, WorkItem.id == WorkTask.item_id)
        .filter(WorkItem.status.in_(ACTIVE_TASK_STATUSES),
                WorkTask.assignee_user_id == user.id,
                WorkTask.due_at.isnot(None),
                WorkTask.due_at < now + timedelta(hours=72)),
        user).order_by(WorkTask.due_at.asc()).all()
    data['due'] = [{
        'item_id': item.id,
        'item_title': item.title,
        'due_at': task.due_at.strftime('%Y-%m-%d %H:%M'),
        'overdue': task.due_at < now,
    } for task, item in due_rows]
    # 待接单：我任协调员的工作区的 offered 跨组交付（X1）
    from services.work import handoffs as handoffs_service
    data['handoffs'] = handoffs_service.my_handoff_queue(user)
    return data

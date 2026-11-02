"""用户反馈工单 · 管理侧蓝图（2026-09-21 IA 重构 · 设计方案 §12.2D / 阶段 6A）。

- 列表：status/category/priority/assignee/q 多条件分页，默认「待处理优先」排序
  （new/reopened/triaged/in_progress 在前，已闭环在后）；
- 详情：全量消息（公开+内部）+ 全量事件轨迹 + 附件短签 URL；
- triage / assign / messages / transitions：状态机后端校验（VALID_TRANSITIONS），
  前端不可直写 status；每次变更落 Event + @audit_log；
- 「删除」不是管理员的正常处理动作（D-12）——闭环靠 resolved/closed/rejected。

权限：@check_permission('system_management')；写操作 @audit_log。
"""
from datetime import datetime

from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required
from sqlalchemy import case, or_

from exts import db
from models import (UserModel, FeedbackTicket, FeedbackTicketAttachment,
                    FeedbackTicketMessage, FeedbackTicketEvent)
from . import check_permission, audit_log, _current_user
from .feedback_tickets import (VALID_TRANSITIONS, CATEGORIES, PRIORITIES,
                               RESOLUTION_CODES, TERMINAL_STATUSES, _clear_resolution,
                               _capture_metadata, _ticket_dict, _attachment_dicts, _add_event, _notify)

bp = Blueprint("feedback_tickets_admin", __name__, url_prefix="/admin/feedback-tickets")

QUEUES = {
    'inbox': ('new', 'reopened'),
    'active': ('triaged', 'in_progress'),
    'waiting': ('waiting_user',),
    'done': ('resolved', 'closed', 'rejected'),
}


def _locked_ticket(tid):
    return FeedbackTicket.query.filter_by(id=tid).with_for_update().first()


def _stale(ticket, data):
    if data.get('expected_status') is not None and data['expected_status'] != ticket.status:
        return jsonify({'code': 409, 'message': '工单状态已更新，请刷新后再操作'}), 409
    return None


def _eligible_staff(uid):
    user = UserModel.query.get(uid) if isinstance(uid, int) and not isinstance(uid, bool) else None
    return user and user.role == 'super_admin' and user.status == 'active' and user.lifecycle == 'active'

# 待处理优先排序：活跃态（new/reopened/triaged/in_progress/waiting_user）在前，
# resolved/closed/rejected 在后；同组内最新提交在前（09-22 用户定调倒序渲染）
_STATUS_SORT = case(
    (FeedbackTicket.status == 'new', 0),
    (FeedbackTicket.status == 'reopened', 1),
    (FeedbackTicket.status == 'triaged', 2),
    (FeedbackTicket.status == 'in_progress', 3),
    (FeedbackTicket.status == 'waiting_user', 4),
    (FeedbackTicket.status == 'resolved', 5),
    (FeedbackTicket.status == 'closed', 6),
    (FeedbackTicket.status == 'rejected', 7),
    else_=8,
)


@bp.route("")
@jwt_required()
@check_permission('system_management')
def list_tickets():
    page = max(1, request.args.get('page', 1, type=int))
    page_size = min(50, max(1, request.args.get('page_size', 15, type=int)))
    status = request.args.get('status')
    category = request.args.get('category')
    priority = request.args.get('priority')
    assignee_id = request.args.get('assignee_id', type=int)
    q = (request.args.get('q') or '').strip()

    query = FeedbackTicket.query
    queue = request.args.get('queue')
    if queue in QUEUES:
        query = query.filter(FeedbackTicket.status.in_(QUEUES[queue]))
    if request.args.get('assignee') == 'mine':
        query = query.filter_by(assignee_user_id=_current_user().id)
    elif request.args.get('assignee') == 'unassigned':
        query = query.filter(FeedbackTicket.assignee_user_id.is_(None))
    if status:
        query = query.filter_by(status=status)
    if category:
        query = query.filter_by(category=category)
    if priority:
        query = query.filter_by(priority=priority)
    if assignee_id:
        query = query.filter_by(assignee_user_id=assignee_id)
    if q:
        like = f"%{q}%"
        query = query.filter(or_(FeedbackTicket.title.like(like),
                                 FeedbackTicket.description.like(like)))

    total = query.count()
    rows = (query.order_by(_STATUS_SORT, FeedbackTicket.created_at.desc())
            .offset((page - 1) * page_size).limit(page_size).all())

    # 名单/计数一次 IN 查询（不逐行 N+1）
    user_ids = {t.reporter_user_id for t in rows} | {t.assignee_user_id for t in rows if t.assignee_user_id}
    users = {u.id: u.username for u in UserModel.query.filter(UserModel.id.in_(user_ids)).all()} if user_ids else {}
    ticket_ids = [t.id for t in rows]
    from sqlalchemy import func
    msg_counts = dict(db.session.query(FeedbackTicketMessage.ticket_id, func.count(FeedbackTicketMessage.id))
                      .filter(FeedbackTicketMessage.ticket_id.in_(ticket_ids),
                              FeedbackTicketMessage.visibility == 'public')
                      .group_by(FeedbackTicketMessage.ticket_id).all()) if ticket_ids else {}
    att_counts = dict(db.session.query(FeedbackTicketAttachment.ticket_id, func.count(FeedbackTicketAttachment.id))
                      .filter(FeedbackTicketAttachment.ticket_id.in_(ticket_ids))
                      .group_by(FeedbackTicketAttachment.ticket_id).all()) if ticket_ids else {}

    return jsonify({"code": 200, "tickets": [{
        **_ticket_dict(t),
        # 名字来自 IN 查询映射（不逐行 get）
        "reporter_name": users.get(t.reporter_user_id),
        "assignee_name": users.get(t.assignee_user_id) if t.assignee_user_id else None,
        "message_count": msg_counts.get(t.id, 0),
        "attachment_count": att_counts.get(t.id, 0),
    } for t in rows], "total": total, "page": page, "page_size": page_size})


@bp.route("/<int:tid>")
@jwt_required()
@check_permission('system_management')
def admin_detail(tid):
    t = FeedbackTicket.query.get(tid)
    if not t:
        return jsonify({"code": 404, "message": "工单不存在"}), 404
    user = _current_user()

    messages = (FeedbackTicketMessage.query.filter_by(ticket_id=t.id)
                .order_by(FeedbackTicketMessage.created_at).all())
    events = (FeedbackTicketEvent.query.filter_by(ticket_id=t.id)
              .order_by(FeedbackTicketEvent.created_at).all())
    reporter = UserModel.query.get(t.reporter_user_id)
    assignee = UserModel.query.get(t.assignee_user_id) if t.assignee_user_id else None
    actor_ids = {m.author_user_id for m in messages} | {e.actor_user_id for e in events}
    actors = {u.id: u.username for u in UserModel.query.filter(UserModel.id.in_(actor_ids)).all()} if actor_ids else {}

    return jsonify({"code": 200, "ticket": {
        **_ticket_dict(t, reporter=reporter, assignee=assignee,
                       attachment_count=len(_attachment_dicts(t, user.id)),
                       message_count=len([m for m in messages if m.visibility == 'public'])),
        "description": t.description,
        "page_context": _capture_metadata(t).get('page_context', {}),
        "allowed_transitions": sorted(VALID_TRANSITIONS.get(t.status, set()) - ({'triaged', 'closed'} if t.status == 'new' else set())),
        "can_triage": t.status not in TERMINAL_STATUSES,
        "attachments": _attachment_dicts(t, user.id),
        "messages": [{
            "id": m.id, "author_user_id": m.author_user_id,
            "author_name": actors.get(m.author_user_id),
            "visibility": m.visibility,
            "body": m.body,
            "created_at": m.created_at.isoformat() if m.created_at else None,
        } for m in messages],
        "events": [{
            "id": e.id, "event_type": e.event_type,
            "from_status": e.from_status, "to_status": e.to_status,
            "actor_user_id": e.actor_user_id,
            "actor_name": actors.get(e.actor_user_id),
            "metadata": e.metadata_json,
            "created_at": e.created_at.isoformat() if e.created_at else None,
        } for e in events],
    }})


@bp.route("/<int:tid>/triage", methods=["PATCH"])
@jwt_required()
@check_permission('system_management')
@audit_log(operation="工单受理分派")
def triage_ticket(tid):
    """受理（triage）：确认类别/优先级/处理人；new → triaged。"""
    t = _locked_ticket(tid)
    if not t:
        return jsonify({"code": 404, "message": "工单不存在"}), 404
    d = request.json or {}
    stale = _stale(t, d)
    if stale:
        return stale
    if t.status in TERMINAL_STATUSES:
        return jsonify({'code': 400, 'message': '请先重新打开工单再调整处理安排'}), 400
    if 'category' in d and d['category'] not in CATEGORIES:
        return jsonify({'code': 400, 'message': '反馈类型不正确'}), 400
    if 'priority' in d and d['priority'] not in PRIORITIES:
        return jsonify({'code': 400, 'message': '优先级不正确'}), 400
    if d.get('assignee_user_id') is not None and not _eligible_staff(d['assignee_user_id']):
        return jsonify({'code': 400, 'message': '请选择有效的管理员作为处理人'}), 400
    was_new = t.status == 'new'
    old_assignee = t.assignee_user_id
    changes = {}
    if 'category' in d and d['category'] != t.category:
        t.category = d['category']; changes['category'] = d['category']
    if 'priority' in d and d['priority'] != t.priority:
        t.priority = d['priority']; changes['priority'] = d['priority']
    if 'assignee_user_id' in d and d['assignee_user_id'] != t.assignee_user_id:
        uid = d['assignee_user_id']
        t.assignee_user_id = uid; changes['assignee_user_id'] = uid

    if t.status == 'new':
        t.status = 'triaged'
        t.triaged_at = datetime.now()
        _add_event(t, _current_user().id, 'triaged', from_status='new', to_status='triaged', meta=changes)
    elif changes:
        _add_event(t, _current_user().id, 'triaged', meta=changes)
    db.session.commit()

    # 保存优先级不重复发送受理/分派通知。
    if was_new:
        _notify(t.reporter_user_id, '你的反馈已受理',
                f'工单「{t.title}」已进入处理流程', t.id)
    if t.assignee_user_id and t.assignee_user_id != old_assignee:
        _notify(t.assignee_user_id, '你有新的反馈工单',
                f'工单「{t.title}」分派给你处理', t.id)
    db.session.commit()
    return jsonify({"code": 200, "message": "已受理" if was_new else "处理安排已保存"})


@bp.route("/<int:tid>/assign", methods=["PATCH"])
@jwt_required()
@check_permission('system_management')
@audit_log(operation="工单改派")
def assign_ticket(tid):
    """改派处理人（处理中随时可换，事件留痕）。"""
    t = _locked_ticket(tid)
    if not t:
        return jsonify({"code": 404, "message": "工单不存在"}), 404
    uid = (request.json or {}).get('assignee_user_id')
    stale = _stale(t, request.json or {})
    if stale:
        return stale
    if t.status in TERMINAL_STATUSES:
        return jsonify({'code': 400, 'message': '请先重新打开工单再调整处理安排'}), 400
    if uid is not None and not _eligible_staff(uid):
        return jsonify({"code": 400, "message": "请选择有效的管理员作为处理人"}), 400
    old = t.assignee_user_id
    t.assignee_user_id = uid
    if old != uid:
        _add_event(t, _current_user().id, 'assigned', meta={'from': old, 'to': uid})
    db.session.commit()
    if uid and uid != old:
        _notify(uid, '你有新的反馈工单', f'工单「{t.title}」分派给你处理', t.id)
        db.session.commit()
    return jsonify({"code": 200, "message": "已更新处理人"})


@bp.route("/<int:tid>/messages", methods=["POST"])
@jwt_required()
@check_permission('system_management')
@audit_log(operation="工单回复")
def admin_message(tid):
    """管理员回复：公开回复（用户可见，通知提交人）或内部备注（仅管理端可见，不通知）。"""
    t = _locked_ticket(tid)
    if not t:
        return jsonify({"code": 404, "message": "工单不存在"}), 404
    d = request.json or {}
    stale = _stale(t, d)
    if stale:
        return stale
    body = d.get('body') or ''
    visibility = d.get('visibility') or 'public'
    if not isinstance(body, str) or not body.strip() or len(body) > 5000:
        return jsonify({"code": 400, "message": "内容不能为空"}), 400
    if visibility not in ('public', 'internal'):
        return jsonify({"code": 400, "message": "visibility 须为 public/internal"}), 400
    body = body.strip()

    admin = _current_user()
    db.session.add(FeedbackTicketMessage(
        ticket_id=t.id, author_user_id=admin.id, visibility=visibility, body=body))
    _add_event(t, admin.id,
               'public_replied' if visibility == 'public' else 'internal_noted')
    db.session.commit()

    # 内部备注不通知用户（R-08：工单保存事实，通知只保存送达）
    if visibility == 'public':
        _notify(t.reporter_user_id, '你的反馈有新回复',
                f'工单「{t.title}」有处理人员的公开回复', t.id)
        db.session.commit()
    return jsonify({"code": 200,
                    "message": '已回复（用户可见）' if visibility == 'public' else '已记录内部备注'})


@bp.route("/<int:tid>/transitions", methods=["POST"])
@jwt_required()
@check_permission('system_management')
@audit_log(operation="工单状态迁移")
def transition_ticket(tid):
    """受控状态迁移（状态机后端校验）。body: {to, resolution_code?, resolution_summary?, reason?}
    - resolved 需 resolution_code；rejected 需 reason（用户必须能知道为什么不受理）。"""
    t = _locked_ticket(tid)
    if not t:
        return jsonify({"code": 404, "message": "工单不存在"}), 404
    d = request.json or {}
    stale = _stale(t, d)
    if stale:
        return stale
    to_status = d.get('to')
    if not isinstance(to_status, str):
        return jsonify({'code': 400, 'message': '工单状态不正确'}), 400
    # new 的受理统一经 triage；closed 仅限提交人撤回，不能绕过处理流程。
    if t.status == 'new' and to_status in ('closed', 'triaged'):
        return jsonify({'code': 400, 'message': '请通过受理操作进入处理流程'}), 400
    if to_status not in VALID_TRANSITIONS.get(t.status, set()):
        return jsonify({"code": 400,
                        "message": f"非法迁移 {t.status} → {to_status}；"
                                   f"当前状态可迁移至 {'/'.join(VALID_TRANSITIONS.get(t.status, set())) or '无'}"}), 400

    # 必填项校验全部前置——校验失败的请求不得留下半改的 ORM 脏状态（会被后续
    # 无关请求的 commit 带入库，造成状态漂移；冒烟负例抓到过这个坑）
    if to_status == 'resolved' and (not isinstance(d.get('resolution_code'), str) or d['resolution_code'] not in RESOLUTION_CODES):
        return jsonify({"code": 400, "message": "请选择有效的解决方式 resolution_code"}), 400
    if to_status in ('rejected', 'waiting_user') and (not isinstance(d.get('reason'), str) or not d['reason'].strip() or len(d['reason']) > 5000):
        return jsonify({"code": 400, "message": "请填写用户可见的说明"}), 400
    if d.get('resolution_summary') is not None and (not isinstance(d['resolution_summary'], str) or len(d['resolution_summary']) > 5000):
        return jsonify({'code': 400, 'message': '处理说明格式不正确或过长'}), 400

    admin = _current_user()
    from_status = t.status
    t.status = to_status

    if to_status == 'resolved':
        t.resolution_code = d['resolution_code']
        t.resolution_summary = (d.get('resolution_summary') or '').strip() or None
        t.resolved_at = datetime.now()
    elif to_status == 'rejected':
        t.resolution_code = 'rejected'
        t.resolution_summary = d['reason'].strip()
    elif to_status == 'closed':
        t.closed_at = datetime.now()
    elif to_status == 'reopened':
        _clear_resolution(t)
    if to_status == 'waiting_user':
        db.session.add(FeedbackTicketMessage(ticket_id=t.id, author_user_id=admin.id,
                       visibility='public', body=d['reason'].strip()))

    _add_event(t, admin.id, 'status_changed', from_status=from_status, to_status=to_status,
               meta={k: d[k] for k in ('resolution_code', 'resolution_summary', 'reason') if d.get(k)})
    db.session.commit()

    # 状态变化通知提交人（§12.4 五事件：waiting_user/resolved/closed/reopened）
    notify_map = {
        'waiting_user': ('需要你补充信息', f'工单「{t.title}」等待你补充信息后继续处理'),
        'resolved': ('你的反馈已解决', f'工单「{t.title}」已处理完成，请查看结果'),
        'closed': ('你的反馈已关闭', f'工单「{t.title}」已关闭'),
        'rejected': ('你的反馈未予受理', f'工单「{t.title}」未予受理：{t.resolution_summary or ""}'),
        'reopened': ('你的反馈已重新打开', f'工单「{t.title}」已重新进入处理队列'),
    }
    if to_status in notify_map:
        title, content = notify_map[to_status]
        _notify(t.reporter_user_id, title, content, t.id)
        db.session.commit()
    return jsonify({"code": 200, "message": f"已迁移至 {to_status}"})


def _legacy_error_view(user):
    """旧 /information/error/query 兼容形状（转调自 information.py，保留一个发布周期）。

    差异说明：新工单附件不再内嵌 Base64（载荷教训）——旧客户端仅显示有图标记；
    新用户端已切 /feedback-tickets/mine（元数据 + 短签 URL）。status 是后端真相源
    （旧实现前端写死「待处理」的缺陷在此修复）。
    """
    from .feedback_tickets import _attachment_dicts
    tickets = (FeedbackTicket.query.filter_by(reporter_user_id=user.id)
               .order_by(FeedbackTicket.created_at.desc()).all())
    status_labels = {
        'new': '待处理', 'triaged': '已受理', 'in_progress': '处理中',
        'waiting_user': '待你补充', 'resolved': '已解决', 'closed': '已关闭',
        'rejected': '未予受理', 'reopened': '已重新打开',
    }
    result = []
    for t in tickets:
        attachments = FeedbackTicketAttachment.query.filter_by(ticket_id=t.id).all()
        result.append({
            "id": t.id,   # 新轨道 id（delete 端点两条轨道都能认）
            "title": t.title,
            "content": t.description,
            "create_time": t.created_at.strftime("%Y-%m-%d %H:%M:%S") if t.created_at else "",
            "has_image": bool(attachments),
            "image": None,
            "status": t.status,
            "status_label": status_labels.get(t.status, t.status),
        })
    return jsonify({"code": 200, "message": "查询成功", "data": result})
@bp.route('/stats')
@jwt_required()
@check_permission('system_management')
def ticket_stats():
    """工作台待办摘要用：各状态计数 + 最老待处理提交时间。"""
    from sqlalchemy import func
    rows = dict(db.session.query(FeedbackTicket.status, func.count(FeedbackTicket.id))
                .group_by(FeedbackTicket.status).all())
    oldest = (db.session.query(func.min(FeedbackTicket.created_at))
              .filter(FeedbackTicket.status.in_(['new', 'reopened'])).scalar())
    return jsonify({"code": 200, "data": {
        "by_status": rows,
        "pending_total": sum(rows.get(s, 0) for s in ('new', 'reopened', 'triaged')),
        "oldest_pending_at": oldest.isoformat() if oldest else None,
    }})


@bp.route('/staff')
@jwt_required()
@check_permission('system_management')
def staff_candidates():
    users = UserModel.query.filter_by(role='super_admin', status='active', lifecycle='active').order_by(UserModel.username).all()
    return jsonify({'code': 200, 'staff': [{'id': user.id, 'username': user.username} for user in users]})

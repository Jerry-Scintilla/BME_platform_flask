"""用户反馈工单 · 用户侧蓝图（2026-09-21 IA 重构 · 设计方案 §12.2D / 阶段 6A）。

用户侧契约：
- 提交（multipart：title/description/category/severity + image 附件）；
- 查本人工单（分页 + status），状态来自后端真相源（旧前端写死「待处理」退役）；
- 详情：本人可见公开消息与状态时间线——内部备注绝不出现在用户接口（R-07，
  查询层强制 visibility='public'，不是前端过滤）；
- 公开回复：waiting_user 自动回 in_progress；
- 撤回（仅未受理 new）：状态迁移 closed(resolution_code=withdrawn)，不物理删；
- 重新打开（resolved/closed）。

附件走 storage 双后端 + media_sign 短签代理（/feedback-tickets/attachments/<id>，
双通道鉴权：短签或 JWT；管理员或工单提交人可读）。
管理侧见 feedback_tickets_admin.py；旧 /information/error/* 三端点内部转调本模块服务。
"""
import uuid

from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required, get_jwt_identity

from exts import db
from models import UserModel, FeedbackTicket, FeedbackTicketAttachment, FeedbackTicketMessage, FeedbackTicketEvent
from storage import storage
from . import _current_user, audit_log
from .media_sign import media_signed_url, resolve_media_request
from .notification import create_notification
from flask import Response
from urllib.parse import quote

bp = Blueprint("feedback_tickets", __name__, url_prefix="/feedback-tickets")

# ── 领域常量与状态机（管理侧共用；后端校验，前端不可直写 status） ──────────────

CATEGORIES = ('bug', 'feature_request', 'content_issue', 'account_issue', 'other')
SEVERITIES = ('low', 'normal', 'high', 'critical')
PRIORITIES = ('low', 'medium', 'high', 'urgent')

VALID_TRANSITIONS = {
    'new':         {'triaged', 'rejected', 'closed'},       # closed 仅限提交人撤回
    'triaged':     {'in_progress', 'rejected'},
    'in_progress': {'waiting_user', 'resolved', 'rejected'},
    'waiting_user': {'in_progress', 'resolved'},
    'resolved':    {'closed', 'reopened'},
    'closed':      {'reopened'},
    'reopened':    {'in_progress', 'rejected'},
    'rejected':    {'reopened'},
}

MAX_ATTACHMENT_SIZE = 5 * 1024 * 1024   # 单附件 5MB
ALLOWED_IMAGE_TYPES = {'image/png', 'image/jpeg', 'image/webp', 'image/gif'}


def _ticket_dict(t, reporter=None, assignee=None, attachment_count=0, message_count=0):
    return {
        "id": t.id,
        "reporter_user_id": t.reporter_user_id,
        "reporter_name": reporter.username if reporter else None,
        "category": t.category,
        "severity": t.severity,
        "priority": t.priority,
        "title": t.title,
        "status": t.status,
        "assignee_user_id": t.assignee_user_id,
        "assignee_name": assignee.username if assignee else None,
        "resolution_code": t.resolution_code,
        "resolution_summary": t.resolution_summary,
        "attachment_count": attachment_count,
        "message_count": message_count,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "updated_at": t.updated_at.isoformat() if t.updated_at else None,
        "triaged_at": t.triaged_at.isoformat() if t.triaged_at else None,
        "resolved_at": t.resolved_at.isoformat() if t.resolved_at else None,
        "closed_at": t.closed_at.isoformat() if t.closed_at else None,
    }


def _attachment_dicts(ticket, uid):
    """附件元数据 + 短签查看 URL（列表不回 Base64 正文）。"""
    out = []
    for a in FeedbackTicketAttachment.query.filter_by(ticket_id=ticket.id).all():
        out.append({
            "id": a.id,
            "original_name": a.original_name,
            "mime_type": a.mime_type,
            "size": a.size,
            "url": media_signed_url('feedback_ticket', a.id, uid),
        })
    return out


def _save_attachment(ticket, file):
    """multipart 文件 → storage 双后端 + Attachment 行。返回错误响应或 None。"""
    if not file or not file.filename:
        return None
    if (file.mimetype or '') not in ALLOWED_IMAGE_TYPES:
        return jsonify({"code": 400, "message": "附件仅支持 png/jpg/webp/gif 图片"}), 400
    data = file.read()
    if len(data) > MAX_ATTACHMENT_SIZE:
        return jsonify({"code": 400, "message": "附件超过 5MB 上限"}), 400
    ext = file.filename.rsplit('.', 1)[1].lower() if '.' in file.filename else 'png'
    key = f"feedback-tickets/{ticket.id}/{uuid.uuid4().hex}.{ext}"
    # storage 双后端契约收流（local 用 copyfileobj、minio 收 fileobj）
    import io as _io
    storage.put_object(key, _io.BytesIO(data), length=len(data), content_type=file.mimetype)
    db.session.add(FeedbackTicketAttachment(
        ticket_id=ticket.id, storage_key=key,
        original_name=file.filename[:200], mime_type=file.mimetype, size=len(data)))
    return None


def _add_event(ticket, actor_id, event_type, from_status=None, to_status=None, meta=None):
    import json as _json
    db.session.add(FeedbackTicketEvent(
        ticket_id=ticket.id, actor_user_id=actor_id, event_type=event_type,
        from_status=from_status, to_status=to_status,
        metadata_json=_json.dumps(meta, ensure_ascii=False) if meta else None))


def _notify(user_id, title, content, ticket_id):
    """工单通知统一走通知中心（category=system，source_type=feedback_ticket）。"""
    create_notification(user_id, title, content,
                        category='system', source_type='feedback_ticket', source_id=ticket_id)


# ── 用户侧端点 ──────────────────────────────────────────────────────────────

@bp.route("", methods=["POST"])
@jwt_required()
@audit_log(operation="提交反馈工单")
def create_ticket():
    """提交反馈工单（multipart：title/description/category/severity?/image?）。"""
    user = _current_user()
    title = (request.form.get('title') or '').strip()
    description = (request.form.get('description') or '').strip() or None
    category = request.form.get('category') or 'bug'
    severity = request.form.get('severity') or 'normal'
    if not title:
        return jsonify({"code": 400, "message": "标题不能为空"}), 400
    if category not in CATEGORIES:
        return jsonify({"code": 400, "message": f"category 须为 {'/'.join(CATEGORIES)}"}), 400
    if severity not in SEVERITIES:
        severity = 'normal'

    # 幂等保护：同用户同标题 60 秒内重复提交（双击/重试）返回原工单
    from datetime import datetime, timedelta
    recent = FeedbackTicket.query.filter(
        FeedbackTicket.reporter_user_id == user.id,
        FeedbackTicket.title == title,
        FeedbackTicket.created_at >= datetime.now() - timedelta(seconds=60),
    ).first()
    if recent:
        return jsonify({"code": 200, "message": "已提交（重复提交已合并）", "data": {"id": recent.id}})

    ticket = FeedbackTicket(
        reporter_user_id=user.id, category=category, severity=severity,
        title=title[:200], description=description)
    db.session.add(ticket)
    db.session.flush()   # 拿 id 供附件 key 与事件使用

    err = _save_attachment(ticket, request.files.get('image'))
    if err:
        db.session.rollback()
        return err

    _add_event(ticket, user.id, 'created', to_status='new')
    db.session.commit()
    return jsonify({"code": 200, "message": "反馈已提交，我们会在处理时通知你",
                    "data": {"id": ticket.id}})


@bp.route("/mine")
@jwt_required()
def my_tickets():
    """本人工单列表（分页 + status 筛选）。"""
    user = _current_user()
    page = max(1, request.args.get('page', 1, type=int))
    page_size = min(50, max(1, request.args.get('page_size', 10, type=int)))
    status = request.args.get('status')

    q = FeedbackTicket.query.filter_by(reporter_user_id=user.id)
    if status:
        q = q.filter_by(status=status)
    total = q.count()
    rows = (q.order_by(FeedbackTicket.created_at.desc())
            .offset((page - 1) * page_size).limit(page_size).all())

    from collections import Counter
    items = []
    for t in rows:
        messages = FeedbackTicketMessage.query.filter_by(
            ticket_id=t.id, visibility='public').count()
        attachments = FeedbackTicketAttachment.query.filter_by(ticket_id=t.id).count()
        items.append({**_ticket_dict(t, reporter=user), **{
            'message_count': messages, 'attachment_count': attachments}})
    return jsonify({"code": 200, "tickets": items,
                    "total": total, "page": page, "page_size": page_size})


@bp.route("/<int:tid>")
@jwt_required()
def ticket_detail(tid):
    """工单详情（提交人视角）：公开消息 + 状态时间线 + 附件短签 URL。
    内部备注（visibility=internal）在查询层即被排除，不存在字段级泄露面。"""
    user = _current_user()
    t = FeedbackTicket.query.get(tid)
    if not t:
        return jsonify({"code": 404, "message": "工单不存在"}), 404
    if t.reporter_user_id != user.id and not user.is_admin():
        return jsonify({"code": 403, "message": "只能查看自己提交的反馈"}), 403

    messages = (FeedbackTicketMessage.query
                .filter_by(ticket_id=t.id, visibility='public')
                .order_by(FeedbackTicketMessage.created_at).all())
    events = (FeedbackTicketEvent.query.filter_by(ticket_id=t.id)
              .order_by(FeedbackTicketEvent.created_at).all())
    assignee = UserModel.query.get(t.assignee_user_id) if t.assignee_user_id else None

    return jsonify({"code": 200, "ticket": {
        **_ticket_dict(t, reporter=user, assignee=assignee,
                       attachment_count=len(_attachment_dicts(t, user.id)),
                       message_count=len(messages)),
        "description": t.description,
        "attachments": _attachment_dicts(t, user.id),
        "messages": [{
            "id": m.id, "author_user_id": m.author_user_id,
            "author_name": (m.author.username if m.author else None),
            "is_staff_reply": m.author_user_id != t.reporter_user_id,
            "body": m.body,
            "created_at": m.created_at.isoformat() if m.created_at else None,
        } for m in messages],
        "events": [{
            "id": e.id, "event_type": e.event_type,
            "from_status": e.from_status, "to_status": e.to_status,
            "actor_user_id": e.actor_user_id,
            "created_at": e.created_at.isoformat() if e.created_at else None,
        } for e in events if e.event_type in ('created', 'status_changed', 'triaged', 'withdrawn', 'reopened')],
    }})


@bp.route("/<int:tid>/messages", methods=["POST"])
@jwt_required()
def add_message(tid):
    """公开回复（提交人）。waiting_user 状态下收到用户回复 → 自动回 in_progress。"""
    user = _current_user()
    t = FeedbackTicket.query.get(tid)
    if not t:
        return jsonify({"code": 404, "message": "工单不存在"}), 404
    if t.reporter_user_id != user.id:
        return jsonify({"code": 403, "message": "只能回复自己提交的反馈"}), 403
    if t.status in ('closed', 'rejected'):
        return jsonify({"code": 400, "message": "工单已关闭，如需继续请先重新打开"}), 400

    body = (request.json or {}).get('body')
    if not (body and body.strip()):
        return jsonify({"code": 400, "message": "回复内容不能为空"}), 400

    db.session.add(FeedbackTicketMessage(
        ticket_id=t.id, author_user_id=user.id, visibility='public', body=body.strip()))
    _add_event(t, user.id, 'public_replied')
    if t.status == 'waiting_user':
        t.status = 'in_progress'
        _add_event(t, user.id, 'status_changed', from_status='waiting_user', to_status='in_progress')
    db.session.commit()

    # 通知处理人（未分派时不打扰：新回复在工作台待办计数里可见）
    if t.assignee_user_id and t.assignee_user_id != user.id:
        _notify(t.assignee_user_id, '反馈工单有新回复',
                f'你负责的工单「{t.title}」有提交人的新回复', t.id)
        db.session.commit()
    return jsonify({"code": 200, "message": "已回复"})


@bp.route("/<int:tid>/withdraw", methods=["POST"])
@jwt_required()
def withdraw_ticket(tid):
    """撤回（D-12）：仅未受理（new）的工单可由提交人撤回——状态迁移而非物理删除，
    事件与审计保留；已进入处理的工单只能等解决/关闭后重新打开。"""
    user = _current_user()
    t = FeedbackTicket.query.get(tid)
    if not t:
        return jsonify({"code": 404, "message": "工单不存在"}), 404
    if t.reporter_user_id != user.id:
        return jsonify({"code": 403, "message": "只能撤回自己提交的反馈"}), 403
    if t.status != 'new':
        return jsonify({"code": 400, "message": "工单已进入处理流程，不能撤回；可联系管理员关闭"}), 400

    from datetime import datetime
    t.status = 'closed'
    t.resolution_code = 'withdrawn'
    t.resolution_summary = '提交人撤回'
    t.closed_at = datetime.now()
    _add_event(t, user.id, 'withdrawn', from_status='new', to_status='closed')
    db.session.commit()
    return jsonify({"code": 200, "message": "已撤回（记录保留，管理员仍可追溯）"})


@bp.route("/<int:tid>/reopen", methods=["POST"])
@jwt_required()
def reopen_ticket(tid):
    """重新打开：resolved/closed 后问题仍存在时由提交人发起，重进处理队列。"""
    user = _current_user()
    t = FeedbackTicket.query.get(tid)
    if not t:
        return jsonify({"code": 404, "message": "工单不存在"}), 404
    if t.reporter_user_id != user.id:
        return jsonify({"code": 403, "message": "只能操作自己提交的反馈"}), 403
    if t.status not in ('resolved', 'closed'):
        return jsonify({"code": 400, "message": "仅已解决/已关闭的工单可重新打开"}), 400

    reason = ((request.json or {}).get('reason') or '').strip() or None
    t.status = 'reopened'
    _add_event(t, user.id, 'reopened', from_status='resolved' if t.resolved_at else 'closed',
               to_status='reopened', meta={'reason': reason} if reason else None)
    db.session.commit()
    if t.assignee_user_id:
        _notify(t.assignee_user_id, '反馈工单被重新打开',
                f'工单「{t.title}」被提交人重新打开', t.id)
        db.session.commit()
    return jsonify({"code": 200, "message": "已重新打开，等待处理"})


# ── 附件代理端点（media_sign 短签 / JWT 双通道；对象不暴露直链） ────────────────

@bp.route("/attachments/<int:aid>")
def attachment_download(aid):
    a = FeedbackTicketAttachment.query.get(aid)
    if not a:
        return jsonify({"code": 404, "message": "附件不存在"}), 404
    user, auth_err = resolve_media_request('feedback_ticket', aid)
    if auth_err:
        return auth_err
    # 资源级权限：管理员或工单提交人（短签已绑 uid，此处复核身份归属）
    t = FeedbackTicket.query.get(a.ticket_id)
    if not t or (not user.is_admin() and t.reporter_user_id != user.id):
        return jsonify({"code": 403, "message": "无权访问该附件"}), 403
    try:
        obj = storage.get_object(a.storage_key)
    except Exception:
        return jsonify({"code": 500, "message": "附件读取失败（存储服务不可用？）"}), 500
    resp = Response(obj, mimetype=a.mime_type or 'application/octet-stream')
    resp.headers["Content-Disposition"] = \
        f"inline; filename*=UTF-8''{quote(a.original_name)}"
    return resp

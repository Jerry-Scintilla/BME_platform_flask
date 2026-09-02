"""
通知蓝图 — 独立的通知中心 API

提供干净的通知 CRUD 接口，与 information 表解耦。
"""
from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity

from exts import db, mail
from models import UserModel, NotificationModel
from flask_mail import Message
from threading import Thread

bp = Blueprint("notification", __name__, url_prefix="/notification")


# ────────────────────────────────────────
# 辅助函数
# ────────────────────────────────────────

def _get_current_user():
    """获取当前登录用户，失败返回 None"""
    email = get_jwt_identity()
    return UserModel.query.filter_by(email=email).first()


SOURCE_TYPE_MAP = {
    'leave': 'group',
    'task': 'group',
    'homework': 'group',
    'notice': 'group',
    'admin': 'system',
}


def create_notification(user_id, title, content, category='group',
                        source_type=None, source_id=None,
                        group_id=None, is_important=False,
                        camp_session_id=None):
    """
    创建一条通知记录。

    参数:
      user_id       — 接收人 ID
      title         — 通知标题
      content       — 通知内容
      category      — 'system' | 'group' | 'course' | 'camp'
      source_type   — 触发来源: 'leave', 'task', 'homework', 'notice', 'admin', 'reward', 'join_request'
      source_id     — 原始业务记录 ID
      group_id      — 所属小组 ID（group 类型时必填）
      is_important  — 是否重要
      camp_session_id — 所属营期 ID（camp 类型时填）
    """
    n = NotificationModel(
        user_id=user_id,
        title=title,
        content=content,
        category=category,
        source_type=source_type,
        source_id=source_id,
        group_id=group_id,
        is_important=is_important,
        camp_session_id=camp_session_id,
    )
    db.session.add(n)
    return n


def batch_create_notifications(user_ids, title, content, category='system',
                                source_type=None, source_id=None,
                                group_id=None, is_important=False,
                                camp_session_id=None):
    """批量创建通知（给多个用户发同一条通知）"""
    notifications = []
    for uid in user_ids:
        n = create_notification(
            user_id=uid,
            title=title,
            content=content,
            category=category,
            source_type=source_type,
            source_id=source_id,
            group_id=group_id,
            is_important=is_important,
            camp_session_id=camp_session_id,
        )
        notifications.append(n)
    return notifications


def _send_email_async(app, user_emails, title, content, html=None, attachments=None):
    """在子线程中发送邮件，不阻塞请求响应。

    html         — 可选 HTML 正文；提供时 content 作为纯文本 fallback
    attachments  — 可选 list[dict]，每项 {filename, content_type, data(bytes)}
    """
    with app.app_context():
        try:
            subject = f'[BME] {title}'
            # 逐个发送，避免一次塞太多收件人暴露隐私
            for email in user_emails:
                try:
                    msg = Message(subject=subject, recipients=[email])
                    if html:
                        msg.html = html
                        msg.body = content or title  # 纯文本 fallback
                    else:
                        msg.body = f'{title}\n\n{content}\n\n— BME 卓越工程师在线教育平台'
                    if attachments:
                        for att in attachments:
                            msg.attach(att['filename'], att['content_type'], att['data'])
                    mail.send(msg)
                except Exception as e:
                    print(f'[通知邮件] 发送失败 {email}: {e}')
            print(f'[通知邮件] 已发送 {len(user_emails)} 封')
        except Exception as e:
            print(f'[通知邮件] 发送异常: {e}')


def send_notification_emails(user_ids, title, content):
    """异步发送通知邮件给指定用户"""
    from flask import current_app
    app = current_app._get_current_object()

    users = UserModel.query.filter(UserModel.id.in_(user_ids)).all()
    emails = [u.email for u in users if u.email and '@' in u.email]
    if not emails:
        return

    Thread(
        target=_send_email_async,
        args=(app, emails, title, content),
        daemon=True,
    ).start()


def send_report_emails(user_ids, title, content, html=None, attachments=None):
    """按 user_id 查 email 后异步发送（支持 HTML 正文与附件）。

    供出勤报告等富文本邮件使用；与 send_notification_emails 的区别仅在于
    支持 html / attachments 参数。
    """
    from flask import current_app
    app = current_app._get_current_object()

    users = UserModel.query.filter(UserModel.id.in_(user_ids)).all()
    emails = [u.email for u in users if u.email and '@' in u.email]
    if not emails:
        print('[报告邮件] 无有效收件人，跳过发送')
        return

    Thread(
        target=_send_email_async,
        args=(app, emails, title, content),
        kwargs={'html': html, 'attachments': attachments},
        daemon=True,
    ).start()


# ────────────────────────────────────────
# API 端点
# ────────────────────────────────────────

@bp.route("/list", methods=["GET"])
@jwt_required()
def notification_list():
    """
    查询通知列表

    Query params:
      category  — 可选，按分类过滤: 'system' | 'group'
      is_read   — 可选，按已读状态过滤: 'true' | 'false'
      page      — 页码，默认 1
      per_page  — 每页条数，默认 20
    """
    user = _get_current_user()
    if not user:
        return jsonify({"code": 401, "message": "用户未认证"}), 401

    category = request.args.get("category")
    is_read = request.args.get("is_read")
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 20, type=int)
    show_all = request.args.get("all", "false").lower() == "true"

    # 管理员可以通过 all=true 查看所有用户的通知
    if show_all and user.is_admin():
        query = NotificationModel.query
        unread_count = NotificationModel.query.filter_by(is_read=False).count()
    else:
        query = NotificationModel.query.filter_by(user_id=user.id)
        unread_count = NotificationModel.query.filter_by(
            user_id=user.id, is_read=False
        ).count()

    if category:
        query = query.filter_by(category=category)
    if is_read is not None:
        query = query.filter_by(is_read=(is_read.lower() == 'true'))

    query = query.order_by(NotificationModel.created_at.desc())

    total = query.count()

    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    notifications = [n.to_dict() for n in pagination.items]

    return jsonify({
        "code": 200,
        "message": "获取通知列表成功",
        "data": {
            "notifications": notifications,
            "total": total,
            "unread_count": unread_count,
            "page": page,
            "per_page": per_page,
        },
    })


@bp.route("/unread_count", methods=["GET"])
@jwt_required()
def notification_unread_count():
    """获取未读通知数量（轻量接口，供铃铛轮询）"""
    user = _get_current_user()
    if not user:
        return jsonify({"code": 401, "message": "用户未认证"}), 401

    count = NotificationModel.query.filter_by(
        user_id=user.id, is_read=False
    ).count()

    return jsonify({
        "code": 200,
        "data": {"unread_count": count},
    })


@bp.route("/mark_read", methods=["POST"])
@jwt_required()
def notification_mark_read():
    """
    标记单条通知为已读

    Body: { "id": <int> }
    """
    user = _get_current_user()
    if not user:
        return jsonify({"code": 401, "message": "用户未认证"}), 401

    data = request.get_json() or {}
    notification_id = data.get("id")

    if not notification_id:
        return jsonify({"code": 400, "message": "通知ID不能为空"}), 400

    n = NotificationModel.query.filter_by(id=notification_id, user_id=user.id).first()
    if not n:
        return jsonify({"code": 404, "message": "通知不存在"}), 404

    n.is_read = True
    db.session.commit()

    return jsonify({"code": 200, "message": "标记已读成功", "data": {"success": True}})


@bp.route("/mark_all_read", methods=["POST"])
@jwt_required()
def notification_mark_all_read():
    """
    全部标记为已读

    Body (可选):
      category — 按分类过滤: 'system' | 'group'
    """
    user = _get_current_user()
    if not user:
        return jsonify({"code": 401, "message": "用户未认证"}), 401

    data = request.get_json() or {}
    category = data.get("category")

    query = NotificationModel.query.filter_by(user_id=user.id, is_read=False)
    if category:
        query = query.filter_by(category=category)

    count = query.update({"is_read": True})
    db.session.commit()

    return jsonify({
        "code": 200,
        "message": f"已将 {count} 条通知标记为已读",
        "data": {"marked_count": count},
    })


@bp.route("/delete", methods=["DELETE"])
@jwt_required()
def notification_delete():
    """
    删除已读通知

    Body:
      ids — 通知ID列表: [<int>, ...]
      只能删除自己的、且已读的通知
    """
    user = _get_current_user()
    if not user:
        return jsonify({"code": 401, "message": "用户未认证"}), 401

    data = request.get_json() or {}
    ids = data.get("ids", [])

    if not ids:
        return jsonify({"code": 400, "message": "ids 不能为空"}), 400

    count = NotificationModel.query.filter(
        NotificationModel.id.in_(ids),
        NotificationModel.user_id == user.id,
        NotificationModel.is_read == True,  # noqa: E712
    ).delete(synchronize_session=False)

    db.session.commit()

    return jsonify({
        "code": 200,
        "message": "删除成功",
        "data": {"deleted_count": count},
    })


@bp.route("/create", methods=["POST"])
@jwt_required()
def notification_create():
    """
    创建通知（管理员/内部调用）

    Body:
      user_id       — 接收人 ID
      title         — 标题
      content       — 内容
      category      — 分类 (默认 'system')
      source_type   — 来源类型
      source_id     — 来源 ID
      group_id      — 小组 ID
      is_important  — 是否重要 (默认 false)
    """
    user = _get_current_user()
    if not user:
        return jsonify({"code": 401, "message": "用户未认证"}), 401

    # 仅管理员可调用
    if not user.is_admin():
        return jsonify({"code": 403, "message": "权限不足，仅管理员可创建通知"}), 403

    data = request.get_json() or {}
    required_fields = ['user_id', 'title']
    for field in required_fields:
        if not data.get(field):
            return jsonify({"code": 400, "message": f"{field} 不能为空"}), 400

    n = create_notification(
        user_id=data['user_id'],
        title=data['title'],
        content=data.get('content', ''),
        category=data.get('category', 'system'),
        source_type=data.get('source_type'),
        source_id=data.get('source_id'),
        group_id=data.get('group_id'),
        is_important=data.get('is_important', False),
    )
    db.session.commit()

    return jsonify({
        "code": 200,
        "message": "通知创建成功",
        "data": n.to_dict(),
    })


@bp.route("/batch_create", methods=["POST"])
@jwt_required()
def notification_batch_create():
    """
    批量创建通知（系统公告发给全体/指定用户）

    Body:
      user_ids      — 接收人 ID 列表 (为空则发给全体用户)
      title         — 标题
      content       — 内容
      category      — 分类 (默认 'system')
      is_important  — 是否重要 (默认 false)
    """
    user = _get_current_user()
    if not user:
        return jsonify({"code": 401, "message": "用户未认证"}), 401

    if not user.is_admin():
        return jsonify({"code": 403, "message": "权限不足，仅管理员可批量创建通知"}), 403

    data = request.get_json() or {}
    title = data.get('title')
    if not title:
        return jsonify({"code": 400, "message": "title 不能为空"}), 400

    user_ids = data.get('user_ids', [])
    if not user_ids:
        # 发给全体用户
        all_users = UserModel.query.all()
        user_ids = [u.id for u in all_users]

    notifications = batch_create_notifications(
        user_ids=user_ids,
        title=title,
        content=data.get('content', ''),
        category=data.get('category', 'system'),
        source_type=data.get('source_type', 'admin'),
        is_important=data.get('is_important', False),
    )
    db.session.commit()

    # 异步发送邮件通知
    send_notification_emails(user_ids, title, data.get('content', ''))

    return jsonify({
        "code": 200,
        "message": f"已向 {len(notifications)} 名用户发送通知",
        "data": {"created_count": len(notifications)},
    })

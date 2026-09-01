from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from sqlalchemy.exc import IntegrityError

from exts import db, redis_client
from models import UserModel, CampSession, NotificationModel, GratitudeModel
from .notification import create_notification

bp = Blueprint("gratitude", __name__, url_prefix="/gratitude")


# ────────────────────────────────────────
# 辅助函数
# ────────────────────────────────────────

def _get_current_user():
    """获取当前登录用户，失败返回 None"""
    email = get_jwt_identity()
    return UserModel.query.filter_by(email=email).first()


def _avatar_url(avatar_url):
    """头像完整 URL（与 discussion.get_avatar_url 同语义：相对路径按当前页 origin 解析）"""
    if not avatar_url:
        return ""
    if avatar_url.startswith('http://') or avatar_url.startswith('https://'):
        return avatar_url
    return f"/data/avatars/{avatar_url}"


# ────────────────────────────────────────
# 端点
# ────────────────────────────────────────

@bp.route("", methods=["POST"])
@jwt_required()
def gratitude_send():
    """寄出一封感谢信（学员 → 导生）"""
    user = _get_current_user()
    if not user:
        return jsonify({"code": 404, "message": "用户不存在"}), 404

    data = request.get_json() or {}
    recipient_id = data.get("recipient_id")
    content = (data.get("content") or "").strip()
    camp_session_id = data.get("camp_session_id")

    if not recipient_id or not content:
        return jsonify({"code": 400, "message": "缺少收件人或内容"}), 400
    if len(content) > 500:
        return jsonify({"code": 400, "message": "感谢信最多 500 字"}), 400

    recipient = UserModel.query.filter_by(id=recipient_id).first()
    if not recipient:
        return jsonify({"code": 404, "message": "收件人不存在"}), 404
    if recipient.id == user.id:
        return jsonify({"code": 400, "message": "不能给自己写感谢信"}), 400
    if recipient.role == "student":
        return jsonify({"code": 400, "message": "只能给导生写感谢信"}), 400

    if camp_session_id is not None:
        if not CampSession.query.filter_by(id=camp_session_id).first():
            return jsonify({"code": 404, "message": "营期不存在"}), 404

    # 频率限制：每用户每小时 ≤ 5 封（redis 手动计数；同营期重复由唯一约束兜底）
    # 放在所有校验通过后、写库前，避免无效请求消耗配额
    key_1h = f"gratitude_rate:{user.id}:1h"
    try:
        n1h = redis_client.incr(key_1h)
        if n1h == 1:
            redis_client.expire(key_1h, 3600)
    except Exception:
        n1h = 0  # redis 不可用时降级为不限流
    if n1h > 5:
        return jsonify({"code": 429, "message": "寄出太频繁，请稍后再试"}), 429

    g = GratitudeModel(
        sender_id=user.id,
        recipient_id=recipient.id,
        camp_session_id=camp_session_id,
        content=content,
    )
    db.session.add(g)
    try:
        db.session.flush()
    except IntegrityError:
        db.session.rollback()
        return jsonify({"code": 409, "message": "本期已经给这位导生写过感谢信"}), 409

    # 送达提醒：通知只是铃铛角标与跳转入口，信件本体在 gratitude 表
    create_notification(
        recipient.id, "收到一封感谢信",
        f"{user.username} 寄来一封感谢信，点开看看吧",
        category='gratitude', source_type='gratitude', source_id=g.id,
        camp_session_id=camp_session_id,
    )
    db.session.commit()
    return jsonify({"code": 200, "message": "感谢信已寄出", "data": {"id": g.id}}), 200


@bp.route("/received", methods=["GET"])
@jwt_required()
def gratitude_received():
    """我收到的感谢信列表（导生侧）"""
    user = _get_current_user()
    if not user:
        return jsonify({"code": 404, "message": "用户不存在"}), 404

    try:
        page = max(1, int(request.args.get("page", 1)))
        per_page = min(100, max(1, int(request.args.get("per_page", 20))))
    except ValueError:
        page, per_page = 1, 20

    q = (GratitudeModel.query.filter_by(recipient_id=user.id)
         .order_by(GratitudeModel.created_at.desc(), GratitudeModel.id.desc()))
    total = q.count()
    letters = q.offset((page - 1) * per_page).limit(per_page).all()

    # 发件人/营期一次性取齐，避免 N+1
    sender_ids = list({g.sender_id for g in letters})
    senders = ({u.id: u for u in UserModel.query.filter(UserModel.id.in_(sender_ids)).all()}
               if sender_ids else {})
    session_ids = list({g.camp_session_id for g in letters if g.camp_session_id})
    sessions = ({s.id: s for s in CampSession.query.filter(CampSession.id.in_(session_ids)).all()}
                if session_ids else {})

    def _letter_dict(g):
        sender = senders.get(g.sender_id)
        session = sessions.get(g.camp_session_id) if g.camp_session_id else None
        return {
            "id": g.id,
            "sender": {
                "user_id": g.sender_id,
                "username": sender.username if sender else "已注销用户",
                "avatar": _avatar_url(sender.avatar_url) if sender else None,
            },
            "camp_session_id": g.camp_session_id,
            "camp_session_name": session.name if session else None,
            "content": g.content,
            "is_read": g.is_read,
            "created_at": g.created_at.isoformat() if g.created_at else None,
        }

    return jsonify({
        "code": 200,
        "message": "获取感谢信成功",
        "data": {
            "letters": [_letter_dict(g) for g in letters],
            "total": total,
            "page": page,
            "per_page": per_page,
        },
    }), 200


@bp.route("/read", methods=["POST"])
@jwt_required()
def gratitude_read():
    """标记信件已读（连带把送达提醒通知一并标为已读，保持铃铛角标一致）"""
    user = _get_current_user()
    if not user:
        return jsonify({"code": 404, "message": "用户不存在"}), 404

    data = request.get_json() or {}
    gid = data.get("id")
    if not gid:
        return jsonify({"code": 400, "message": "缺少参数"}), 400

    g = GratitudeModel.query.filter_by(id=gid).first()
    if not g or g.recipient_id != user.id:
        return jsonify({"code": 404, "message": "感谢信不存在"}), 404

    if not g.is_read:
        g.is_read = True
    NotificationModel.query.filter_by(
        user_id=user.id, category='gratitude', source_id=g.id,
    ).update({"is_read": True})
    db.session.commit()
    return jsonify({"code": 200, "message": "已标记已读"}), 200

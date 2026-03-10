from flask import Blueprint, request, jsonify
from flask_cors import cross_origin
from sqlalchemy import and_, or_
from datetime import datetime

from exts import db
from models import UserModel, DiscussionThread, DiscussionReply, DiscussionReaction, CourseGroup, CourseGroupMember
from flask_jwt_extended import get_jwt_identity, jwt_required

bp = Blueprint("discussion", __name__, url_prefix="/discussions")

# CORS 配置
_cors_config = {
    "origins": "*",
    "methods": ["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    "allow_headers": ["Content-Type", "Authorization"]
}


@bp.route('', defaults={'path': ''}, methods=['OPTIONS'])
@bp.route('/<path:path>', methods=['OPTIONS'])
@cross_origin(**_cors_config)
def options_handler(path):
    return jsonify({"code": 200}), 200


# ==================== 权限辅助函数 ====================

def get_current_user():
    """获取当前登录用户"""
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    return user


def can_view_thread(thread, user):
    """检查用户是否可以查看主题帖"""
    if not user:
        return False

    # 管理员可见
    if user.user_mode == 'admin':
        return True

    # 已删除/隐藏的帖子，管理员和作者可见
    if thread.status in [DiscussionThread.STATUS_DELETED, DiscussionThread.STATUS_HIDDEN]:
        return thread.author_id == user.id

    # global: 登录用户可见
    if thread.scope_type == 'global':
        return True

    # group: 小组成员可见
    if thread.scope_type == 'group':
        member = CourseGroupMember.query.filter_by(
            group_id=thread.scope_id,
            student_id=user.id
        ).first()
        return member is not None

    # task: 暂按小组成员可见（后续扩展）
    if thread.scope_type == 'task':
        # TODO: 扩展 task 可见性判断
        return True

    # course: 选课学生可见
    if thread.scope_type == 'course':
        from models import UserCourseModel
        enrollment = UserCourseModel.query.filter_by(
            user_id=user.id,
            course_id=thread.scope_id
        ).first()
        return enrollment is not None

    # article: 文章可见用户
    if thread.scope_type == 'article':
        from models import ArticleModel
        article = ArticleModel.query.get(thread.scope_id)
        if not article:
            return False
        # TODO: 根据文章可见性判断
        return True

    return False


def can_post_thread(scope_type, scope_id, user):
    """检查用户是否可以发帖"""
    if not user:
        return False

    if user.user_mode == 'admin':
        return True

    if scope_type == 'global':
        return True

    if scope_type == 'group':
        member = CourseGroupMember.query.filter_by(
            group_id=scope_id,
            student_id=user.id
        ).first()
        return member is not None

    if scope_type == 'task':
        return True

    if scope_type == 'course':
        from models import UserCourseModel
        enrollment = UserCourseModel.query.filter_by(
            user_id=user.id,
            course_id=scope_id
        ).first()
        return enrollment is not None

    return False


def can_moderate_thread(thread, user):
    """检查用户是否可以管理主题帖（置顶/锁帖等）"""
    if not user:
        return False

    # 管理员可管理
    if user.user_mode == 'admin':
        return True

    # 作者可管理自己的帖子
    return thread.author_id == user.id


# ==================== 主题帖 CRUD ====================

# 创建主题帖 POST /discussions/threads
@bp.route("/threads", methods=["POST"])
@jwt_required()
def create_thread():
    """创建主题帖"""
    user = get_current_user()
    if not user:
        return jsonify({"code": 401, "message": "用户不存在"}), 401

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"code": 400, "message": "请求参数错误"}), 400

    title = data.get('title')
    content = data.get('content')
    scope_type = data.get('scope_type', 'global')
    scope_id = data.get('scope_id')  # global 时可为空

    if not title or not content:
        return jsonify({"code": 400, "message": "标题和内容不能为空"}), 400

    # 校验 scope_type
    valid_scopes = ['global', 'article', 'course', 'group', 'task']
    if scope_type not in valid_scopes:
        return jsonify({"code": 400, "message": f"scope_type 必须为: {', '.join(valid_scopes)}"}), 400

    # 非 global 类型需要 scope_id
    if scope_type != 'global' and not scope_id:
        return jsonify({"code": 400, "message": "非全局讨论需要指定 scope_id"}), 400

    # 权限检查
    if not can_post_thread(scope_type, scope_id, user):
        return jsonify({"code": 403, "message": "无权限在此范围发帖"}), 403

    thread = DiscussionThread(
        title=title,
        content=content,
        scope_type=scope_type,
        scope_id=scope_id,
        author_id=user.id
    )
    db.session.add(thread)
    db.session.commit()

    return jsonify({
        "code": 201,
        "message": "created",
        "data": {
            "id": thread.id,
            "title": thread.title,
            "content": thread.content,
            "scope_type": thread.scope_type,
            "scope_id": thread.scope_id,
            "author_id": thread.author_id,
            "status": thread.status,
            "is_pinned": thread.is_pinned,
            "reply_count": thread.reply_count,
            "like_count": thread.like_count,
            "created_at": thread.created_at.strftime('%Y-%m-%d %H:%M:%S')
        }
    }), 201


# 主题帖列表 GET /discussions/threads
@bp.route("/threads", methods=["GET"])
@jwt_required()
def list_threads():
    """获取主题帖列表（分页）"""
    user = get_current_user()
    if not user:
        return jsonify({"code": 401, "message": "用户不存在"}), 401

    # 分页参数
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 10, type=int)
    per_page = min(per_page, 50)  # 限制最大每页数量

    # 过滤参数
    scope_type = request.args.get('scope_type')
    scope_id = request.args.get('scope_id', type=int)
    status = request.args.get('status', DiscussionThread.STATUS_NORMAL)
    sort = request.args.get('sort', 'latest')  # latest/pinned

    query = DiscussionThread.query

    # 权限过滤：只返回用户有权限查看的帖子
    if user.user_mode != 'admin':
        # global 或非 group 的帖子
        or_conditions = [
            and_(
                DiscussionThread.scope_type == 'global',
                DiscussionThread.status != DiscussionThread.STATUS_DELETED
            )
        ]

        # group 范围：用户加入的小组
        group_ids = [m.group_id for m in CourseGroupMember.query.filter_by(student_id=user.id).all()]
        if group_ids:
            or_conditions.append(and_(
                DiscussionThread.scope_type == 'group',
                DiscussionThread.scope_id.in_(group_ids),
                DiscussionThread.status != DiscussionThread.STATUS_DELETED
            ))

        # 其他 scope_type 暂不做过滤
        query = query.filter(or_(*or_conditions))

    # 应用过滤条件
    if scope_type:
        query = query.filter(DiscussionThread.scope_type == scope_type)
    if scope_id:
        query = query.filter(DiscussionThread.scope_id == scope_id)
    if status:
        query = query.filter(DiscussionThread.status == status)

    # 排序
    if sort == 'pinned':
        query = query.order_by(DiscussionThread.is_pinned.desc(), DiscussionThread.last_reply_at.desc())
    else:
        query = query.order_by(DiscussionThread.is_pinned.desc(), DiscussionThread.created_at.desc())

    # 分页
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)

    result = []
    for thread in pagination.items:
        # 检查用户是否有权限查看（用于隐藏状态帖子）
        if thread.status in [DiscussionThread.STATUS_HIDDEN, DiscussionThread.STATUS_DELETED]:
            if thread.author_id != user.id and user.user_mode != 'admin':
                continue

        result.append({
            "id": thread.id,
            "title": thread.title,
            "scope_type": thread.scope_type,
            "scope_id": thread.scope_id,
            "author_id": thread.author_id,
            "author_name": thread.author.username if thread.author else "",
            "status": thread.status,
            "is_pinned": thread.is_pinned,
            "reply_count": thread.reply_count,
            "like_count": thread.like_count,
            "view_count": thread.view_count,
            "last_reply_at": thread.last_reply_at.strftime('%Y-%m-%d %H:%M:%S') if thread.last_reply_at else None,
            "created_at": thread.created_at.strftime('%Y-%m-%d %H:%M:%S')
        })

    return jsonify({
        "code": 200,
        "data": result,
        "total": pagination.total,
        "page": page,
        "per_page": per_page,
        "pages": pagination.pages
    })


# 主题帖详情 GET /discussions/threads/{thread_id}
@bp.route("/threads/<int:thread_id>", methods=["GET"])
@jwt_required()
def get_thread(thread_id):
    """获取主题帖详情"""
    user = get_current_user()
    if not user:
        return jsonify({"code": 401, "message": "用户不存在"}), 401

    thread = DiscussionThread.query.get(thread_id)
    if not thread:
        return jsonify({"code": 404, "message": "帖子不存在"}), 404

    # 权限检查
    if not can_view_thread(thread, user):
        return jsonify({"code": 403, "message": "无权限查看"}), 403

    # 增加浏览计数
    thread.view_count += 1
    db.session.commit()

    return jsonify({
        "code": 200,
        "data": {
            "id": thread.id,
            "title": thread.title,
            "content": thread.content,
            "scope_type": thread.scope_type,
            "scope_id": thread.scope_id,
            "author_id": thread.author_id,
            "author_name": thread.author.username if thread.author else "",
            "status": thread.status,
            "is_pinned": thread.is_pinned,
            "reply_count": thread.reply_count,
            "like_count": thread.like_count,
            "view_count": thread.view_count,
            "last_reply_at": thread.last_reply_at.strftime('%Y-%m-%d %H:%M:%S') if thread.last_reply_at else None,
            "created_at": thread.created_at.strftime('%Y-%m-%d %H:%M:%S'),
            "updated_at": thread.updated_at.strftime('%Y-%m-%d %H:%M:%S') if thread.updated_at else None
        }
    })


# 编辑主题帖 PUT /discussions/threads/{thread_id}
@bp.route("/threads/<int:thread_id>", methods=["PUT"])
@jwt_required()
def update_thread(thread_id):
    """编辑主题帖"""
    user = get_current_user()
    if not user:
        return jsonify({"code": 401, "message": "用户不存在"}), 401

    thread = DiscussionThread.query.get(thread_id)
    if not thread:
        return jsonify({"code": 404, "message": "帖子不存在"}), 404

    # 权限检查：作者或管理员
    if thread.author_id != user.id and user.user_mode != 'admin':
        return jsonify({"code": 403, "message": "无权限编辑"}), 403

    data = request.get_json(silent=True) or {}

    if 'title' in data:
        thread.title = data['title']
    if 'content' in data:
        thread.content = data['content']

    db.session.commit()

    return jsonify({
        "code": 200,
        "message": "updated",
        "data": {
            "id": thread.id,
            "title": thread.title,
            "content": thread.content,
            "updated_at": thread.updated_at.strftime('%Y-%m-%d %H:%M:%S')
        }
    })


# 删除主题帖 DELETE /discussions/threads/{thread_id}
@bp.route("/threads/<int:thread_id>", methods=["DELETE"])
@jwt_required()
def delete_thread(thread_id):
    """删除主题帖（软删除）"""
    user = get_current_user()
    if not user:
        return jsonify({"code": 401, "message": "用户不存在"}), 401

    thread = DiscussionThread.query.get(thread_id)
    if not thread:
        return jsonify({"code": 404, "message": "帖子不存在"}), 404

    # 权限检查：作者或管理员
    if thread.author_id != user.id and user.user_mode != 'admin':
        return jsonify({"code": 403, "message": "无权限删除"}), 403

    # 软删除
    thread.status = DiscussionThread.STATUS_DELETED
    db.session.commit()

    return jsonify({
        "code": 200,
        "message": "deleted"
    })


# 置顶/取消置顶 POST /discussions/threads/{thread_id}/pin
@bp.route("/threads/<int:thread_id>/pin", methods=["POST"])
@jwt_required()
def pin_thread(thread_id):
    """置顶/取消置顶主题帖"""
    user = get_current_user()
    if not user:
        return jsonify({"code": 401, "message": "用户不存在"}), 401

    thread = DiscussionThread.query.get(thread_id)
    if not thread:
        return jsonify({"code": 404, "message": "帖子不存在"}), 404

    # 权限检查
    if not can_moderate_thread(thread, user):
        return jsonify({"code": 403, "message": "无权限操作"}), 403

    # 切换置顶状态
    thread.is_pinned = not thread.is_pinned
    db.session.commit()

    return jsonify({
        "code": 200,
        "message": "pinned" if thread.is_pinned else "unpinned",
        "data": {
            "is_pinned": thread.is_pinned
        }
    })


# 锁帖/解锁 POST /discussions/threads/{thread_id}/lock
@bp.route("/threads/<int:thread_id>/lock", methods=["POST"])
@jwt_required()
def lock_thread(thread_id):
    """锁帖/解锁主题帖"""
    user = get_current_user()
    if not user:
        return jsonify({"code": 401, "message": "用户不存在"}), 401

    thread = DiscussionThread.query.get(thread_id)
    if not thread:
        return jsonify({"code": 404, "message": "帖子不存在"}), 404

    # 权限检查
    if not can_moderate_thread(thread, user):
        return jsonify({"code": 403, "message": "无权限操作"}), 403

    # 切换锁帖状态
    if thread.status == DiscussionThread.STATUS_LOCKED:
        thread.status = DiscussionThread.STATUS_NORMAL
    else:
        thread.status = DiscussionThread.STATUS_LOCKED

    db.session.commit()

    return jsonify({
        "code": 200,
        "message": "locked" if thread.status == DiscussionThread.STATUS_LOCKED else "unlocked",
        "data": {
            "status": thread.status
        }
    })


# ==================== 回复功能 ====================

# 回复主题帖 POST /discussions/threads/{thread_id}/replies
@bp.route("/threads/<int:thread_id>/replies", methods=["POST"])
@jwt_required()
def create_reply(thread_id):
    """回复主题帖"""
    user = get_current_user()
    if not user:
        return jsonify({"code": 401, "message": "用户不存在"}), 401

    thread = DiscussionThread.query.get(thread_id)
    if not thread:
        return jsonify({"code": 404, "message": "帖子不存在"}), 404

    # 权限检查
    if not can_view_thread(thread, user):
        return jsonify({"code": 403, "message": "无权限查看"}), 403

    # 锁帖状态下不允许回复
    if thread.status == DiscussionThread.STATUS_LOCKED:
        return jsonify({"code": 403, "message": "帖子已锁定，无法回复"}), 403

    data = request.get_json(silent=True) or {}
    content = data.get('content')
    parent_reply_id = data.get('parent_reply_id')  # 楼中楼回复

    if not content:
        return jsonify({"code": 400, "message": "回复内容不能为空"}), 400

    # 权限检查：发帖权限
    if not can_post_thread(thread.scope_type, thread.scope_id, user):
        return jsonify({"code": 403, "message": "无权限回复"}), 403

    # 楼中楼回复：检查父回复是否存在
    if parent_reply_id:
        parent_reply = DiscussionReply.query.get(parent_reply_id)
        if not parent_reply or parent_reply.thread_id != thread_id:
            return jsonify({"code": 400, "message": "无效的父回复"}), 400

    reply = DiscussionReply(
        thread_id=thread_id,
        parent_reply_id=parent_reply_id,
        author_id=user.id,
        content=content
    )
    db.session.add(reply)

    # 更新主题帖的回复计数和最后回复时间
    thread.reply_count += 1
    thread.last_reply_at = datetime.now()

    db.session.commit()

    return jsonify({
        "code": 201,
        "message": "created",
        "data": {
            "id": reply.id,
            "thread_id": reply.thread_id,
            "parent_reply_id": reply.parent_reply_id,
            "author_id": reply.author_id,
            "content": reply.content,
            "status": reply.status,
            "like_count": reply.like_count,
            "created_at": reply.created_at.strftime('%Y-%m-%d %H:%M:%S')
        }
    }), 201


# 回复列表 GET /discussions/threads/{thread_id}/replies
@bp.route("/threads/<int:thread_id>/replies", methods=["GET"])
@jwt_required()
def list_replies(thread_id):
    """获取回复列表"""
    user = get_current_user()
    if not user:
        return jsonify({"code": 401, "message": "用户不存在"}), 401

    thread = DiscussionThread.query.get(thread_id)
    if not thread:
        return jsonify({"code": 404, "message": "帖子不存在"}), 404

    # 权限检查
    if not can_view_thread(thread, user):
        return jsonify({"code": 403, "message": "无权限查看"}), 403

    # 分页参数
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)
    per_page = min(per_page, 50)

    # 只返回正常状态的回复
    query = DiscussionReply.query.filter_by(
        thread_id=thread_id,
        status=DiscussionReply.STATUS_NORMAL
    )

    # 楼中楼需要特殊处理：先查顶级回复，再关联子回复
    # 为简化，首期只返回一级回复（不含楼中楼）
    query = query.filter(DiscussionReply.parent_reply_id == None)

    query = query.order_by(DiscussionReply.created_at.asc())

    pagination = query.paginate(page=page, per_page=per_page, error_out=False)

    result = []
    for reply in pagination.items:
        # 获取子回复（楼中楼）
        children = []
        for child in reply.children.filter_by(status=DiscussionReply.STATUS_NORMAL).all():
            children.append({
                "id": child.id,
                "author_id": child.author_id,
                "author_name": child.author.username if child.author else "",
                "content": child.content,
                "like_count": child.like_count,
                "created_at": child.created_at.strftime('%Y-%m-%d %H:%M:%S')
            })

        result.append({
            "id": reply.id,
            "author_id": reply.author_id,
            "author_name": reply.author.username if reply.author else "",
            "content": reply.content,
            "like_count": reply.like_count,
            "created_at": reply.created_at.strftime('%Y-%m-%d %H:%M:%S'),
            "children": children
        })

    return jsonify({
        "code": 200,
        "data": result,
        "total": pagination.total,
        "page": page,
        "per_page": per_page,
        "pages": pagination.pages
    })


# 编辑回复 PUT /discussions/replies/{reply_id}
@bp.route("/replies/<int:reply_id>", methods=["PUT"])
@jwt_required()
def update_reply(reply_id):
    """编辑回复"""
    user = get_current_user()
    if not user:
        return jsonify({"code": 401, "message": "用户不存在"}), 401

    reply = DiscussionReply.query.get(reply_id)
    if not reply:
        return jsonify({"code": 404, "message": "回复不存在"}), 404

    # 权限检查：作者或管理员
    if reply.author_id != user.id and user.user_mode != 'admin':
        return jsonify({"code": 403, "message": "无权限编辑"}), 403

    data = request.get_json(silent=True) or {}
    if 'content' in data:
        reply.content = data['content']

    db.session.commit()

    return jsonify({
        "code": 200,
        "message": "updated",
        "data": {
            "id": reply.id,
            "content": reply.content,
            "updated_at": reply.updated_at.strftime('%Y-%m-%d %H:%M:%S')
        }
    })


# 删除回复 DELETE /discussions/replies/{reply_id}
@bp.route("/replies/<int:reply_id>", methods=["DELETE"])
@jwt_required()
def delete_reply(reply_id):
    """删除回复（软删除）"""
    user = get_current_user()
    if not user:
        return jsonify({"code": 401, "message": "用户不存在"}), 401

    reply = DiscussionReply.query.get(reply_id)
    if not reply:
        return jsonify({"code": 404, "message": "回复不存在"}), 404

    # 权限检查：作者或管理员
    if reply.author_id != user.id and user.user_mode != 'admin':
        return jsonify({"code": 403, "message": "无权限删除"}), 403

    # 软删除
    reply.status = DiscussionReply.STATUS_DELETED

    # 更新主题帖回复计数
    thread = reply.thread
    if thread and thread.reply_count > 0:
        thread.reply_count -= 1

    db.session.commit()

    return jsonify({
        "code": 200,
        "message": "deleted"
    })


# ==================== 点赞功能 ====================

# 点赞/取消点赞 POST /discussions/reactions
@bp.route("/reactions", methods=["POST"])
@jwt_required()
def toggle_reaction():
    """点赞或取消点赞"""
    user = get_current_user()
    if not user:
        return jsonify({"code": 401, "message": "用户不存在"}), 401

    data = request.get_json(silent=True) or {}
    target_type = data.get('target_type')  # thread/reply
    target_id = data.get('target_id')
    reaction_type = data.get('reaction_type', 'like')

    if not target_type or not target_id:
        return jsonify({"code": 400, "message": "target_type 和 target_id 不能为空"}), 400

    if target_type not in ['thread', 'reply']:
        return jsonify({"code": 400, "message": "target_type 必须为 thread 或 reply"}), 400

    # 检查目标是否存在
    if target_type == 'thread':
        target = DiscussionThread.query.get(target_id)
        if not target:
            return jsonify({"code": 404, "message": "帖子不存在"}), 404
        # 权限检查
        if not can_view_thread(target, user):
            return jsonify({"code": 403, "message": "无权限"}), 403
    else:
        target = DiscussionReply.query.get(target_id)
        if not target:
            return jsonify({"code": 404, "message": "回复不存在"}), 404

    # 检查是否已点赞
    existing = DiscussionReaction.query.filter_by(
        user_id=user.id,
        target_type=target_type,
        target_id=target_id,
        reaction_type=reaction_type
    ).first()

    if existing:
        # 取消点赞
        db.session.delete(existing)
        # 更新计数
        if target_type == 'thread':
            target.like_count = max(0, target.like_count - 1)
        else:
            target.like_count = max(0, target.like_count - 1)
        db.session.commit()
        liked = False
    else:
        # 点赞
        reaction = DiscussionReaction(
            user_id=user.id,
            target_type=target_type,
            target_id=target_id,
            reaction_type=reaction_type
        )
        db.session.add(reaction)
        # 更新计数
        if target_type == 'thread':
            target.like_count += 1
        else:
            target.like_count += 1
        db.session.commit()
        liked = True

    return jsonify({
        "code": 200,
        "message": "liked" if liked else "unliked",
        "data": {
            "target_type": target_type,
            "target_id": target_id,
            "liked": liked
        }
    })


# 获取当前用户点赞状态 GET /discussions/threads/{thread_id}/reactions/me
@bp.route("/threads/<int:thread_id>/reactions/me", methods=["GET"])
@jwt_required()
def get_my_reaction(thread_id):
    """获取当前用户对主题帖的点赞状态"""
    user = get_current_user()
    if not user:
        return jsonify({"code": 401, "message": "用户不存在"}), 401

    thread = DiscussionThread.query.get(thread_id)
    if not thread:
        return jsonify({"code": 404, "message": "帖子不存在"}), 404

    reaction = DiscussionReaction.query.filter_by(
        user_id=user.id,
        target_type='thread',
        target_id=thread_id,
        reaction_type='like'
    ).first()

    return jsonify({
        "code": 200,
        "data": {
            "thread_id": thread_id,
            "liked": reaction is not None
        }
    })

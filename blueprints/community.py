from flask import Blueprint, request, jsonify
from flask_cors import cross_origin

from exts import db
from models import UserModel, DiscussionThread, DiscussionReaction, ArticleModel
from flask_jwt_extended import get_jwt_identity, jwt_required

bp = Blueprint("community", __name__, url_prefix="/community")

# CORS 配置（与 discussion 蓝图保持一致）
_cors_config = {
    "origins": "*",
    "methods": ["GET", "OPTIONS"],
    "allow_headers": ["Content-Type", "Authorization"],
}


@bp.route('', defaults={'path': ''}, methods=['OPTIONS'])
@bp.route('/<path:path>', methods=['OPTIONS'])
@cross_origin(**_cors_config)
def options_handler(path):
    return jsonify({"code": 200}), 200


# ==================== 辅助函数 ====================

def get_current_user():
    """获取当前登录用户"""
    user_email = get_jwt_identity()
    if not user_email:
        return None
    return UserModel.query.filter_by(email=user_email).first()


def get_avatar_url(avatar_url):
    """获取完整的头像URL（与 discussion 蓝图同逻辑）"""
    if not avatar_url:
        return ""
    if avatar_url.startswith('http://') or avatar_url.startswith('https://'):
        return avatar_url
    base_url = request.host_url.rstrip('/')
    return f"{base_url}/data/avatars/{avatar_url}"


# ==================== 社区广场聚合信息流 ====================

# GET /community/feed
# 聚合「global 讨论帖」+「全部文章」为统一格式混合流，供首页/主社区广场展示。
# 文章帖与讨论帖通过 type 字段区分；文章帖带 article_id 供前端跳转文章详情。
@bp.route("/feed", methods=["GET"])
@jwt_required()
def community_feed():
    """社区广场聚合信息流：讨论帖 + 文章，按时间倒序（置顶优先）。"""
    user = get_current_user()
    if not user:
        return jsonify({"code": 401, "message": "用户不存在"}), 401

    # 分页参数
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 10, type=int)
    per_page = min(per_page, 50)
    sort = request.args.get('sort', 'latest')  # latest | pinned（首期均按「置顶优先 + 时间倒序」）

    is_admin = user.user_mode == 'admin'

    # 批量预取，避免 N+1：
    # 1) 当前用户点赞过的 thread id 集合
    liked_thread_ids = {
        r.target_id for r in DiscussionReaction.query.filter_by(
            user_id=user.id, target_type='thread', reaction_type='like'
        ).all()
    }
    # 2) 文章评论数：每篇文章所有 article-scope 汇总 thread 的 reply_count 之和
    article_reply_map = {}
    for t in DiscussionThread.query.filter_by(
        scope_type='article', status=DiscussionThread.STATUS_NORMAL
    ).all():
        article_reply_map[t.scope_id] = article_reply_map.get(t.scope_id, 0) + (t.reply_count or 0)

    items = []

    # ── 1. global 讨论帖（与 list_threads 权限口径一致） ──
    thread_query = DiscussionThread.query.filter(DiscussionThread.scope_type == 'global')
    if not is_admin:
        thread_query = thread_query.filter(DiscussionThread.status != DiscussionThread.STATUS_DELETED)
    for t in thread_query.all():
        # 隐藏/删除帖仅作者与管理员可见
        if t.status in [DiscussionThread.STATUS_HIDDEN, DiscussionThread.STATUS_DELETED]:
            if t.author_id != user.id and not is_admin:
                continue
        items.append({
            "type": "discussion",
            "id": t.id,
            "title": t.title,
            "summary": (t.content or '')[:200],
            "author_id": t.author_id,
            "author_name": t.author.username if t.author else "",
            "author_avatar": get_avatar_url(t.author.avatar_url) if t.author else "",
            "created_at": t.created_at.strftime('%Y-%m-%d %H:%M:%S') if t.created_at else "",
            "like_count": t.like_count or 0,
            "reply_count": t.reply_count or 0,
            "view_count": t.view_count or 0,
            "liked": t.id in liked_thread_ids,
            "is_pinned": bool(t.is_pinned),
            "article_id": None,
        })

    # ── 2. 文章（沿用 article_list 无可见性过滤，全部可见） ──
    for a in ArticleModel.query.all():
        items.append({
            "type": "article",
            "id": a.id,
            "title": a.title,
            "summary": (a.introduction or '')[:200],
            "author_id": a.author_id,
            "author_name": a.author.username if a.author else "",
            "author_avatar": get_avatar_url(a.author.avatar_url) if a.author else "",
            "created_at": a.publish_time.strftime('%Y-%m-%d %H:%M:%S') if a.publish_time else "",
            "like_count": 0,                       # 文章卡不展示点赞
            "reply_count": article_reply_map.get(a.id, 0),  # = 文章评论数
            "view_count": 0,
            "liked": False,
            "is_pinned": False,
            "article_id": a.id,                    # 供前端跳转文章详情
        })

    # 排序：置顶优先，再按创建时间倒序（时间字符串字典序即时间序）
    items.sort(key=lambda x: (x['is_pinned'], x['created_at']), reverse=True)

    # 内存分页
    total = len(items)
    pages = (total + per_page - 1) // per_page if per_page > 0 else 0
    start = (page - 1) * per_page
    page_items = items[start:start + per_page]

    return jsonify({
        "code": 200,
        "data": page_items,
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": pages,
    })

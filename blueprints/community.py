from flask import Blueprint, request, jsonify
from flask_cors import cross_origin
from datetime import datetime

from exts import db
from models import UserModel, DiscussionThread, DiscussionReaction, ArticleModel, ArticleV2Model
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


# ==================== 热度排序 ====================

# 半衰期（天）：内容每过这么多天，热度衰减一半。
# 这是热度公式唯一的主调节旋钮——若好内容掉得太快，先调大这里（14→21→30）再动互动权重。
_HALF_LIFE_DAYS = 14


def _hot_score(interaction, activity_dt, now):
    """半衰期热度分：HOT = (互动分 + 1) * 0.5 ^ (age_days / 半衰期)。

    - +1 给新内容基础分，零互动也有分（=1）不会沉底；
    - 用真实 datetime 算 age，不要传格式化字符串；
    - activity_dt 为空时回退到 now（age=0，仅基础分）。
    注意：now 必须与模型 default=datetime.now() 同源（本地时间），勿用 utcnow()。
    """
    if activity_dt is None:
        activity_dt = now
    age_days = max((now - activity_dt).total_seconds() / 86400.0, 0.0)
    return (interaction + 1.0) * (0.5 ** (age_days / _HALF_LIFE_DAYS))


# ==================== 社区广场聚合信息流 ====================

# GET /community/feed
# 聚合「global 讨论帖」+「全部文章」为统一格式混合流，供首页/主社区广场展示。
# 文章帖与讨论帖通过 type 字段区分；文章帖带 article_id 供前端跳转文章详情。
@bp.route("/feed", methods=["GET"])
@jwt_required()
def community_feed():
    """社区广场聚合信息流：讨论帖 + 文章。
    type 筛选 all/article/discussion；sort 排序 hot(半衰期热度)/latest(活跃时间倒序)；置顶绝对优先。
    """
    user = get_current_user()
    if not user:
        return jsonify({"code": 401, "message": "用户不存在"}), 401

    # 分页参数
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 10, type=int)
    per_page = min(per_page, 50)
    # 内容类型筛选：all(默认) | article | discussion
    content_type = request.args.get('type', 'all')
    # 排序：hot(默认，半衰期热度) | latest(按活跃时间倒序)
    sort = request.args.get('sort', 'hot')
    # 整个请求只取一次 now，与模型 default=datetime.now() 同源（本地时间），勿用 utcnow()
    now = datetime.now()

    is_admin = user.user_mode == 'admin'

    # 批量预取，避免 N+1：
    # 1) 当前用户点赞过的 thread id 集合
    liked_thread_ids = {
        r.target_id for r in DiscussionReaction.query.filter_by(
            user_id=user.id, target_type='thread', reaction_type='like'
        ).all()
    }
    # 2) 文章评论数 + 文章最近评论时间（同一循环同时构建，无额外查询）
    #    article_last_reply_map 让有新评论的文章也能 bump 浮起，否则文章 T 永远停在 publish_time
    article_reply_map = {}
    article_last_reply_map = {}
    for t in DiscussionThread.query.filter_by(
        scope_type='article', status=DiscussionThread.STATUS_NORMAL
    ).all():
        sid = t.scope_id
        article_reply_map[sid] = article_reply_map.get(sid, 0) + (t.reply_count or 0)
        if t.last_reply_at is not None:
            cur = article_last_reply_map.get(sid)
            article_last_reply_map[sid] = t.last_reply_at if cur is None else max(cur, t.last_reply_at)

    items = []

    # ── 1. global 讨论帖（与 list_threads 权限口径一致） ──
    thread_query = DiscussionThread.query.filter(DiscussionThread.scope_type == 'global')
    if not is_admin:
        thread_query = thread_query.filter(DiscussionThread.status != DiscussionThread.STATUS_DELETED)
    for t in thread_query.all():
        # 类型筛选：当前只要文章时跳过讨论帖（数据量小，循环内过滤开销可忽略）
        if content_type == 'article':
            continue
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
            # ── 排序用私有字段（返回前剔除，不下发客户端）──
            "_interaction": (t.like_count or 0) + 2 * (t.reply_count or 0),
            "_rank_dt": t.last_reply_at or t.created_at,
        })

    # ── 2. 文章（沿用 article_list 无可见性过滤，全部可见） ──
    for a in ArticleModel.query.all():
        # 类型筛选：当前只要讨论时跳过文章
        if content_type == 'discussion':
            continue
        rc = article_reply_map.get(a.id, 0)
        # 活跃时间 T = max(发布时间, 最近评论时间)；都为空回退 now（age=0，仅基础分）
        rank_dt = a.publish_time
        last_rep = article_last_reply_map.get(a.id)
        if last_rep is not None:
            rank_dt = last_rep if rank_dt is None else max(rank_dt, last_rep)
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
            "reply_count": rc,                     # = 文章评论数
            "view_count": 0,
            "liked": False,
            "is_pinned": False,                    # ArticleModel 无 is_pinned，文章暂不可置顶
            "article_id": a.id,                    # 供前端跳转文章详情
            # ── 排序用私有字段（返回前剔除，不下发客户端）──
            "_interaction": 2 * rc,
            "_rank_dt": rank_dt or now,
            "_article_reply_count": rc,            # 确定性平局打破
        })

    # ── 3. V2 文章（Markdown，article_v2 表；与旧文章同格式并入信息流） ──
    # V2 第一版无评论/点赞统计：reply/like/view 均为 0，仅按发布时间参与排序
    for a in ArticleV2Model.query.all():
        if content_type == 'discussion':
            continue
        items.append({
            "type": "article",
            "id": a.id,
            "title": a.title,
            "summary": (a.introduction or '')[:200],
            "author_id": a.author_id,
            "author_name": a.author.username if a.author else "",
            "author_avatar": get_avatar_url(a.author.avatar_url) if a.author else "",
            "created_at": a.publish_time.strftime('%Y-%m-%d %H:%M:%S') if a.publish_time else "",
            "like_count": 0,
            "reply_count": 0,                       # V2 第一版无评论
            "view_count": 0,
            "liked": False,
            "is_pinned": False,
            "article_id": a.id,
            "article_version": 2,                   # 前端据此跳 /article-v2
            "_interaction": 0,
            "_rank_dt": a.publish_time or now,
            "_article_reply_count": 0,
        })

    # 排序：置顶(is_pinned)绝对优先 → 热度分(hot)或活跃时间(latest) → 确定性平局打破
    # 用真实 datetime（_rank_dt）排序，勿用格式化字符串（空串会错误沉底）
    if sort == 'hot':
        items.sort(key=lambda x: (
            x['is_pinned'],
            _hot_score(x['_interaction'], x['_rank_dt'], now),
            x.get('_article_reply_count', x['reply_count'] or 0),
            x['_rank_dt'],
        ), reverse=True)
    else:  # latest：按活跃时间倒序
        items.sort(key=lambda x: (
            x['is_pinned'],
            x['_rank_dt'],
            x.get('_article_reply_count', x['reply_count'] or 0),
        ), reverse=True)

    # 内存分页
    total = len(items)
    pages = (total + per_page - 1) // per_page if per_page > 0 else 0
    start = (page - 1) * per_page
    page_items = items[start:start + per_page]
    # 下发前剔除 _ 前缀私有排序字段，避免污染 API schema（两个前端调用者都依赖公共字段）
    page_items = [{k: v for k, v in it.items() if not k.startswith('_')} for it in page_items]

    return jsonify({
        "code": 200,
        "data": page_items,
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": pages,
    })

import json
import itertools

from flask import Blueprint, request, jsonify
from flask_cors import cross_origin
from datetime import datetime

from exts import db, redis_client
from sqlalchemy.orm import joinedload
from models import UserModel, DiscussionThread, DiscussionReply, DiscussionReaction, ArticleModel, ArticleV2Model
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
    # 相对路径：浏览器按当前页 origin 解析，避免绝对 URL 的 host/端口在反代/端口转发下失配
    return f"/data/avatars/{avatar_url}"


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

def _serialize_reply(r):
    """把 DiscussionReply 序列化为 feed 预览 shape（对齐 discussion.list_replies 的顶级回复）。
    liked 恒为 False：回复预览随公共缓存下发，用户特定的点赞状态由 community_feed 命中后回填。
    """
    return {
        "id": r.id,
        "author_id": r.author_id,
        "author_name": r.author.username if r.author else "",
        "author_avatar": get_avatar_url(r.author.avatar_url) if r.author else "",
        "content": r.content,
        "like_count": r.like_count,
        "liked": False,
        "created_at": r.created_at.strftime('%Y-%m-%d %H:%M:%S') if r.created_at else "",
    }


def _build_feed_page(content_type, sort, page, per_page, is_admin, now):
    """构建 feed 的公共分页（不含任何用户特定状态），结果可被多用户共享缓存。

    返回 (page_items, total, pages)。每项保留 `_like_tid` 供调用方回填 liked 后剔除；
    讨论帖项额外挂 `replies`（前 2 条顶级回复预览）。

    语义说明：非管理员视角只展示 STATUS_NORMAL 的讨论帖（管理员视角仍含 hidden/deleted）。
    这是为了让公共缓存不依赖具体 user.id——"作者在主 feed 看到自己隐藏帖"的旧边缘行为随之取消，
    作者管理自己的隐藏帖请在详情/个人页进行。
    """
    # ── 预取文章互动数（v1 + v2），避免正文循环内查库 ──
    # article_last_reply_map 让有新评论的文章也能 bump 浮起，否则文章活跃时间永远停在 publish_time
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

    # V2 文章互动数（reply/like/view）+ thread_id 映射；只读，绝不在此为 v2 文章新建 thread
    v2_reply_map = {}
    v2_like_map = {}
    v2_view_map = {}
    v2_thread_id_map = {}
    v2_last_reply_map = {}
    for t in DiscussionThread.query.filter_by(
        scope_type='article_v2', status=DiscussionThread.STATUS_NORMAL
    ).all():
        sid = t.scope_id
        v2_reply_map[sid] = v2_reply_map.get(sid, 0) + (t.reply_count or 0)
        v2_like_map[sid] = v2_like_map.get(sid, 0) + (t.like_count or 0)
        v2_view_map[sid] = v2_view_map.get(sid, 0) + (t.view_count or 0)
        v2_thread_id_map[sid] = t.id
        if t.last_reply_at is not None:
            cur = v2_last_reply_map.get(sid)
            v2_last_reply_map[sid] = t.last_reply_at if cur is None else max(cur, t.last_reply_at)

    items = []

    # ── 1. global 讨论帖（管理员见全部状态，非管理员只见 normal） ──
    thread_query = DiscussionThread.query.filter(
        DiscussionThread.scope_type == 'global'
    ).options(joinedload(DiscussionThread.author))
    if is_admin:
        pass  # 管理员可见所有状态
    else:
        thread_query = thread_query.filter(DiscussionThread.status == DiscussionThread.STATUS_NORMAL)
    for t in thread_query.all():
        # 类型筛选：当前只要文章时跳过讨论帖（数据量小，循环内过滤开销可忽略）
        if content_type == 'article':
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
            "liked": False,
            "is_pinned": bool(t.is_pinned),
            "article_id": None,
            # ── 排序用私有字段（返回前剔除，不下发客户端）──
            "_interaction": (t.like_count or 0) + 2 * (t.reply_count or 0),
            "_rank_dt": t.last_reply_at or t.created_at,
            # ── 回填 liked 用（调用方剔除）──
            "_like_tid": t.id,
        })

    # ── 2. 文章 v1（沿用 article_list 无可见性过滤，全部可见） ──
    for a in ArticleModel.query.options(joinedload(ArticleModel.author)).all():
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
            # v1 文章无点赞通道，无 _like_tid
        })

    # ── 3. V2 文章（Markdown，article_v2 表；与旧文章同格式并入信息流） ──
    # 互动数取自上方预取的 v2_*_map（scope_type='article_v2' 的 thread）；无 thread 的文章显示 0
    for a in ArticleV2Model.query.options(joinedload(ArticleV2Model.author)).filter_by(
        status=ArticleV2Model.STATUS_PUBLISHED
    ).all():
        if content_type == 'discussion':
            continue
        rc = v2_reply_map.get(a.id, 0)
        lc = v2_like_map.get(a.id, 0)
        tid = v2_thread_id_map.get(a.id)
        vc = v2_view_map.get(a.id, 0)
        rank_dt = a.publish_time
        last_rep = v2_last_reply_map.get(a.id)
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
            "like_count": lc,
            "reply_count": rc,
            "view_count": vc,
            "liked": False,
            "is_pinned": False,
            "article_id": a.id,
            "article_version": 2,                   # 前端据此跳 /article-v2
            "_interaction": lc + 2 * rc,            # 与 v1 文章口径对齐：赞 + 2*评
            "_rank_dt": rank_dt or now,
            "_article_reply_count": rc,
            "_like_tid": tid,                       # 回填 v2 文章点赞（按其 thread id）；无 thread 时为 None
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
    # 下发前剔除 _ 前缀私有排序字段；保留 _like_tid 供调用方回填 liked 后再剔除
    page_items = [
        {k: v for k, v in it.items() if not k.startswith('_') or k == '_like_tid'}
        for it in page_items
    ]

    # ── 附带每条讨论帖前 2 条顶级回复预览（一次 IN 查询，消灭前端 N+1 #1） ──
    feed_thread_ids = [it['id'] for it in page_items if it['type'] == 'discussion']
    if feed_thread_ids:
        reply_rows = (
            DiscussionReply.query
            .filter(
                DiscussionReply.thread_id.in_(feed_thread_ids),
                DiscussionReply.parent_reply_id.is_(None),       # 与 list_replies 口径一致，只取顶级
                DiscussionReply.status == DiscussionReply.STATUS_NORMAL,
            )
            .order_by(DiscussionReply.thread_id, DiscussionReply.created_at.desc())
            .options(joinedload(DiscussionReply.author))
            .all()
        )
        # 已按 (thread_id, created_at desc) 排序：groupby 后每组取最新 2 条，再反转为正序展示
        preview_map = {}
        for tid, group in itertools.groupby(reply_rows, key=lambda r: r.thread_id):
            latest_two = list(group)[:2]
            latest_two.reverse()
            preview_map[tid] = latest_two
        for it in page_items:
            if it['type'] == 'discussion':
                it['replies'] = [_serialize_reply(r) for r in preview_map.get(it['id'], [])]

    return page_items, total, pages


# GET /community/feed
# 聚合「global 讨论帖」+「全部文章」为统一格式混合流，供首页/主社区广场展示。
# 讨论帖附带前 2 条回复预览，前端无需逐帖拉回复（消灭 N+1）。
# 公共分页按 (type,sort,page,per_page,view) 缓存 8s；用户特定的 liked 命中后回填。
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

    # ── 公共缓存（不含用户特定 liked）：按管理员/普通视角分桶，TTL 8s ──
    view = 'admin' if is_admin else 'public'
    cache_key = f"community:feed:{content_type}:{sort}:{page}:{per_page}:{view}"

    page_items = None
    total = pages = 0
    try:
        cached = redis_client.get(cache_key)
    except Exception:
        cached = None
    if cached:
        try:
            payload = json.loads(cached)
            page_items = payload['data']
            total = payload['total']
            pages = payload['pages']
        except (ValueError, KeyError, TypeError):
            page_items = None

    if page_items is None:
        page_items, total, pages = _build_feed_page(content_type, sort, page, per_page, is_admin, now)
        try:
            redis_client.setex(cache_key, 8, json.dumps({
                "code": 200,
                "data": page_items,
                "total": total,
                "page": page,
                "per_page": per_page,
                "pages": pages,
            }))
        except Exception:
            pass  # 缓存写失败不影响响应

    # ── 回填用户特定的 liked（thread + 回复预览），用集合 O(n) 判定 ──
    liked_thread_ids = {
        r.target_id for r in DiscussionReaction.query.filter_by(
            user_id=user.id, target_type='thread', reaction_type='like'
        ).all()
    }
    liked_reply_ids = {
        r.target_id for r in DiscussionReaction.query.filter_by(
            user_id=user.id, target_type='reply', reaction_type='like'
        ).all()
    }
    for it in page_items:
        tid = it.pop('_like_tid', None)
        it['liked'] = (tid in liked_thread_ids) if tid else False
        for r in it.get('replies', []):
            r['liked'] = r['id'] in liked_reply_ids

    return jsonify({
        "code": 200,
        "data": page_items,
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": pages,
    })

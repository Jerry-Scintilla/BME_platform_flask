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


from .media import public_avatar_url as get_avatar_url   # 新链路 /media/，旧值兜底 /data/avatars/


def _thread_images(t):
    """帖子图集 URL 数组（json 列解析；旧帖无图为空数组）。"""
    try:
        return json.loads(t.images_json) if t.images_json else []
    except (TypeError, ValueError):
        return []


# ==================== 热度排序 ====================

# 半衰期（天）：内容每过这么多天，热度衰减一半。
# 这是热度公式唯一的主调节旋钮——若好内容掉得太快，先调大这里（14→21→30）再动互动权重。
# 社区重设计（09-19）分层：帖子 14 天（快节奏对话），文章 30 天（长内容慢衰减，P1 公式落地）。
_HALF_LIFE_DAYS = 14
_ARTICLE_HALF_LIFE_DAYS = 30


def _hot_score(interaction, activity_dt, now, half_life_days=_HALF_LIFE_DAYS):
    """半衰期热度分：HOT = (互动分 + 1) * 0.5 ^ (age_days / 半衰期)。

    - +1 给新内容基础分，零互动也有分（=1）不会沉底；
    - 用真实 datetime 算 age，不要传格式化字符串；
    - activity_dt 为空时回退到 now（age=0，仅基础分）。
    注意：now 必须与模型 default=datetime.now() 同源（本地时间），勿用 utcnow()。
    """
    if activity_dt is None:
        activity_dt = now
    age_days = max((now - activity_dt).total_seconds() / 86400.0, 0.0)
    return (interaction + 1.0) * (0.5 ** (age_days / half_life_days))


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
    """构建 feed 的公共分页（Phase 3 09-20：SQL UNION 真分页，替代全量加载内存排序）。

    三类内容（global 帖 / v1 文章 / v2 文章）各出统一列的子查询 UNION ALL，
    热度公式 SQL 化（半衰期 POW 表达式），DB 层 ORDER BY + LIMIT/OFFSET——
    不再全量实体化 ORM 对象；页行返回后由调用方做 enrichment（徽章/回复预览/liked）。

    质量分层（P1 落地）：作者为 super_admin ×1.5 权重、精华 ×2、发布 48h 内 +3 曝光保护。
    帖子半衰期 14 天 / 文章 30 天；官方推文（is_official）不进 feed（精选带展示）。
    is_pinned 用生效态（pinned_until 到点自动失效）。
    """
    from sqlalchemy import text as _text

    # 统一热度表达式：HOT = (互动分+1+新内容保护) * 衰减 * 作者权重 * 精华倍率
    hot = ("(interaction + 1 + IF(rank_dt > NOW() - INTERVAL 48 HOUR, 3, 0)) "
           "* POW(0.5, TIMESTAMPDIFF(SECOND, rank_dt, NOW()) / 86400.0 / half_life) "
           "* author_weight * IF(is_essence, 2.0, 1.0)")

    subsqls = []

    # ── 1. global 讨论帖 ──
    category = request.args.get('category')
    from .discussion import THREAD_CATEGORIES
    category_on = category in THREAD_CATEGORIES     # 白名单：枚举外的值一律忽略（防注入）
    if content_type != 'article':
        cond = "t.status = 'normal'"
        if is_admin:
            cond = "t.status != 'deleted'"
        cat = f" AND t.category = '{category}'" if category_on else ""
        subsqls.append(f"""
            SELECT 'discussion' AS type, t.id, t.title, LEFT(t.content, 200) AS summary,
                   t.images_json AS images_raw, t.category, t.project_id,
                   sp.title AS project_title,
                   sp.summary AS project_summary,
                   CONCAT(SUBSTRING_INDEX(sp.cover, '.', 1), '_thumb.',
                          SUBSTRING_INDEX(sp.cover, '.', -1)) AS project_cover_thumb,
                   sp.project_status AS project_status,
                   sp.source AS project_source,
                   sp.tags AS project_tags_raw,
                   sp.view_count AS project_view_count,
                   t.author_id, u.username AS author_name, u.avatar_url AS author_avatar_raw,
                   COALESCE(t.last_reply_at, t.created_at) AS rank_dt,
                   t.created_at AS created_dt,
                   (t.like_count + 2 * t.reply_count) AS interaction,
                   t.reply_count, t.like_count AS like_count, t.view_count,
                   (t.is_pinned AND (t.pinned_until IS NULL OR t.pinned_until > NOW())) AS is_pinned,
                   t.is_essence, 14 AS half_life,
                   IF(u.role = 'super_admin', 1.5, 1.0) AS author_weight,
                   NULL AS article_id, NULL AS article_version,
                   NULL AS cover, NULL AS cover_thumb, t.id AS like_tid
            FROM discussion_thread t
            JOIN user u ON u.id = t.author_id
            LEFT JOIN showcase_project sp ON sp.id = t.project_id AND sp.status = 'visible'
            WHERE t.scope_type = 'global' AND {cond}{cat}
        """)

    # ── 2. v1 文章（无 status/点赞通道；互动=评论聚合） ──
    # 话题筛选开启时只看该话题的帖子（文章无话题概念，不混入）
    if content_type != 'discussion' and not category_on:
        subsqls.append("""
            SELECT 'article' AS type, a.id, a.title, LEFT(a.introduction, 200) AS summary,
                   NULL AS images_raw, NULL AS category, NULL AS project_id,
                   NULL AS project_title,
                   NULL AS project_summary, NULL AS project_cover_thumb,
                   NULL AS project_status, NULL AS project_source,
                   NULL AS project_tags_raw, NULL AS project_view_count,
                   a.author_id, u.username AS author_name, u.avatar_url AS author_avatar_raw,
                   COALESCE(agg.last_reply_at, a.publish_time) AS rank_dt,
                   a.publish_time AS created_dt,
                   2 * IFNULL(agg.rc, 0) AS interaction,
                   IFNULL(agg.rc, 0) AS reply_count, 0 AS like_count, 0 AS view_count,
                   0 AS is_pinned, 0 AS is_essence, 30 AS half_life,
                   IF(u.role = 'super_admin', 1.5, 1.0) AS author_weight,
                   a.id AS article_id, NULL AS article_version,
                   NULL AS cover, NULL AS cover_thumb, NULL AS like_tid
            FROM article a
            JOIN user u ON u.id = a.author_id
            LEFT JOIN (SELECT scope_id, SUM(reply_count) AS rc, MAX(last_reply_at) AS last_reply_at
                       FROM discussion_thread WHERE scope_type = 'article' AND status = 'normal'
                       GROUP BY scope_id) agg ON agg.scope_id = a.id
        """)
        # ── 3. v2 文章（排除官方推文；互动=赞+2评+0.3浏览） ──
        subsqls.append("""
            SELECT 'article' AS type, a.id, a.title, LEFT(a.introduction, 200) AS summary,
                   NULL AS images_raw, NULL AS category, NULL AS project_id,
                   NULL AS project_title,
                   NULL AS project_summary, NULL AS project_cover_thumb,
                   NULL AS project_status, NULL AS project_source,
                   NULL AS project_tags_raw, NULL AS project_view_count,
                   a.author_id, u.username AS author_name, u.avatar_url AS author_avatar_raw,
                   COALESCE(agg.last_reply_at, a.publish_time) AS rank_dt,
                   a.publish_time AS created_dt,
                   (IFNULL(agg.lc, 0) + 2 * IFNULL(agg.rc, 0) + 0.3 * IFNULL(agg.vc, 0)) AS interaction,
                   IFNULL(agg.rc, 0) AS reply_count, IFNULL(agg.lc, 0) AS like_count,
                   IFNULL(agg.vc, 0) AS view_count,
                   0 AS is_pinned, a.is_essence, 30 AS half_life,
                   IF(u.role = 'super_admin', 1.5, 1.0) AS author_weight,
                   a.id AS article_id, 2 AS article_version,
                   a.cover_image_key AS cover,
                   CONCAT(SUBSTRING_INDEX(a.cover_image_key, '.', 1), '_thumb.',
                          SUBSTRING_INDEX(a.cover_image_key, '.', -1)) AS cover_thumb,
                   agg.thread_id AS like_tid
            FROM article_v2 a
            JOIN user u ON u.id = a.author_id
            LEFT JOIN (SELECT scope_id, SUM(reply_count) AS rc, SUM(like_count) AS lc,
                              SUM(view_count) AS vc, MAX(last_reply_at) AS last_reply_at,
                              MAX(id) AS thread_id
                       FROM discussion_thread WHERE scope_type = 'article_v2' AND status = 'normal'
                       GROUP BY scope_id) agg ON agg.scope_id = a.id
            WHERE a.status = 'published' AND NOT a.is_official
        """)

    union = " UNION ALL ".join(f"({s})" for s in subsqls)
    offset = (page - 1) * per_page
    if sort == 'hot':
        order = (f" ORDER BY is_pinned DESC, {hot} DESC, reply_count DESC, rank_dt DESC")
    else:
        order = " ORDER BY is_pinned DESC, rank_dt DESC, reply_count DESC"

    total = db.session.execute(_text(f"SELECT COUNT(*) FROM ({union}) x")).scalar() or 0
    pages = (total + per_page - 1) // per_page if per_page > 0 else 0
    rows = db.session.execute(_text(
        f"SELECT * FROM ({union}) x{order} LIMIT {int(per_page)} OFFSET {int(offset)}"
    )).mappings().all()

    page_items = []
    from .discussion import CATEGORY_TEXT, _thread_images
    from .showcase import STATUS_TEXT as SHOWCASE_STATUS_TEXT, SOURCE_TEXT as SHOWCASE_SOURCE_TEXT
    for r in rows:
        item = {
            "type": r["type"], "id": r["id"], "title": r["title"],
            "summary": r["summary"] or '',
            "images": _thread_images_from_raw(r["images_raw"]),
            "category": r["category"],
            "category_text": CATEGORY_TEXT.get(r["category"]) if r["category"] else None,
            "project_id": r["project_id"], "project_title": r["project_title"],
            "author_id": r["author_id"], "author_name": r["author_name"] or "",
            "author_avatar": get_avatar_url(r["author_avatar_raw"]),
            "created_at": r["created_dt"].strftime('%Y-%m-%d %H:%M:%S') if r["created_dt"] else "",
            "like_count": r["like_count"] or 0, "reply_count": r["reply_count"] or 0,
            "view_count": r["view_count"] or 0, "liked": False,
            "is_pinned": bool(r["is_pinned"]),
            "is_essence": bool(r["is_essence"]),
            "article_id": r["article_id"],
            "_like_tid": r["like_tid"],
        }
        # 关联项目摘要投影（XLab 引流优化 §7.2）：LEFT JOIN 只带 visible 项目，
        # 下架/无关联帖子的 project_title 为 NULL，这里不落字段——前端据此不渲染项目卡
        if r["type"] == 'discussion' and r["project_id"] and r["project_title"]:
            item["project_summary"] = r["project_summary"]
            item["project_cover_thumb"] = r["project_cover_thumb"]
            item["project_status"] = r["project_status"]
            item["project_status_text"] = SHOWCASE_STATUS_TEXT.get(
                r["project_status"], r["project_status"])
            item["project_source"] = r["project_source"]
            item["project_source_text"] = SHOWCASE_SOURCE_TEXT.get(r["project_source"], r["project_source"])
            try:
                item["project_tags"] = json.loads(r["project_tags_raw"]) if r["project_tags_raw"] else []
            except (TypeError, ValueError):
                item["project_tags"] = []
            item["project_view_count"] = r["project_view_count"] or 0
        if r["type"] == 'article':
            item["article_version"] = r["article_version"]
            item["cover"] = r["cover"]
            item["cover_thumb"] = r["cover_thumb"]
        page_items.append(item)

    # ── 社团徽章：按页收集作者一次 IN 查询 ──
    from .officers import badge_map as officer_badge_map
    page_author_ids = list({it.get('author_id') for it in page_items if it.get('author_id')})
    if page_author_ids:
        badges = officer_badge_map(page_author_ids)
        for it in page_items:
            badge = badges.get(it.get('author_id'))
            if badge:
                it['author_badge'] = badge['text']
                it['author_badge_tier'] = badge['tier']

    # ── 附带每条讨论帖前 2 条顶级回复预览（一次 IN 查询，消灭前端 N+1 #1） ──
    feed_thread_ids = [it['id'] for it in page_items if it['type'] == 'discussion']
    if feed_thread_ids:
        reply_rows = (
            DiscussionReply.query
            .filter(
                DiscussionReply.thread_id.in_(feed_thread_ids),
                DiscussionReply.parent_reply_id.is_(None),
                DiscussionReply.status == DiscussionReply.STATUS_NORMAL,
            )
            .order_by(DiscussionReply.thread_id, DiscussionReply.created_at.desc())
            .options(joinedload(DiscussionReply.author))
            .all()
        )
        preview_map = {}
        for tid, group in itertools.groupby(reply_rows, key=lambda r: r.thread_id):
            latest_two = list(group)[:2]
            latest_two.reverse()
            preview_map[tid] = latest_two
        for it in page_items:
            if it['type'] == 'discussion':
                it['replies'] = [_serialize_reply(r) for r in preview_map.get(it['id'], [])]

    return page_items, total, pages


def _thread_images_from_raw(raw):
    """feed SQL 行的 images_json 原始串 -> URL 数组（与 discussion._thread_images 同口径）。"""
    try:
        return json.loads(raw) if raw else []
    except (TypeError, ValueError):
        return []


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
    is_admin = user.is_admin()

    # ── 公共缓存（不含用户特定 liked）：按管理员/普通视角分桶，TTL 8s ──
    view = 'admin' if is_admin else 'public'
    cache_key = f"community:feed:{content_type}:{sort}:{page}:{per_page}:{view}:{request.args.get('category') or ''}"

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


# GET /community/spotlight
# 推文精选带（社区重设计 09-19）：最新 N 篇官方推文（is_official 且已发布），
# 带 cover/introduction/作者；redis 60s 公共缓存（运营内容变化低频）。
# 官方推文不进 /feed 正文流——精选带是它们的唯一展示位。
@bp.route("/spotlight", methods=["GET"])
@jwt_required()
def community_spotlight():
    limit = request.args.get('limit', 3, type=int)
    limit = max(1, min(limit, 6))
    cache_key = f"community:spotlight:{limit}"
    cached = None
    try:
        cached = redis_client.get(cache_key)
    except Exception:
        cached = None
    if cached:
        try:
            return jsonify(json.loads(cached))
        except (ValueError, TypeError):
            pass

    rows = ArticleV2Model.query.options(joinedload(ArticleV2Model.author)).filter(
        ArticleV2Model.status == ArticleV2Model.STATUS_PUBLISHED,
        ArticleV2Model.is_official.is_(True),
    ).order_by(ArticleV2Model.publish_time.desc()).limit(limit).all()
    data = []
    for a in rows:
        stem, ext = (a.cover_image_key.rsplit('.', 1) if a.cover_image_key else (None, None))
        data.append({
            "id": a.id,
            "title": a.title or '',
            "summary": (a.introduction or '')[:120],
            "cover": a.cover_image_key,
            "cover_thumb": f"{stem}_thumb.{ext}" if stem else None,
            "author_id": a.author_id,
            "author_name": a.author.username if a.author else '',
            "publish_time": a.publish_time.strftime('%Y-%m-%d %H:%M:%S') if a.publish_time else '',
        })
    payload = {"code": 200, "data": data}
    try:
        redis_client.setex(cache_key, 60, json.dumps(payload))
    except Exception:
        pass
    return jsonify(payload)

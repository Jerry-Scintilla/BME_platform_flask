import json
import itertools

from flask import Blueprint, request, jsonify
from flask_cors import cross_origin
from datetime import datetime

from exts import db, redis_client
from sqlalchemy.orm import joinedload
from models import UserModel, DiscussionThread, DiscussionReply, DiscussionReaction, ArticleModel, ArticleV2Model
from flask_jwt_extended import get_jwt_identity, jwt_required
from services import community_reading as reading
from models import CommunityTag, CommunityBook

bp = Blueprint("community", __name__, url_prefix="/community")


@bp.errorhandler(reading.CommunityError)
def reading_error(error):
    db.session.rollback()
    return jsonify(code=error.status, message=error.message, **error.extra), error.status


@bp.route('/tags', methods=['GET', 'POST'])
@jwt_required()
def community_tags():
    user = get_current_user()
    if not user:
        return jsonify(code=401, message='用户不存在'), 401
    if request.method == 'GET':
        return jsonify(code=200, data=reading.list_tags(user.is_admin() and request.args.get('all') == 'true'))
    if not user.is_admin():
        return jsonify(code=403, message='只有管理员可以维护标签'), 403
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise reading.CommunityError('请求参数错误')
    tag = reading.save_tag(data, user)
    return jsonify(code=201, data=reading.tag_data(tag)), 201


@bp.route('/tags/<int:tag_id>', methods=['GET', 'PUT', 'DELETE'])
@jwt_required()
def update_community_tag(tag_id):
    user = get_current_user()
    if not user:
        return jsonify(code=401, message='用户不存在'), 401
    tag = db.session.get(CommunityTag, tag_id)
    if not tag:
        raise reading.CommunityError('标签不存在', 404)
    if request.method == 'GET':
        return jsonify(code=200, data=reading.tag_data(tag))
    if not user.is_admin():
        return jsonify(code=403, message='只有管理员可以维护标签'), 403
    if request.method == 'DELETE':
        reading.delete_tag(tag)
        return jsonify(code=200, message='标签已删除')
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise reading.CommunityError('请求参数错误')
    return jsonify(code=200, data=reading.tag_data(reading.save_tag(data, user, tag)))


@bp.route('/tags/<int:tag_id>/books', methods=['GET', 'POST'])
@jwt_required()
def community_books(tag_id):
    user = get_current_user()
    if not user:
        return jsonify(code=401, message='用户不存在'), 401
    tag = db.session.get(CommunityTag, tag_id)
    if not tag or not tag.is_reading:
        raise reading.CommunityError('阅读活动不存在', 404)
    if request.method == 'POST':
        if not user.is_admin():
            return jsonify(code=403, message='只有管理员可以导入书单'), 403
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            raise reading.CommunityError('书单格式错误')
        count = reading.import_books(tag, body.get('books'))
        return jsonify(code=200, created=count)
    books = CommunityBook.query.filter_by(tag_id=tag_id).order_by(CommunityBook.id).all()
    return jsonify(code=200, data=[reading.book_data(b) for b in books])


@bp.route('/discovery', methods=['GET'])
@jwt_required()
def community_discovery():
    user = get_current_user()
    if not user:
        return jsonify(code=401, message='用户不存在'), 401
    from services.community_discovery import discovery
    return jsonify(discovery(request.args, user))


@bp.route('/books/<int:book_id>/threads', methods=['GET'])
@jwt_required()
def community_book_threads(book_id):
    user = get_current_user()
    if not user:
        return jsonify(code=401, message='用户不存在'), 401
    book = db.session.get(CommunityBook, book_id)
    if not book:
        raise reading.CommunityError('书籍不存在', 404)
    rows = reading.topic_query(book.tag_id, active_only=True).filter(DiscussionThread.book_id == book_id).order_by(DiscussionThread.created_at).limit(5).all()
    return jsonify(code=200, data=[dict(id=t.id, title=t.title) for t in rows])


@bp.route('/recommendations', methods=['GET'])
@jwt_required()
def community_recommendations():
    user = get_current_user()
    if not user:
        return jsonify(code=401, message='用户不存在'), 401
    return jsonify(reading.topic_page(request.args, user, recommendations=True))

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

    from services.community_feed import build_page
    return jsonify(build_page(request.args, user))


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

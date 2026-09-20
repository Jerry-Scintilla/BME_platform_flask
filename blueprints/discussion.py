from flask import Blueprint, request, jsonify
from flask_cors import cross_origin
from sqlalchemy import and_, or_
from datetime import datetime, timedelta
import io
import json
import os
import uuid

from exts import db, redis_client
from models import UserModel, DiscussionThread, DiscussionReply, DiscussionReaction, CourseGroup, CourseGroupMember
from flask_jwt_extended import get_jwt_identity, jwt_required

from storage import storage
import imaging

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


from .media import public_avatar_url as get_avatar_url   # 新链路 /media/，旧值兜底 /data/avatars/
from .media import media_url as _media_url

# 话题标签（Phase 2 09-20）：global 帖轻量分类——招人类内容引导关联 XLAB 项目
THREAD_CATEGORIES = ('chat', 'ask', 'share', 'recruit')
CATEGORY_TEXT = {'chat': '闲聊', 'ask': '提问', 'share': '分享', 'recruit': '招人'}


def _thread_images(thread):
    """帖子图集 URL 数组（json 列解析；旧帖无图为空数组）。"""
    try:
        return json.loads(thread.images_json) if thread.images_json else []
    except (TypeError, ValueError):
        return []


def _validate_category_project(data):
    """create/update 共用：校验 category（枚举内/None）与 project_id（存在且上架）。
    返回 (category, project_id, 错误响应)。字段缺省时返回 None 哨兵表示"不改"。"""
    from models import ShowcaseProject
    category = data.get('category') if 'category' in data else False
    if category is False:
        cat_out = False
    elif category is None or category == '':
        cat_out = None
    elif category not in THREAD_CATEGORIES:
        return None, None, (jsonify({"code": 400, "message": f"category 须为 {'/'.join(THREAD_CATEGORIES)} 或空"}), 400)
    else:
        cat_out = category
    project_id = data.get('project_id') if 'project_id' in data else False
    if project_id is False:
        pid_out = False
    elif project_id in (None, ''):
        pid_out = None
    else:
        if not isinstance(project_id, int):
            return None, None, (jsonify({"code": 400, "message": "project_id 须为整数或空"}), 400)
        if not ShowcaseProject.query.filter_by(id=project_id, status='visible').first():
            return None, None, (jsonify({"code": 404, "message": "关联的项目不存在或已下架"}), 404)
        pid_out = project_id
    return cat_out, pid_out, None


def _pin_active(thread, now=None):
    """置顶是否生效（pinned_until 到点自动失效）。"""
    if not thread.is_pinned:
        return False
    until = thread.pinned_until
    if until is None:
        return True
    return until > (now or datetime.now())


def _thread_extra(t, project_titles=None):
    """话题/关联项目序列化块。project_titles 为 {id: title} 预查映射（列表批量场景免 N+1）。"""
    title = None
    if t.project_id:
        if project_titles is not None:
            title = project_titles.get(t.project_id)
        else:
            from models import ShowcaseProject
            p = ShowcaseProject.query.get(t.project_id)
            title = p.title if p and p.status == 'visible' else None
    return {
        "category": t.category,
        "category_text": CATEGORY_TEXT.get(t.category) if t.category else None,
        "project_id": t.project_id,
        "project_title": title,
    }


def _project_title_map(threads):
    """批量取 {project_id: title}（仅上架项目；一次 IN 查询）。"""
    from models import ShowcaseProject
    pids = list({t.project_id for t in threads if t.project_id})
    if not pids:
        return {}
    rows = ShowcaseProject.query.filter(
        ShowcaseProject.id.in_(pids), ShowcaseProject.status == 'visible'
    ).with_entities(ShowcaseProject.id, ShowcaseProject.title).all()
    return dict(rows)


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
    if user.is_admin():
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

    # article_v2: V2 文章（Markdown）评论区，与 v1 同口径
    if thread.scope_type == 'article_v2':
        from models import ArticleV2Model
        article = ArticleV2Model.query.get(thread.scope_id)
        if not article:
            return False
        return True

    return False


def can_post_thread(scope_type, scope_id, user):
    """检查用户是否可以发帖"""
    if not user:
        return False

    if user.is_admin():
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

    # article / article_v2: 文章评论区，登录用户均可参与
    if scope_type in ('article', 'article_v2'):
        return True

    if scope_type == 'course':
        from models import UserCourseModel
        enrollment = UserCourseModel.query.filter_by(
            user_id=user.id,
            course_id=scope_id
        ).first()
        return enrollment is not None

    # project: 项目广场条目评论区——展示板块，登录用户均可参与（条目可见性由广场侧控制）
    if scope_type == 'project':
        return True

    return False


def can_moderate_thread(thread, user):
    """检查用户是否可以管理主题帖（置顶/锁帖/隐藏等治理动作）。

    super_admin 直通；Phase 2（09-20）激活 discussion_management 权限点——持有者可治理
    （原空转种子权限，社区治理页上线启用）。作者不再能管理自己的帖子（自助置顶越权
    已修）；作者的删帖/编辑权限走各自端点的独立内联检查，不受此函数影响。
    """
    if not user:
        return False
    if user.is_admin():
        return True
    from models import PermissionModel, UserPermissionModel
    perm = PermissionModel.query.filter_by(name='discussion_management').first()
    if not perm:
        return False
    return UserPermissionModel.query.filter_by(
        user_id=user.id, permission_id=perm.id
    ).first() is not None


# ==================== 主题帖 CRUD ====================

# 获取或创建某篇文章的评论汇总 thread（scope=article），用于文章评论区
@bp.route("/article/<int:article_id>/thread", methods=["GET"])
@jwt_required()
def get_or_create_article_thread(article_id):
    """文章评论区：获取/创建该文章的汇总 thread，评论挂在其 replies 下"""
    user = get_current_user()
    if not user:
        return jsonify({"code": 401, "message": "用户不存在"}), 401

    from models import ArticleModel
    article = ArticleModel.query.get(article_id)
    if not article:
        return jsonify({"code": 404, "message": "文章不存在"}), 404

    thread = DiscussionThread.query.filter_by(
        scope_type='article',
        scope_id=article_id,
        status=DiscussionThread.STATUS_NORMAL
    ).order_by(DiscussionThread.created_at.asc()).first()

    if not thread:
        thread = DiscussionThread(
            title=f"文章评论 · {article.title}",
            content="该文章的评论区（系统自动创建）",
            scope_type='article',
            scope_id=article_id,
            author_id=user.id
        )
        db.session.add(thread)
        db.session.commit()

    return jsonify({
        "code": 200,
        "data": {
            "thread_id": thread.id,
            "article_id": article_id,
            "reply_count": thread.reply_count
        }
    }), 200


# 获取或创建某篇 V2 文章的评论汇总 thread（scope=article_v2）
@bp.route("/article_v2/<int:article_id>/thread", methods=["GET"])
@jwt_required()
def get_or_create_article_v2_thread(article_id):
    """V2 文章评论区：获取/创建该文章的汇总 thread，评论挂在其 replies 下"""
    user = get_current_user()
    if not user:
        return jsonify({"code": 401, "message": "用户不存在"}), 401

    from models import ArticleV2Model
    article = ArticleV2Model.query.get(article_id)
    if not article:
        return jsonify({"code": 404, "message": "文章不存在"}), 404

    thread = DiscussionThread.query.filter_by(
        scope_type='article_v2',
        scope_id=article_id,
        status=DiscussionThread.STATUS_NORMAL
    ).order_by(DiscussionThread.created_at.asc()).first()

    if not thread:
        thread = DiscussionThread(
            title=f"文章评论 · {article.title}",
            content="该文章的评论区（系统自动创建）",
            scope_type='article_v2',
            scope_id=article_id,
            author_id=user.id
        )
        db.session.add(thread)
        db.session.commit()

    return jsonify({
        "code": 200,
        "data": {
            "thread_id": thread.id,
            "article_id": article_id,
            "reply_count": thread.reply_count,
            "like_count": thread.like_count
        }
    }), 200


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

    # 校验 scope_type（提前：title 校验按 scope 分支，project=留言式无标题）
    valid_scopes = ['global', 'article', 'article_v2', 'course', 'group', 'task', 'project']  # project=项目广场（功能扩展轮 §五）
    if scope_type not in valid_scopes:
        return jsonify({"code": 400, "message": f"scope_type 必须为: {', '.join(valid_scopes)}"}), 400

    if not content:
        return jsonify({"code": 400, "message": "内容不能为空"}), 400

    if scope_type == 'project':
        # 项目广场讨论区=留言式：无标题，缺省自动用正文前 20 字占位（DB title 不可空）
        title = (title or '').strip() or (content or '').strip()[:20]
    else:
        if not title:
            return jsonify({"code": 400, "message": "标题和内容不能为空"}), 400
        # 长度校验（strip 后计字符），挡 1 字灌水
        if len((title or '').strip()) < 4:
            return jsonify({"code": 400, "message": "标题至少 4 个字"}), 400

    if len((content or '').strip()) < 10:
        return jsonify({"code": 400, "message": "正文至少 10 个字"}), 400

    # 非 global 类型需要 scope_id
    if scope_type != 'global' and not scope_id:
        return jsonify({"code": 400, "message": "非全局讨论需要指定 scope_id"}), 400

    # 权限检查
    if not can_post_thread(scope_type, scope_id, user):
        return jsonify({"code": 403, "message": "无权限在此范围发帖"}), 403

    # 频率限制：5 分钟 ≤ 3 帖、小时 ≤ 10 帖（redis 手动计数；limiter 按 IP 限流拿不到 jwt 用户）
    # 放在所有校验通过后、写库前，避免无效请求消耗配额
    uid = str(user.id)
    key_5m = f"post_rate:{uid}:5m"
    key_1h = f"post_rate:{uid}:1h"
    try:
        n5 = redis_client.incr(key_5m)
        if n5 == 1:
            redis_client.expire(key_5m, 300)
        n1h = redis_client.incr(key_1h)
        if n1h == 1:
            redis_client.expire(key_1h, 3600)
    except Exception:
        n5 = n1h = 0  # redis 不可用时降级为不限流
    if n5 > 3:
        return jsonify({"code": 429, "message": "发帖太频繁，请 5 分钟后再试"}), 429
    if n1h > 10:
        return jsonify({"code": 429, "message": "发帖太频繁，请稍后再试"}), 429

    # 帖子图集（社区重设计 09-19）：URL 数组 ≤4，只收 /media/discussions/ 上传回包（防外链）
    images = data.get('images')
    if images is not None:
        if (not isinstance(images, list) or len(images) > 4
                or any(not isinstance(u, str) or not u.startswith('/media/discussions/') for u in images)):
            return jsonify({"code": 400, "message": "images 须为 ≤4 个 /media/discussions/ 上传返回的 URL"}), 400

    # 话题标签 + 关联 XLAB 项目（Phase 2 09-20；仅 global 帖有意义，其他 scope 忽略）
    category, project_id, cp_err = _validate_category_project(data if scope_type == 'global' else {})
    if cp_err:
        return cp_err

    thread = DiscussionThread(
        title=title,
        content=content,
        scope_type=scope_type,
        scope_id=scope_id,
        author_id=user.id,
        images_json=json.dumps(images, ensure_ascii=False) if images else None,
        category=category if scope_type == 'global' else None,
        project_id=project_id if scope_type == 'global' else None,
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
            "images": _thread_images(thread),
            **_thread_extra(thread),
            "is_essence": bool(thread.is_essence),
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
    if status == 'all':
        status = None    # 治理视角：列全部状态（Phase 2 09-20）
    category = request.args.get('category')    # 话题筛选（global 帖，Phase 2）
    author_id = request.args.get('author_id', type=int)   # 按作者筛（本人或 admin；我的帖子 09-20）
    if author_id and author_id != user.id and not user.is_admin():
        return jsonify({"code": 403, "message": "只能查看自己的帖子"}), 403
    sort = request.args.get('sort', 'latest')  # latest/pinned

    query = DiscussionThread.query

    # 权限过滤：只返回用户有权限查看的帖子。
    # 仅作用于无显式 scope 的 feed 视图——显式按 scope 查询时可见性由 scope 自身决定
    # （article/course 各自端点已校验；project=广场公开评论区）。group 显式查询保留成员门。
    if not user.is_admin() and (not scope_type or scope_type == 'group'):
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
    if category:
        query = query.filter(DiscussionThread.category == category)
    if author_id:
        query = query.filter(DiscussionThread.author_id == author_id)

    # 排序
    if sort == 'pinned':
        query = query.order_by(DiscussionThread.is_pinned.desc(), DiscussionThread.last_reply_at.desc())
    else:
        query = query.order_by(DiscussionThread.is_pinned.desc(), DiscussionThread.created_at.desc())

    # 分页
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)

    result = []
    ptitle_map = _project_title_map(pagination.items)
    for thread in pagination.items:
        # 检查用户是否有权限查看（用于隐藏状态帖子）
        if thread.status in [DiscussionThread.STATUS_HIDDEN, DiscussionThread.STATUS_DELETED]:
            if thread.author_id != user.id and not user.is_admin():
                continue

        item = {
            "id": thread.id,
            "title": thread.title,
            "content": thread.content,
            "images": _thread_images(thread),
            **_thread_extra(thread, ptitle_map),
            "pinned_effective": _pin_active(thread),
            "is_essence": bool(thread.is_essence),
            "scope_type": thread.scope_type,
            "scope_id": thread.scope_id,
            "author_id": thread.author_id,
            "author_name": thread.author.username if thread.author else "",
            "author_avatar": get_avatar_url(thread.author.avatar_url) if thread.author else "",
            "status": thread.status,
            "is_pinned": thread.is_pinned,
            "reply_count": thread.reply_count,
            "like_count": thread.like_count,
            "view_count": thread.view_count,
            "last_reply_at": thread.last_reply_at.strftime('%Y-%m-%d %H:%M:%S') if thread.last_reply_at else None,
            "created_at": thread.created_at.strftime('%Y-%m-%d %H:%M:%S')
        }
        result.append(item)

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

    # 增加浏览计数（按用户去重：同一用户对同一帖子只计一次，避免刷新社区广场反复 +1）
    # 复用 DiscussionReaction(target_type=thread, reaction_type=view) 作为浏览印记，
    # 其 (user_id, target_type, target_id, reaction_type) 唯一约束天然保证幂等。
    viewed = DiscussionReaction.query.filter_by(
        user_id=user.id, target_type='thread', target_id=thread_id, reaction_type='view'
    ).first()
    if not viewed:
        thread.view_count += 1
        db.session.add(DiscussionReaction(
            user_id=user.id, target_type='thread', target_id=thread_id, reaction_type='view'
        ))
        db.session.commit()

    return jsonify({
        "code": 200,
        "data": {
            "id": thread.id,
            "title": thread.title,
            "content": thread.content,
            "images": _thread_images(thread),
            **_thread_extra(thread),
            "pinned_effective": _pin_active(thread),
            "is_essence": bool(thread.is_essence),
            "scope_type": thread.scope_type,
            "scope_id": thread.scope_id,
            "author_id": thread.author_id,
            "author_name": thread.author.username if thread.author else "",
            "author_avatar": get_avatar_url(thread.author.avatar_url) if thread.author else "",
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


# 批量记录浏览 POST /discussions/threads/view_batch
# 社区广场拉取一页后一次性上报当前页讨论帖 id，按用户幂等去重 +1 view_count。
# 替代旧的"每卡拉详情接口顺带 +1"（N+1），改为每页 1 个轻量上报。
@bp.route("/threads/view_batch", methods=["POST"])
@jwt_required()
def view_batch_threads():
    """批量记录浏览（按用户幂等去重）。"""
    user = get_current_user()
    if not user:
        return jsonify({"code": 401, "message": "用户不存在"}), 401

    data = request.get_json(silent=True) or {}
    raw = data.get('thread_ids') or []
    try:
        tids = list({int(t) for t in raw})[:100]   # 去重 + 上限保护
    except (TypeError, ValueError):
        return jsonify({"code": 400, "message": "thread_ids 格式错误"}), 400
    if not tids:
        return jsonify({"code": 200, "viewed": []}), 200

    # 复用 get_thread 的幂等印记：(user, thread, view) reaction 存在即已计过
    already = {
        r.target_id for r in DiscussionReaction.query.filter(
            DiscussionReaction.user_id == user.id,
            DiscussionReaction.target_type == 'thread',
            DiscussionReaction.reaction_type == 'view',
            DiscussionReaction.target_id.in_(tids),
        ).all()
    }
    to_view = [t for t in tids if t not in already]
    viewed = []
    if to_view:
        threads = {t.id: t for t in DiscussionThread.query.filter(DiscussionThread.id.in_(to_view)).all()}
        for tid in to_view:
            t = threads.get(tid)
            if not t:
                continue
            t.view_count = (t.view_count or 0) + 1
            db.session.add(DiscussionReaction(
                user_id=user.id, target_type='thread', target_id=tid, reaction_type='view'
            ))
            viewed.append(tid)
        db.session.commit()
    return jsonify({"code": 200, "viewed": viewed}), 200


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
    if thread.author_id != user.id and not user.is_admin():
        return jsonify({"code": 403, "message": "无权限编辑"}), 403

    data = request.get_json(silent=True) or {}

    if 'title' in data:
        thread.title = data['title']
    if 'content' in data:
        thread.content = data['content']
    # 图集整体替换（同 create 的校验口径）
    if 'images' in data:
        images = data['images']
        if images is not None and (not isinstance(images, list) or len(images) > 4
                                    or any(not isinstance(u, str) or not u.startswith('/media/discussions/') for u in images)):
            return jsonify({"code": 400, "message": "images 须为 ≤4 个 /media/discussions/ 上传返回的 URL"}), 400
        thread.images_json = json.dumps(images, ensure_ascii=False) if images else None
    # 话题/关联项目（Phase 2；仅 global 帖）
    if thread.scope_type == 'global':
        category, project_id, cp_err = _validate_category_project(data)
        if cp_err:
            return cp_err
        if category is not False:
            thread.category = category
        if project_id is not False:
            thread.project_id = project_id

    db.session.commit()

    return jsonify({
        "code": 200,
        "message": "updated",
        "data": {
            "id": thread.id,
            "title": thread.title,
            "content": thread.content,
            "images": _thread_images(thread),
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
    if thread.author_id != user.id and not user.is_admin():
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
    """置顶/取消置顶主题帖。body 可选 expires_days（≤30）：置顶 N 天后自动失效；取消置顶清空。"""
    user = get_current_user()
    if not user:
        return jsonify({"code": 401, "message": "用户不存在"}), 401

    thread = DiscussionThread.query.get(thread_id)
    if not thread:
        return jsonify({"code": 404, "message": "帖子不存在"}), 404

    # 权限检查
    if not can_moderate_thread(thread, user):
        return jsonify({"code": 403, "message": "无权限操作"}), 403

    if thread.is_pinned and _pin_active(thread):
        # 已置顶 → 取消
        thread.is_pinned = False
        thread.pinned_until = None
    else:
        days = (request.get_json(silent=True) or {}).get('expires_days')
        thread.is_pinned = True
        if isinstance(days, int) and 1 <= days <= 30:
            thread.pinned_until = datetime.now() + timedelta(days=days)
        else:
            thread.pinned_until = None
    db.session.commit()

    return jsonify({
        "code": 200,
        "message": "pinned" if thread.is_pinned else "unpinned",
        "data": {
            "is_pinned": thread.is_pinned,
            "pinned_until": thread.pinned_until.strftime('%Y-%m-%d %H:%M:%S') if thread.pinned_until else None,
        }
    })


# 隐藏/恢复 POST /discussions/threads/{thread_id}/hide（治理：hidden<->normal）
@bp.route("/threads/<int:thread_id>/hide", methods=["POST"])
@jwt_required()
def hide_thread(thread_id):
    """隐藏/恢复主题帖（hidden<->normal 切换）。隐藏帖仅作者/admin 在详情可见，不进公共 feed。"""
    user = get_current_user()
    if not user:
        return jsonify({"code": 401, "message": "用户不存在"}), 401

    thread = DiscussionThread.query.get(thread_id)
    if not thread:
        return jsonify({"code": 404, "message": "帖子不存在"}), 404

    if not can_moderate_thread(thread, user):
        return jsonify({"code": 403, "message": "无权限操作"}), 403

    if thread.status == DiscussionThread.STATUS_HIDDEN:
        thread.status = DiscussionThread.STATUS_NORMAL
    else:
        thread.status = DiscussionThread.STATUS_HIDDEN
    db.session.commit()
    return jsonify({
        "code": 200,
        "message": "hidden" if thread.status == DiscussionThread.STATUS_HIDDEN else "restored",
        "data": {"status": thread.status}
    })


# 精华标记 POST /discussions/threads/{thread_id}/essence（治理：切换，热度 ×2，Phase 3 质量分层）
@bp.route("/threads/<int:thread_id>/essence", methods=["POST"])
@jwt_required()
def essence_thread(thread_id):
    """精华帖标记切换（can_moderate_thread 门禁；feed 热度 ×2）。"""
    user = get_current_user()
    if not user:
        return jsonify({"code": 401, "message": "用户不存在"}), 401
    thread = DiscussionThread.query.get(thread_id)
    if not thread:
        return jsonify({"code": 404, "message": "帖子不存在"}), 404
    if not can_moderate_thread(thread, user):
        return jsonify({"code": 403, "message": "无权限操作"}), 403
    thread.is_essence = not thread.is_essence
    db.session.commit()
    return jsonify({
        "code": 200,
        "message": "essence" if thread.is_essence else "un-essence",
        "data": {"is_essence": thread.is_essence},
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

    if len((content or '').strip()) < 2:
        return jsonify({"code": 400, "message": "回复内容至少 2 个字"}), 400

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

    # 互动通知（Phase 3 09-20）：回复产生 community 通知——
    # 普通帖通知楼主；文章评论（scope=article/article_v2）通知文章作者；
    # 楼中楼额外通知父回复作者。自己回自己/通知对象=操作人时跳过。
    try:
        from .notification import create_notification
        from models import ArticleModel, ArticleV2Model
        targets = set()
        if thread.scope_type in ('article', 'article_v2'):
            model = ArticleV2Model if thread.scope_type == 'article_v2' else ArticleModel
            art = model.query.get(thread.scope_id)
            if art:
                targets.add(art.author_id)
        else:
            targets.add(thread.author_id)
        if parent_reply_id:
            pr = DiscussionReply.query.get(parent_reply_id)
            if pr:
                targets.add(pr.author_id)
        targets.discard(user.id)
        for uid in targets:
            create_notification(
                uid,
                title='你的内容有新回复',
                content=f"{user.username or '有人'} 回复了你：{content.strip()[:80]}",
                category='community',
                source_type='discussion_reply',
                source_id=reply.id,
            )
        db.session.commit()
    except Exception:
        db.session.rollback()   # 通知失败不影响回复本身

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

    # 预取每个顶级回复的正常子回复（避免下面逐条重复查询，也便于批量取点赞状态）
    top_replies = []
    for reply in pagination.items:
        children = reply.children.filter_by(status=DiscussionReply.STATUS_NORMAL).all()
        top_replies.append((reply, children))

    # 批量查当前用户对本页回复（含楼中楼）的点赞状态，组装时回填 liked 字段
    all_reply_ids = [r.id for r, _ in top_replies]
    for _, children in top_replies:
        all_reply_ids.extend(c.id for c in children)

    liked_ids = set()
    if all_reply_ids:
        liked_rows = DiscussionReaction.query.filter(
            DiscussionReaction.user_id == user.id,
            DiscussionReaction.target_type == 'reply',
            DiscussionReaction.target_id.in_(all_reply_ids),
            DiscussionReaction.reaction_type == 'like'
        ).all()
        liked_ids = {row.target_id for row in liked_rows}

    result = []
    for reply, children in top_replies:
        children_data = [{
            "id": c.id,
            "author_id": c.author_id,
            "author_name": c.author.username if c.author else "",
            "author_avatar": get_avatar_url(c.author.avatar_url) if c.author else "",
            "content": c.content,
            "like_count": c.like_count,
            "liked": c.id in liked_ids,
            "created_at": c.created_at.strftime('%Y-%m-%d %H:%M:%S')
        } for c in children]

        result.append({
            "id": reply.id,
            "author_id": reply.author_id,
            "author_name": reply.author.username if reply.author else "",
            "author_avatar": get_avatar_url(reply.author.avatar_url) if reply.author else "",
            "content": reply.content,
            "like_count": reply.like_count,
            "liked": reply.id in liked_ids,
            "created_at": reply.created_at.strftime('%Y-%m-%d %H:%M:%S'),
            "children": children_data
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
    if reply.author_id != user.id and not user.is_admin():
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
    if reply.author_id != user.id and not user.is_admin():
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
        # 取消 reaction
        db.session.delete(existing)
        # 仅点赞计入 like_count（收藏等不计）
        if reaction_type == 'like':
            target.like_count = max(0, target.like_count - 1)
        db.session.commit()
        liked = False
    else:
        # 新增 reaction
        reaction = DiscussionReaction(
            user_id=user.id,
            target_type=target_type,
            target_id=target_id,
            reaction_type=reaction_type
        )
        db.session.add(reaction)
        if reaction_type == 'like':
            target.like_count += 1
        db.session.commit()
        liked = True

        # 互动通知（Phase 3）：点赞产生 community 通知（thread→楼主；reply→回复作者；
        # 文章评论 thread 点赞→文章作者）。取消赞不撤回通知；自己赞自己跳过。
        if reaction_type == 'like':
            try:
                from .notification import create_notification
                from models import ArticleModel, ArticleV2Model
                if target_type == 'thread' and target.scope_type in ('article', 'article_v2'):
                    model = ArticleV2Model if target.scope_type == 'article_v2' else ArticleModel
                    art = model.query.get(target.scope_id)
                    notify_uid = art.author_id if art else target.author_id
                else:
                    notify_uid = target.author_id
                if notify_uid and notify_uid != user.id:
                    create_notification(
                        notify_uid,
                        title='你的内容获赞',
                        content=f"{user.username or '有人'} 赞了你的{'回复' if target_type == 'reply' else '内容'}",
                        category='community',
                        source_type='discussion_like',
                        source_id=target_id,
                    )
                    db.session.commit()
            except Exception:
                db.session.rollback()

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

    like = DiscussionReaction.query.filter_by(
        user_id=user.id, target_type='thread', target_id=thread_id, reaction_type='like'
    ).first()
    bookmark = DiscussionReaction.query.filter_by(
        user_id=user.id, target_type='thread', target_id=thread_id, reaction_type='bookmark'
    ).first()

    return jsonify({
        "code": 200,
        "data": {
            "thread_id": thread_id,
            "liked": like is not None,
            "bookmarked": bookmark is not None
        }
    })


# 当前用户收藏的文章列表 GET /discussions/article/favorites/me
@bp.route("/article/favorites/me", methods=["GET"])
@jwt_required()
def my_article_favorites():
    """当前用户收藏（bookmark）的文章列表"""
    user = get_current_user()
    if not user:
        return jsonify({"code": 401, "message": "用户不存在"}), 401

    from models import ArticleModel, ArticleV2Model
    # 用户 bookmark 的 article / article_v2 scope threads
    rows = db.session.query(DiscussionThread, DiscussionReaction).join(
        DiscussionReaction,
        and_(
            DiscussionReaction.target_type == 'thread',
            DiscussionReaction.target_id == DiscussionThread.id,
            DiscussionReaction.reaction_type == 'bookmark',
        )
    ).filter(
        DiscussionReaction.user_id == user.id,
        DiscussionThread.scope_type.in_(['article', 'article_v2']),
    ).order_by(DiscussionReaction.created_at.desc()).all()

    result = []
    for thread, reaction in rows:
        if thread.scope_type == 'article_v2':
            article = ArticleV2Model.query.get(thread.scope_id)
            version = 2
        else:
            article = ArticleModel.query.get(thread.scope_id)
            version = 1
        if not article:
            continue
        result.append({
            "article_id": article.id,
            "article_version": version,
            "title": article.title,
            "introduction": article.introduction,
            "author": article.author.username if article.author else '',
            "author_avatar": get_avatar_url(article.author.avatar_url) if article.author else '',
            "publish_time": article.publish_time.strftime('%Y-%m-%d %H:%M:%S') if article.publish_time else '',
            "favorited_at": reaction.created_at.strftime('%Y-%m-%d %H:%M:%S') if reaction.created_at else '',
        })

    return jsonify({"code": 200, "data": result, "total": len(result)}), 200


# 帖子图床（社区重设计 09-19）：发帖/编辑帖的图片上传，保比例缩最长边 1600，
# 返回 /media/discussions/ 相对 URL；create/update 以 URL 数组引用（防外链）。
@bp.route("/upload_image", methods=["POST"])
@jwt_required()
def upload_image():
    file = request.files.get("image")
    if not file or not file.filename:
        return jsonify({"code": 400, "message": "缺少图片文件 image"}), 400
    ext = os.path.splitext(file.filename)[1].lower().lstrip(".")
    if ext not in ("jpg", "jpeg", "png", "webp"):
        return jsonify({"code": 400, "message": "图片仅支持 jpg/jpeg/png/webp"}), 400
    if file.content_length and file.content_length > 10 * 1024 * 1024:
        return jsonify({"code": 400, "message": "图片不能超过 10MB"}), 400
    try:
        data = imaging.showcase_gallery_bytes(file.stream)
    except imaging.ImageError as e:
        return jsonify({"code": 400, "message": f"图片无效：{e}"}), 400
    uid = uuid.uuid4()
    key = f"media/discussions/{str(uid)[:8]}/{uid}.webp"
    try:
        storage.put_object(key, io.BytesIO(data), len(data), "image/webp")
    except Exception as e:
        return jsonify({"code": 500, "message": f"图片存储失败：{e}"}), 500
    return jsonify({"code": 200, "url": _media_url(key)})

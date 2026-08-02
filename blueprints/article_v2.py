"""文章 V2 蓝图：正文存 Markdown（article_v2 表），与旧 article 蓝图完全隔离。

- url_prefix="/v2/article"，蓝图名 "article_v2"（唯一）
- 发布对任何登录用户开放（仅 @jwt_required，无 check_permission；与旧 /article/public 一致）
- 正文存 ArticleV2Model.content_md 字段，不写文件
- _is_article_manager / _ensure_article_access 自 article.py 复制（自包含，不碰旧码）
- ArticleV2Form 内联（不动 forms.py）
"""
from flask import Blueprint, request, jsonify
import wtforms
from wtforms.validators import length

from sqlalchemy import and_, or_
from datetime import datetime

from exts import db
from models import (
    ArticleV2Model, UserModel, PermissionModel, UserPermissionModel,
    DiscussionThread, DiscussionReply, DiscussionReaction,
)

# 导入token验证模块
from flask_jwt_extended import get_jwt_identity, jwt_required

# 导入api文档模块
from flasgger import swag_from

# 导入审计 / 当前用户
from . import audit_log, _current_user


bp = Blueprint("article_v2", __name__, url_prefix="/v2/article")


def _is_article_manager(user):
    """super_admin 直通；其余查 ACL 是否授予 article_management（逻辑同 check_permission）"""
    if not user:
        return False
    if user.is_admin_like():
        return True
    permission = PermissionModel.query.filter_by(name='article_management').first()
    if not permission:
        return False
    return UserPermissionModel.query.filter_by(
        user_id=user.id, permission_id=permission.id
    ).first() is not None


def _ensure_article_access(user, article):
    """文章管理权限或作者本人放行，否则返回 403 响应"""
    if article is None:
        return jsonify({"code": 404, "message": "文章不存在"}), 404
    if _is_article_manager(user) or article.author_id == user.id:
        return None
    return jsonify({"code": 403, "message": "用户权限不足"}), 403


def _article_to_dict(a, with_author_avatar=False, summary=False):
    """序列化文章为 dict（/list、/by_author、/my 共用，收敛重复）。

    - with_author_avatar：带 author_avatar（需 host_url，/by_author 用）
    - summary：把 introduction 截断为 summary 字段（/by_author 用）
    """
    d = {
        "id": a.id,
        "title": a.title or '',
        "introduction": a.introduction or '',
        "status": a.status,
        "author_id": a.author_id,
        "author_name": a.author.username if a.author else '',
        "created_at": a.created_at.strftime('%Y-%m-%d %H:%M:%S') if a.created_at else '',
        "updated_at": a.updated_at.strftime('%Y-%m-%d %H:%M:%S') if a.updated_at else '',
        "publish_time": a.publish_time.strftime('%Y-%m-%d %H:%M:%S') if a.publish_time else '',
    }
    if summary:
        d['summary'] = (a.introduction or '')[:200]
    if with_author_avatar:
        au = a.author.avatar_url if a.author else None
        if au:
            d['author_avatar'] = au if au.startswith('http') else f"{request.host_url.rstrip('/')}/data/avatars/{au}"
        else:
            d['author_avatar'] = ''
    return d


class ArticleV2Form(wtforms.Form):
    """V2 文章表单（内联，不动 forms.py）。字段名与前端 JSON 键一致（小写）。"""

    def __init__(self):
        if "application/json" in request.headers.get("Content-Type", ""):
            data = request.get_json(silent=True) or {}
            args = request.args.to_dict()
            super(ArticleV2Form, self).__init__(data=data, **args)
        else:
            data = request.form.to_dict()
            args = request.args.to_dict()
            super(ArticleV2Form, self).__init__(data=data, **args)

    title = wtforms.StringField(validators=[length(min=1, max=100, message='标题格式不对')])
    introduction = wtforms.StringField(validators=[length(min=1, max=300, message='简介格式不对')])
    content_md = wtforms.StringField(validators=[length(min=1, max=500000, message='正文内容过长（上限 50 万字符）')])


class ArticleV2DraftForm(wtforms.Form):
    """V2 草稿表单（内联）：字段均可空，草稿允许半成品；带可选 id（有=更新草稿）。"""

    def __init__(self):
        if "application/json" in request.headers.get("Content-Type", ""):
            data = request.get_json(silent=True) or {}
            args = request.args.to_dict()
            super(ArticleV2DraftForm, self).__init__(data=data, **args)
        else:
            data = request.form.to_dict()
            args = request.args.to_dict()
            super(ArticleV2DraftForm, self).__init__(data=data, **args)

    id = wtforms.IntegerField()
    title = wtforms.StringField(validators=[length(max=100, message='标题上限 100 字')])
    introduction = wtforms.StringField(validators=[length(max=300, message='简介上限 300 字')])
    content_md = wtforms.StringField(validators=[length(max=500000, message='正文内容过长（上限 50 万字符）')])


# 发布文章（存 md，不写文件）
@bp.route("/public", methods=["POST"])
@jwt_required()
@audit_log(operation="创建文章")
@swag_from('../apidocs/article_v2/public.yaml')
def article_v2_public():
    form = ArticleV2Form()
    if not form.validate():
        return jsonify({"code": 400, "message": form.errors}), 400

    title = form.title.data
    introduction = form.introduction.data
    content_md = form.content_md.data

    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    if user is None:
        return jsonify({"code": 401, "message": "用户不存在"}), 401

    article = ArticleV2Model(
        title=title, introduction=introduction,
        content_md=content_md, author_id=user.id,
        status=ArticleV2Model.STATUS_PUBLISHED, publish_time=datetime.now(),
    )
    db.session.add(article)
    db.session.commit()

    return jsonify({
        "code": 200,
        'message': '文章发布成功',
        "id": article.id,
        "title": article.title,
        "introduction": article.introduction,
    }), 200


# 保存草稿（无 id 新建草稿；带 id 更新现有草稿内容，仅限 status=draft）
@bp.route("/draft", methods=["POST"])
@jwt_required()
@audit_log(operation="保存草稿")
def article_v2_draft():
    form = ArticleV2DraftForm()
    if not form.validate():
        return jsonify({"code": 400, "message": form.errors}), 400

    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    if user is None:
        return jsonify({"code": 401, "message": "用户不存在"}), 401

    title = (form.title.data or '').strip()
    introduction = form.introduction.data or ''
    content_md = form.content_md.data or ''
    # 草稿至少要有标题或正文，避免存全空记录
    if not title and not content_md.strip():
        return jsonify({"code": 400, "message": "写点标题或内容再保存草稿"}), 400

    article_id = form.id.data
    if article_id:
        article = ArticleV2Model.query.filter_by(id=article_id).first()
        if article is None:
            return jsonify({"code": 404, "message": "文章不存在"}), 404
        check = _ensure_article_access(user, article)
        if check:
            return check
        if article.status != ArticleV2Model.STATUS_DRAFT:
            return jsonify({"code": 400, "message": "该文章已发布，请使用编辑功能"}), 400
        article.title = title
        article.introduction = introduction
        article.content_md = content_md
    else:
        article = ArticleV2Model(
            title=title, introduction=introduction, content_md=content_md,
            author_id=user.id, status=ArticleV2Model.STATUS_DRAFT, publish_time=None,
        )
        db.session.add(article)
    db.session.commit()
    return jsonify({
        "code": 200, "message": "草稿已保存",
        "id": article.id, "status": article.status,
    }), 200


# 发布草稿（draft→published；已发布则幂等返回）
@bp.route("/<int:article_id>/publish", methods=["POST"])
@jwt_required()
@audit_log(operation="发布文章")
def article_v2_publish(article_id):
    article = ArticleV2Model.query.filter_by(id=article_id).first()
    if article is None:
        return jsonify({"code": 404, "message": "文章不存在"}), 404
    check = _ensure_article_access(_current_user(), article)
    if check:
        return check
    # 可选：随发布一起更新内容（草稿发布时把当前编辑内容写入，避免改动丢失）
    data = request.get_json(silent=True) or {}
    if 'title' in data:
        article.title = data['title']
    if 'introduction' in data:
        article.introduction = data['introduction']
    if 'content_md' in data:
        article.content_md = data['content_md']
    if article.status != ArticleV2Model.STATUS_PUBLISHED:
        # 发布前补校验：标题与正文不能空
        if not (article.title or '').strip() or not (article.content_md or '').strip():
            return jsonify({"code": 400, "message": "标题和正文不能为空"}), 400
        article.status = ArticleV2Model.STATUS_PUBLISHED
        if not article.publish_time:
            article.publish_time = datetime.now()
    db.session.commit()
    return jsonify({"code": 200, "message": "文章发布成功", "id": article.id}), 200


# 获取文章详情（md 直接出字段，无需读文件）
@bp.route("/<int:article_id>", methods=["GET"])
@jwt_required(optional=True)
@swag_from('../apidocs/article_v2/get.yaml')
def article_v2_get(article_id):
    article = ArticleV2Model.query.filter_by(id=article_id).first()
    if article is None:
        return jsonify({"code": 404, "message": "文章不存在"}), 404
    # 草稿仅作者本人/管理员可见，其余视为不存在（不泄露草稿存在）
    if article.status == ArticleV2Model.STATUS_DRAFT:
        u = _current_user()
        if not (u is not None and (_is_article_manager(u) or article.author_id == u.id)):
            return jsonify({"code": 404, "message": "文章不存在"}), 404
    author_avatar = ''
    if article.author and article.author.avatar_url:
        au = article.author.avatar_url
        author_avatar = au if au.startswith('http') else request.host_url.rstrip('/') + '/data/avatars/' + au
    # 互动计数：取该 v2 文章的汇总 thread（只读，不创建）；匿名阅读页也能直接拿到
    thread = DiscussionThread.query.filter_by(
        scope_type='article_v2', scope_id=article_id,
        status=DiscussionThread.STATUS_NORMAL
    ).order_by(DiscussionThread.created_at.asc()).first()
    reply_count = thread.reply_count if thread else 0
    like_count = thread.like_count if thread else 0
    view_count = thread.view_count if thread else 0
    return jsonify({
        "code": 200,
        "message": "获取文章详情成功",
        "data": {
            "id": article.id,
            "title": article.title,
            "introduction": article.introduction,
            "content_md": article.content_md,
            "status": article.status,
            "publish_time": article.publish_time.strftime('%Y-%m-%d %H:%M:%S') if article.publish_time else "",
            "author_id": article.author_id,
            "author_name": article.author.username if article.author else "",
            "author_avatar": author_avatar,
            "reply_count": reply_count,
            "like_count": like_count,
            "view_count": view_count,
        }
    })


# 编辑文章（改 title/introduction/content_md）
@bp.route("/<int:article_id>/edit", methods=["POST"])
@jwt_required()
@audit_log(operation="编辑文章")
@swag_from('../apidocs/article_v2/edit.yaml')
def article_v2_edit(article_id):
    data = request.get_json(silent=True) or {}
    article = ArticleV2Model.query.filter_by(id=article_id).first()
    if article is None:
        return jsonify({"code": 404, "message": "文章不存在"}), 404
    check = _ensure_article_access(_current_user(), article)
    if check:
        return check

    if 'title' in data:
        article.title = data['title']
    if 'introduction' in data:
        article.introduction = data['introduction']
    content_md = data.get('content_md')
    if content_md is not None:
        if len(content_md) > 500000:
            return jsonify({"code": 400, 'message': '正文内容过长（上限 50 万字符）'}), 400
        article.content_md = content_md
    db.session.commit()
    return jsonify({"code": 200, "message": "文章编辑成功"})


# 删除文章（连同其 discussion 互动数据：thread / replies / reactions，软关联需手工清）
@bp.route("/<int:article_id>/delete", methods=["POST"])
@jwt_required()
@audit_log(operation="删除文章")
@swag_from('../apidocs/article_v2/delete.yaml')
def article_v2_delete(article_id):
    article = ArticleV2Model.query.filter_by(id=article_id).first()
    if article is None:
        return jsonify({"code": 404, "message": "找不到该文章"}), 404
    check = _ensure_article_access(_current_user(), article)
    if check:
        return check

    # 清理该 v2 文章的 discussion 互动（scope_type/target_type 均为软关联，无 DB FK 级联）
    v2_threads = DiscussionThread.query.filter_by(
        scope_type='article_v2', scope_id=article_id
    ).all()
    thread_ids = [t.id for t in v2_threads]
    reply_ids = [r.id for t in v2_threads
                 for r in DiscussionReply.query.filter_by(thread_id=t.id).all()]
    conds = []
    if thread_ids:
        conds.append(and_(DiscussionReaction.target_type == 'thread',
                          DiscussionReaction.target_id.in_(thread_ids)))
    if reply_ids:
        conds.append(and_(DiscussionReaction.target_type == 'reply',
                          DiscussionReaction.target_id.in_(reply_ids)))
    if conds:
        DiscussionReaction.query.filter(or_(*conds)).delete(synchronize_session=False)
    for t in v2_threads:
        db.session.delete(t)   # replies 由 DiscussionThread.replies cascade=all,delete-orphan 连带删

    db.session.delete(article)
    db.session.commit()
    return jsonify({"code": 200, "message": "文章删除成功"})


# 全部文章列表（{code,data:[...]} 风格，对齐旧 article_by_author）
@bp.route("/list", methods=["GET"])
@swag_from('../apidocs/article_v2/list.yaml')
def article_v2_list():
    articles = ArticleV2Model.query.filter_by(status=ArticleV2Model.STATUS_PUBLISHED)\
        .order_by(ArticleV2Model.publish_time.desc()).all()
    data = [_article_to_dict(a) for a in articles]
    return jsonify({"code": 200, "data": data})


# 某用户发布的文章列表
@bp.route("/by_author/<int:user_id>", methods=["GET"])
@swag_from('../apidocs/article_v2/by_author.yaml')
def article_v2_by_author(user_id):
    articles = ArticleV2Model.query.filter_by(author_id=user_id, status=ArticleV2Model.STATUS_PUBLISHED)\
        .order_by(ArticleV2Model.publish_time.desc()).all()
    data = [_article_to_dict(a, with_author_avatar=True, summary=True) for a in articles]
    return jsonify({"code": 200, "data": data}), 200


# 我的文章列表（管理用：含草稿，按 updated_at 倒序；?status=all|draft|published）
@bp.route("/my", methods=["GET"])
@jwt_required()
def article_v2_my():
    status = request.args.get('status', 'all')
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    if user is None:
        return jsonify({"code": 401, "message": "用户不存在"}), 401
    q = ArticleV2Model.query.filter_by(author_id=user.id)
    if status in (ArticleV2Model.STATUS_DRAFT, ArticleV2Model.STATUS_PUBLISHED):
        q = q.filter_by(status=status)
    articles = q.order_by(ArticleV2Model.updated_at.desc()).all()
    data = [_article_to_dict(a) for a in articles]
    return jsonify({"code": 200, "data": data}), 200

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

from exts import db
from models import ArticleV2Model, UserModel, PermissionModel, UserPermissionModel

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
        content_md=content_md, author_id=user.id
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


# 获取文章详情（md 直接出字段，无需读文件）
@bp.route("/<int:article_id>", methods=["GET"])
@swag_from('../apidocs/article_v2/get.yaml')
def article_v2_get(article_id):
    article = ArticleV2Model.query.filter_by(id=article_id).first()
    if article is None:
        return jsonify({"code": 404, "message": "文章不存在"}), 404
    author_avatar = ''
    if article.author and article.author.avatar_url:
        au = article.author.avatar_url
        author_avatar = au if au.startswith('http') else request.host_url.rstrip('/') + '/data/avatars/' + au
    return jsonify({
        "code": 200,
        "message": "获取文章详情成功",
        "data": {
            "id": article.id,
            "title": article.title,
            "introduction": article.introduction,
            "content_md": article.content_md,
            "publish_time": article.publish_time.strftime('%Y-%m-%d %H:%M:%S') if article.publish_time else "",
            "author_id": article.author_id,
            "author_name": article.author.username if article.author else "",
            "author_avatar": author_avatar,
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


# 删除文章（V2 第一版无评论关系，仅删记录）
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
    db.session.delete(article)
    db.session.commit()
    return jsonify({"code": 200, "message": "文章删除成功"})


# 全部文章列表（{code,data:[...]} 风格，对齐旧 article_by_author）
@bp.route("/list", methods=["GET"])
@swag_from('../apidocs/article_v2/list.yaml')
def article_v2_list():
    articles = ArticleV2Model.query.order_by(ArticleV2Model.publish_time.desc()).all()
    data = []
    for a in articles:
        data.append({
            "id": a.id,
            "title": a.title,
            "introduction": a.introduction,
            "publish_time": a.publish_time.strftime('%Y-%m-%d %H:%M:%S') if a.publish_time else "",
            "author_id": a.author_id,
            "author_name": a.author.username if a.author else "",
        })
    return jsonify({"code": 200, "data": data})


# 某用户发布的文章列表
@bp.route("/by_author/<int:user_id>", methods=["GET"])
@swag_from('../apidocs/article_v2/by_author.yaml')
def article_v2_by_author(user_id):
    articles = ArticleV2Model.query.filter_by(author_id=user_id)\
        .order_by(ArticleV2Model.publish_time.desc()).all()
    host = request.host_url.rstrip('/')
    data = []
    for a in articles:
        au = a.author.avatar_url if a.author else None
        avatar = ''
        if au:
            avatar = au if au.startswith('http') else f"{host}/data/avatars/{au}"
        data.append({
            "id": a.id,
            "title": a.title,
            "summary": (a.introduction or '')[:200],
            "author_id": a.author_id,
            "author_name": a.author.username if a.author else "",
            "author_avatar": avatar,
            "publish_time": a.publish_time.strftime('%Y-%m-%d %H:%M:%S') if a.publish_time else "",
        })
    return jsonify({"code": 200, "data": data}), 200

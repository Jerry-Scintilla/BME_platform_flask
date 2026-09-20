"""文章 V2 蓝图：正文存 Markdown（article_v2 表），与旧 article 蓝图完全隔离。

- url_prefix="/v2/article"，蓝图名 "article_v2"（唯一）
- 发布对任何登录用户开放（仅 @jwt_required，无 check_permission；与旧 /article/public 一致）
- 正文存 ArticleV2Model.content_md 字段，不写文件
- _is_article_manager / _ensure_article_access 自 article.py 复制（自包含，不碰旧码）
- ArticleV2Form 内联（不动 forms.py）
- 社区重设计（09-19）：cover_image_key 封面 + is_official 官方推文（仅文章管理员可设）+
  编辑器图床 /upload_image + 发布限流（同讨论帖规格）
- 官方富文本推文（09-20 方案）：content_type=html 第二种正文格式，仅文章管理员可写，
  服务端每写必洗（services/article_html.py），必须官方推文；/admin/html/import 导入。
"""
import io
import json
import os
import uuid

from flask import Blueprint, request, jsonify, current_app
import wtforms
from wtforms.validators import length

from sqlalchemy import and_, or_
from datetime import datetime

from exts import db, redis_client
from models import (
    ArticleV2Model, UserModel, PermissionModel, UserPermissionModel,
    DiscussionThread, DiscussionReply, DiscussionReaction,
)
from storage import storage
import imaging
from services import article_html

# 导入token验证模块
from flask_jwt_extended import get_jwt_identity, jwt_required

# 导入api文档模块
from flasgger import swag_from

# 导入审计 / 当前用户
from . import audit_log, _current_user
from .media import public_avatar_url, media_url


bp = Blueprint("article_v2", __name__, url_prefix="/v2/article")

IMG_EXTS = ("jpg", "jpeg", "png", "webp")
IMG_MAX_BYTES = 10 * 1024 * 1024


def _publish_rate_guard(user):
    """发文限流（同讨论帖规格：5 分钟 ≤3、小时 ≤10；redis 不可用降级不限流）。超限返回 429 响应。"""
    uid = str(user.id)
    try:
        n5 = redis_client.incr(f"article_rate:{uid}:5m")
        if n5 == 1:
            redis_client.expire(f"article_rate:{uid}:5m", 300)
        n1h = redis_client.incr(f"article_rate:{uid}:1h")
        if n1h == 1:
            redis_client.expire(f"article_rate:{uid}:1h", 3600)
    except Exception:
        return None
    if n5 > 3:
        return jsonify({"code": 429, "message": "发文太频繁，请 5 分钟后再试"}), 429
    if n1h > 10:
        return jsonify({"code": 429, "message": "发文太频繁，请稍后再试"}), 429
    return None


def _official_flag(data, user, article=None):
    """从请求体解析 is_official：仅文章管理员可设（推文是运营动作）；其余请求一律不动该标记。
    返回 (值或 None=不改, 错误响应或 None)。"""
    if 'is_official' not in (data or {}):
        return None, None
    if not _is_article_manager(user):
        return None, (jsonify({"code": 403, "message": "官方推文标记仅文章管理员可设置"}), 403)
    val = data['is_official']
    if not isinstance(val, bool):
        return None, (jsonify({"code": 400, "message": "is_official 须为布尔值"}), 400)
    return val, None


def _essence_flag(data, user):
    """is_essence 解析（仅文章管理员可设，与 is_official 同门禁；Phase 3 精华标记）。"""
    if 'is_essence' not in (data or {}):
        return None, None
    if not _is_article_manager(user):
        return None, (jsonify({"code": 403, "message": "精华标记仅文章管理员可设置"}), 403)
    val = data['is_essence']
    if not isinstance(val, bool):
        return None, (jsonify({"code": 400, "message": "is_essence 须为布尔值"}), 400)
    return val, None


def _remove_cover_objects(article):
    """删除封面母版 + 缩略对象（best-effort）。"""
    if not article.cover_image_key:
        return
    stem, ext = article.cover_image_key.lstrip('/').rsplit('.', 1)
    for key in (article.cover_image_key.lstrip('/'), f"{stem}_thumb.{ext}"):
        try:
            storage.remove_object(key)
        except Exception:
            pass


def _cover_thumb_url(article):
    if not article.cover_image_key:
        return None
    stem, ext = article.cover_image_key.rsplit('.', 1)
    return f"{stem}_thumb.{ext}"


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


# ─────────────────────────────────────────────
# 官方富文本（content_type）集中校验（方案 §6.3）
# 草稿/发布/草稿转发布/编辑全部走这里，权限与互斥规则不散落路由。
# ─────────────────────────────────────────────

def _html_enabled():
    return bool(current_app.config.get("ARTICLE_HTML_ENABLED", True))


def _content_fields(data, user, article=None):
    """解析并校验 content_type / content_md / content_html。

    返回 (content_type, content_md, content_html, 错误响应)；正文值为 None 表示本次请求未携带。
    规则（方案 §5.2/§6）：
    - 缺省 content_type 按存量 markdown 处理（向后兼容，老客户端不受影响）
    - html 仅文章管理员可用；HTML 文章连作者本人（非管理员）也不放行
    - 格式一经保存锁定：已有文章的 content_type 不可改
    - markdown 与 content_html 互斥；html 与 content_md 互斥
    """
    data = data or {}
    ctype = data.get('content_type')
    if ctype is None:
        ctype = article.content_type if article is not None else ArticleV2Model.CONTENT_TYPE_MD
    if ctype not in (ArticleV2Model.CONTENT_TYPE_MD, ArticleV2Model.CONTENT_TYPE_HTML):
        return None, None, None, (jsonify({"code": 400, "message": "content_type 仅支持 markdown/html"}), 400)

    if ctype == ArticleV2Model.CONTENT_TYPE_HTML:
        if not _html_enabled():
            return None, None, None, (jsonify({"code": 403, "message": "官方富文本功能已关闭"}), 403)
        if not _is_article_manager(user):
            return None, None, None, (jsonify({"code": 403, "message": "HTML 正文仅文章管理员可用"}), 403)

    if article is not None and ctype != article.content_type:
        return None, None, None, (
            jsonify({"code": 400, "message": f"正文格式已锁定为 {article.content_type}，如需另一种格式请复制为新文章"}), 400)

    content_md = data.get('content_md')
    content_html = data.get('content_html')
    if ctype == ArticleV2Model.CONTENT_TYPE_MD:
        if content_html:
            return None, None, None, (jsonify({"code": 400, "message": "Markdown 文章不能提交 content_html"}), 400)
        if content_md is not None and len(content_md) > 500000:
            return None, None, None, (jsonify({"code": 400, "message": "正文内容过长（上限 50 万字符）"}), 400)
    else:
        if content_md:
            return None, None, None, (jsonify({"code": 400, "message": "HTML 文章不能提交 content_md"}), 400)
        if content_html is not None and len(content_html.encode("utf-8", errors="ignore")) > article_html.RAW_HTML_MAX_BYTES:
            return None, None, None, (jsonify({"code": 400, "message": "正文超过 3MB 上限"}), 400)
    return ctype, content_md, content_html, None


def _official_for_html(official):
    """HTML 文章的官方标记规则（方案 §6.2）：必须官方；显式 false 拒绝；缺省补 True。
    返回 (生效值, 错误响应)。"""
    if official is False:
        return None, (jsonify({"code": 400, "message": "HTML 文章必须为官方推文，不能取消官方标记；如下线请直接下架文章"}), 400)
    return True, None


def _sanitize_and_set_html(article, content_html):
    """写库前的服务端重洗（幂等），以清洗结果覆盖客户端正文。返回错误响应或 None。"""
    if content_html is None:
        return None
    try:
        cleaned, _report = article_html.clean_for_save(article.id, content_html)
    except article_html.HtmlImportError as e:
        return jsonify({"code": 400, "message": str(e)}), 400
    article.content_html = cleaned or None
    article.content_version = article.content_version or 1
    return None


def _article_to_dict(a, with_author_avatar=False, summary=False):
    """序列化文章为 dict（/list、/by_author、/my 共用，收敛重复）。

    - with_author_avatar：带 author_avatar（需 host_url，/by_author 用）
    - summary：把 introduction 截断为 summary 字段（/by_author 用）
    """
    d = {
        "id": a.id,
        "title": a.title or '',
        "introduction": a.introduction or '',
        "content_type": a.content_type or ArticleV2Model.CONTENT_TYPE_MD,
        "status": a.status,
        "cover": a.cover_image_key,
        "cover_thumb": _cover_thumb_url(a),
        "is_official": bool(a.is_official),
        "is_essence": bool(a.is_essence),
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
            d['author_avatar'] = public_avatar_url(au)
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
    # content_md 改为可选：HTML 官方推文发布不带 content_md（必填校验移到 _content_fields 按格式分流）
    content_md = wtforms.StringField(validators=[length(max=500000, message='正文内容过长（上限 50 万字符）')])


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


# 发布文章（markdown 或 html 官方推文；html 仅文章管理员，服务端重洗后落库）
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

    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    if user is None:
        return jsonify({"code": 401, "message": "用户不存在"}), 401

    data = request.get_json(silent=True) or {}
    ctype, content_md, content_html, err = _content_fields(data, user)
    if err:
        return err
    # 发布必须有正文：markdown 看 content_md，html 看 content_html
    if ctype == ArticleV2Model.CONTENT_TYPE_MD and not (content_md or '').strip():
        return jsonify({"code": 400, "message": "正文内容不能为空"}), 400
    if ctype == ArticleV2Model.CONTENT_TYPE_HTML and not (content_html or '').strip():
        return jsonify({"code": 400, "message": "正文内容不能为空"}), 400

    official, err = _official_flag(data, user)
    if err:
        return err
    essence, err = _essence_flag(data, user)
    if err:
        return err
    if ctype == ArticleV2Model.CONTENT_TYPE_HTML:
        official, err = _official_for_html(official)
        if err:
            return err
    limited = _publish_rate_guard(user)
    if limited:
        return limited

    article = ArticleV2Model(
        title=title, introduction=introduction,
        content_md=content_md if ctype == ArticleV2Model.CONTENT_TYPE_MD else None,
        content_type=ctype, content_version=1,
        author_id=user.id,
        status=ArticleV2Model.STATUS_PUBLISHED, publish_time=datetime.now(),
        is_official=bool(official),
        is_essence=bool(essence),
    )
    db.session.add(article)
    db.session.flush()                     # 先拿 id：HTML 转存路径要挂文章归属
    if ctype == ArticleV2Model.CONTENT_TYPE_HTML:
        err = _sanitize_and_set_html(article, content_html)
        if err:
            db.session.rollback()
            return err
    db.session.commit()

    return jsonify({
        "code": 200,
        'message': '文章发布成功',
        "id": article.id,
        "title": article.title,
        "introduction": article.introduction,
        "is_official": bool(article.is_official),
    }), 200


# 保存草稿（无 id 新建草稿；带 id 更新现有草稿内容，仅限 status=draft；
# HTML 官方推文草稿允许全空——新建入口先建空草稿拿 id 再进编辑器，方案 §8.1）
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

    data = request.get_json(silent=True) or {}
    article_id = form.id.data
    existing = None
    if article_id:
        existing = ArticleV2Model.query.filter_by(id=article_id).first()
        if existing is None:
            return jsonify({"code": 404, "message": "文章不存在"}), 404
        check = _ensure_article_access(user, existing)
        if check:
            return check

    ctype, content_md, content_html, err = _content_fields(data, user, existing)
    if err:
        return err

    title = (form.title.data or '').strip()
    introduction = form.introduction.data or ''
    # 草稿至少要有标题或正文（HTML 空草稿例外：新建流程先占位）
    if ctype == ArticleV2Model.CONTENT_TYPE_MD:
        if not title and not (content_md or '').strip():
            return jsonify({"code": 400, "message": "写点标题或内容再保存草稿"}), 400
        content_html = None
    else:
        content_md = None

    official, err = _official_flag(data, user)
    if err:
        return err
    if ctype == ArticleV2Model.CONTENT_TYPE_HTML:
        official, err = _official_for_html(official)
        if err:
            return err

    if existing is not None:
        if existing.status != ArticleV2Model.STATUS_DRAFT:
            return jsonify({"code": 400, "message": "该文章已发布，请使用编辑功能"}), 400
        existing.title = title
        existing.introduction = introduction
        if ctype == ArticleV2Model.CONTENT_TYPE_MD:
            existing.content_md = content_md or ''
        else:
            err = _sanitize_and_set_html(existing, content_html)
            if err:
                return err
        if official is not None:
            existing.is_official = official
        article = existing
    else:
        article = ArticleV2Model(
            title=title, introduction=introduction,
            content_md=(content_md or '') if ctype == ArticleV2Model.CONTENT_TYPE_MD else None,
            content_type=ctype, content_version=1,
            author_id=user.id, status=ArticleV2Model.STATUS_DRAFT, publish_time=None,
            is_official=bool(official),
        )
        db.session.add(article)
        db.session.flush()                 # 先拿 id，HTML 正文转存挂文章归属
        if ctype == ArticleV2Model.CONTENT_TYPE_HTML:
            err = _sanitize_and_set_html(article, content_html)
            if err:
                db.session.rollback()
                return err
    db.session.commit()
    return jsonify({
        "code": 200, "message": "草稿已保存",
        "id": article.id, "status": article.status,
        "content_type": article.content_type,
    }), 200


# 发布草稿（draft→published；已发布则幂等返回）
@bp.route("/<int:article_id>/publish", methods=["POST"])
@jwt_required()
@audit_log(operation="发布文章")
def article_v2_publish(article_id):
    user = _current_user()
    article = ArticleV2Model.query.filter_by(id=article_id).first()
    if article is None:
        return jsonify({"code": 404, "message": "文章不存在"}), 404
    check = _ensure_article_access(user, article)
    if check:
        return check
    # HTML 文章的发布属编辑动作：非文章管理员连作者本人也不放行（方案 §6.1）
    if article.content_type == ArticleV2Model.CONTENT_TYPE_HTML and not _is_article_manager(user):
        return jsonify({"code": 403, "message": "HTML 文章仅文章管理员可操作"}), 403
    # 可选：随发布一起更新内容（草稿发布时把当前编辑内容写入，避免改动丢失）
    data = request.get_json(silent=True) or {}
    ctype, content_md, content_html, err = _content_fields(data, user, article)
    if err:
        return err
    if 'title' in data:
        article.title = data['title']
    if 'introduction' in data:
        article.introduction = data['introduction']
    if ctype == ArticleV2Model.CONTENT_TYPE_MD:
        if content_md is not None:
            article.content_md = content_md
    else:
        err = _sanitize_and_set_html(article, content_html)
        if err:
            return err
    official, err = _official_flag(data, user)
    if err:
        return err
    if article.content_type == ArticleV2Model.CONTENT_TYPE_HTML:
        official, err = _official_for_html(official)
        if err:
            return err
    if official is not None:
        article.is_official = official
    essence, err = _essence_flag(data, user)
    if err:
        return err
    if essence is not None:
        article.is_essence = essence
    if article.status != ArticleV2Model.STATUS_PUBLISHED:
        # 发布前补校验：标题与正文不能空（按格式分流；HTML 还须无转存失败占位块）
        if not (article.title or '').strip():
            return jsonify({"code": 400, "message": "标题不能为空"}), 400
        if article.content_type == ArticleV2Model.CONTENT_TYPE_MD:
            if not (article.content_md or '').strip():
                return jsonify({"code": 400, "message": "正文不能为空"}), 400
        else:
            if not (article.content_html or '').strip():
                return jsonify({"code": 400, "message": "正文不能为空"}), 400
            if article_html.has_failed_images(article.content_html):
                return jsonify({"code": 400, "message": "正文中仍有图片转存失败占位块，请处理后（删除或重传）再发布"}), 400
        article.status = ArticleV2Model.STATUS_PUBLISHED
        if not article.publish_time:
            article.publish_time = datetime.now()
    db.session.commit()
    return jsonify({"code": 200, "message": "文章发布成功", "id": article.id}), 200


# 下架：已发布转草稿（admin 状态管理；publish_time 保留不重设，再次发布不覆盖）
@bp.route("/<int:article_id>/unpublish", methods=["POST"])
@jwt_required()
@audit_log(operation="下架文章")
def article_v2_unpublish(article_id):
    article = ArticleV2Model.query.filter_by(id=article_id).first()
    if article is None:
        return jsonify({"code": 404, "message": "文章不存在"}), 404
    check = _ensure_article_access(_current_user(), article)
    if check:
        return check
    if article.status != ArticleV2Model.STATUS_DRAFT:
        article.status = ArticleV2Model.STATUS_DRAFT
        db.session.commit()
    return jsonify({"code": 200, "message": "文章已下架", "id": article.id}), 200


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
        author_avatar = public_avatar_url(au)
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
            "content_type": article.content_type or ArticleV2Model.CONTENT_TYPE_MD,
            "content_version": article.content_version or 1,
            "content_md": article.content_md if article.content_type != ArticleV2Model.CONTENT_TYPE_HTML else None,
            "content_html": article.content_html if article.content_type == ArticleV2Model.CONTENT_TYPE_HTML else None,
            "status": article.status,
            "cover": article.cover_image_key,
            "cover_thumb": _cover_thumb_url(article),
            "is_official": bool(article.is_official),
            "is_essence": bool(article.is_essence),
            "publish_time": article.publish_time.strftime('%Y-%m-%d %H:%M:%S') if article.publish_time else "",
            "author_id": article.author_id,
            "author_name": article.author.username if article.author else "",
            "author_avatar": author_avatar,
            "reply_count": reply_count,
            "like_count": like_count,
            "view_count": view_count,
        }
    })


# 编辑文章（改 title/introduction/正文；正文按 content_type 分流且每次写库前重洗）
@bp.route("/<int:article_id>/edit", methods=["POST"])
@jwt_required()
@audit_log(operation="编辑文章")
@swag_from('../apidocs/article_v2/edit.yaml')
def article_v2_edit(article_id):
    user = _current_user()
    data = request.get_json(silent=True) or {}
    article = ArticleV2Model.query.filter_by(id=article_id).first()
    if article is None:
        return jsonify({"code": 404, "message": "文章不存在"}), 404
    check = _ensure_article_access(user, article)
    if check:
        return check
    # HTML 文章编辑：非文章管理员连作者本人也不放行（方案 §6.1）
    if article.content_type == ArticleV2Model.CONTENT_TYPE_HTML and not _is_article_manager(user):
        return jsonify({"code": 403, "message": "HTML 文章仅文章管理员可编辑"}), 403

    ctype, content_md, content_html, err = _content_fields(data, user, article)
    if err:
        return err

    if 'title' in data:
        article.title = data['title']
    if 'introduction' in data:
        article.introduction = data['introduction']
    if ctype == ArticleV2Model.CONTENT_TYPE_MD:
        if content_md is not None:
            if len(content_md) > 500000:
                return jsonify({"code": 400, 'message': '正文内容过长（上限 50 万字符）'}), 400
            article.content_md = content_md
    else:
        err = _sanitize_and_set_html(article, content_html)
        if err:
            return err
    official, err = _official_flag(data, user)
    if err:
        return err
    if article.content_type == ArticleV2Model.CONTENT_TYPE_HTML:
        official, err = _official_for_html(official)
        if err:
            return err
    if official is not None:
        article.is_official = official
    essence, err = _essence_flag(data, user)
    if err:
        return err
    if essence is not None:
        article.is_essence = essence
    db.session.commit()
    return jsonify({"code": 200, "message": "文章编辑成功"})


# ─────────────────────────────────────────────
# 媒体（社区重设计 09-19）：封面成对转码 + 编辑器图床
# ─────────────────────────────────────────────

# 封面上传/替换（16:9 母版+缩略成对入 storage；作者本人或文章管理员）
@bp.route("/<int:article_id>/cover", methods=["POST"])
@jwt_required()
@audit_log(operation="上传文章封面")
def article_v2_cover_update(article_id):
    article = ArticleV2Model.query.filter_by(id=article_id).first()
    if article is None:
        return jsonify({"code": 404, "message": "文章不存在"}), 404
    check = _ensure_article_access(_current_user(), article)
    if check:
        return check
    file = request.files.get("cover")
    if not file or not file.filename:
        return jsonify({"code": 400, "message": "缺少封面文件 cover"}), 400
    ext = os.path.splitext(file.filename)[1].lower().lstrip(".")
    if ext not in IMG_EXTS:
        return jsonify({"code": 400, "message": "封面仅支持 jpg/jpeg/png/webp"}), 400
    if file.content_length and file.content_length > IMG_MAX_BYTES:
        return jsonify({"code": 400, "message": "封面不能超过 10MB"}), 400

    try:
        master, thumb = imaging.article_cover_pair(file.stream)
    except imaging.ImageError as e:
        return jsonify({"code": 400, "message": f"封面图无效：{e}"}), 400

    uid = uuid.uuid4().hex
    master_key = f"media/articles/{article.id}/{uid}.webp"
    thumb_key = f"media/articles/{article.id}/{uid}_thumb.webp"
    try:
        storage.put_object(master_key, io.BytesIO(master), len(master), "image/webp")
        storage.put_object(thumb_key, io.BytesIO(thumb), len(thumb), "image/webp")
    except Exception as e:
        return jsonify({"code": 500, "message": f"封面存储失败：{e}"}), 500

    _remove_cover_objects(article)                    # 替换：清旧对象（best-effort）
    article.cover_image_key = media_url(master_key)
    db.session.commit()
    return jsonify({"code": 200, "message": "封面上传成功",
                    "cover": article.cover_image_key, "cover_thumb": _cover_thumb_url(article)})


# 封面删除（回退无封面样式）
@bp.route("/<int:article_id>/cover/delete", methods=["POST"])
@jwt_required()
@audit_log(operation="删除文章封面")
def article_v2_cover_delete(article_id):
    article = ArticleV2Model.query.filter_by(id=article_id).first()
    if article is None:
        return jsonify({"code": 404, "message": "文章不存在"}), 404
    check = _ensure_article_access(_current_user(), article)
    if check:
        return check
    _remove_cover_objects(article)
    article.cover_image_key = None
    db.session.commit()
    return jsonify({"code": 200, "message": "封面已删除"})


# 编辑器图床（md-editor-v3 on-upload-image）：正文插图上传，保比例缩最长边 1600，
# 返回 /media 相对 URL 供 markdown 引用。挂在作者名下（article 未建时按 uid 归档）
@bp.route("/upload_image", methods=["POST"])
@jwt_required()
def article_v2_upload_image():
    file = request.files.get("image")
    if not file or not file.filename:
        return jsonify({"code": 400, "message": "缺少图片文件 image"}), 400
    ext = os.path.splitext(file.filename)[1].lower().lstrip(".")
    if ext not in IMG_EXTS:
        return jsonify({"code": 400, "message": "插图仅支持 jpg/jpeg/png/webp"}), 400
    if file.content_length and file.content_length > IMG_MAX_BYTES:
        return jsonify({"code": 400, "message": "插图不能超过 10MB"}), 400
    try:
        data = imaging.showcase_gallery_bytes(file.stream)
    except imaging.ImageError as e:
        return jsonify({"code": 400, "message": f"图片无效：{e}"}), 400
    uid = str(uuid.uuid4())
    key = f"media/articles/inline/{uid[:8]}/{uid}.webp"
    try:
        storage.put_object(key, io.BytesIO(data), len(data), "image/webp")
    except Exception as e:
        return jsonify({"code": 500, "message": f"图片存储失败：{e}"}), 500
    return jsonify({"code": 200, "url": media_url(key)})


# ─────────────────────────────────────────────
# 官方富文本导入（方案 §7.2）：剪贴板 HTML -> 归一化/转存/清洗 -> 干净 HTML + 报告
# ─────────────────────────────────────────────

# 官方富文本导入（multipart/form-data：article_id 必填先建草稿、html 剪贴板内容、
# source_hint 可选、files[] 剪贴板本地图片、file_map 占位符->files 序号映射）
@bp.route("/admin/html/import", methods=["POST"])
@jwt_required()
@audit_log(operation="导入官方富文本")
@swag_from('../apidocs/article_v2/admin_html_import.yaml')
def article_v2_admin_html_import():
    user = _current_user()
    if not _is_article_manager(user):
        return jsonify({"code": 403, "message": "需要文章管理权限"}), 403
    if not _html_enabled():
        return jsonify({"code": 403, "message": "官方富文本功能已关闭"}), 403

    article_id = request.form.get('article_id', type=int)
    if not article_id:
        return jsonify({"code": 400, "message": "缺少 article_id（请先创建草稿再导入）"}), 400
    article = ArticleV2Model.query.filter_by(id=article_id).first()
    if article is None:
        return jsonify({"code": 404, "message": "文章不存在"}), 404
    check = _ensure_article_access(user, article)
    if check:
        return check
    if article.content_type != ArticleV2Model.CONTENT_TYPE_HTML:
        return jsonify({"code": 400, "message": "仅 HTML 官方推文可导入富文本"}), 400

    raw_html = request.form.get('html')
    if not raw_html or not raw_html.strip():
        return jsonify({"code": 400, "message": "缺少 html 字段（剪贴板 text/html）"}), 400
    source_hint = request.form.get('source_hint') or 'unknown'
    files = request.files.getlist('files')
    file_map_raw = request.form.get('file_map')
    file_map = {}
    if file_map_raw:
        try:
            file_map = json.loads(file_map_raw)
            if not isinstance(file_map, dict):
                file_map = {}
        except (ValueError, TypeError):
            return jsonify({"code": 400, "message": "file_map 须为 JSON 对象"}), 400

    try:
        cleaned, report, file_urls = article_html.import_html(article.id, raw_html, source_hint, files)
    except article_html.HtmlImportError as e:
        return jsonify({"code": 400, "message": str(e)}), 400

    # 占位符替换（剪贴板图片文件与 HTML 内占位文本的映射，按 files 序号）
    for placeholder, idx in file_map.items():
        try:
            url = file_urls[int(idx)]
            cleaned = cleaned.replace(str(placeholder), url)
        except (ValueError, IndexError, KeyError):
            pass

    return jsonify({
        "code": 200,
        "message": "导入完成",
        "data": {"html": cleaned, "report": report, "files": file_urls},
    }), 200


# 删除文章（连同其 discussion 互动数据：thread / replies / reactions，软关联需手工清；
# HTML 文章额外清理其媒体目录 media/articles/html/<id>/，方案 §12.5）
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
    if article.content_type == ArticleV2Model.CONTENT_TYPE_HTML:
        article_html.remove_article_html_media(article.id)   # best-effort，失败不阻塞删除

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


# admin 列全部 v2 文章（全部作者 + 含草稿；?status=all|draft|published &q= &author_id= &official=true|false）
@bp.route("/admin/list", methods=["GET"])
@jwt_required()
def article_v2_admin_list():
    if not _is_article_manager(_current_user()):
        return jsonify({"code": 403, "message": "需要文章管理权限"}), 403
    status = request.args.get('status', 'all')
    author_id = request.args.get('author_id', type=int)
    kw = request.args.get('q', '', type=str).strip()
    official = request.args.get('official')
    query = ArticleV2Model.query
    if status in (ArticleV2Model.STATUS_DRAFT, ArticleV2Model.STATUS_PUBLISHED):
        query = query.filter_by(status=status)
    if author_id:
        query = query.filter_by(author_id=author_id)
    if official is not None and official != '':
        query = query.filter(ArticleV2Model.is_official.is_(official.lower() == 'true'))
    if kw:
        like = f'%{kw}%'
        query = query.filter(or_(ArticleV2Model.title.like(like),
                                 ArticleV2Model.introduction.like(like)))
    articles = query.order_by(ArticleV2Model.updated_at.desc()).all()
    data = [_article_to_dict(a) for a in articles]
    return jsonify({"code": 200, "data": data}), 200

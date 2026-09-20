"""学习资源中心蓝图（2026-09-20，migrate_40）。

两类来源聚合（课程资料按课程归组、平台资料按分类归组，互不打散）：
- 课程资料：复用 CourseResourceModel 及既有五件套接口（/course/resources 列表 +
  Down_Code 一次性码下载），本蓝图不动它，只提供聚合导航与跨来源搜索；
- 平台资料：StandaloneResourceModel，管理员上传的独立资料（不挂课程）。

下载链路口径：平台资料走 media_sign 短签（2h 多次有效，为第二期在线预览铺路），
与课程资料的 Down_Code 一次性码并存、互不迁移。权限：登录可见可下载（对齐课程
资源现状）；上传/编辑/删除/排序 _require_admin（与课程资源上传同口径）。
"""
import os
import uuid

from flask import Blueprint, Response, jsonify, request
from flask_jwt_extended import get_jwt_identity, jwt_required
from sqlalchemy import func
from urllib.parse import quote

from exts import db
from models import UserModel, CourseModel, CourseResourceModel, StandaloneResourceModel
from storage import storage

from . import audit_log, _current_user
from .media_sign import media_token_response, resolve_media_request

bp = Blueprint("resource_center", __name__, url_prefix="/resources")

MAX_FILE_MB = 100    # 单文件上限（与营期材料/课程资源同口径）
SEARCH_LIMIT = 20    # 全局搜索单来源条数上限

# 分类枚举（应用层常量，不建字典表；顺序即左栏展示顺序）
CATEGORIES = {
    'software': '软件工具',
    'handbook': '学习手册',
    'standard': '规范文档',
    'other': '其他',
}


def _require_admin():
    """admin 校验，通过返回 None，否则返回错误响应。与 course.py 同口径。"""
    user = UserModel.query.filter_by(email=get_jwt_identity()).first()
    if user is None or not user.is_admin():
        return jsonify({"code": 400, 'message': "用户权限不够"}), 400
    return None


def _like(keyword):
    """LIKE 模式 + 通配符转义（照 user.py 搜索口径）。"""
    return "%" + keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _category_label(key):
    return CATEGORIES.get(key, CATEGORIES['other'])


# ── 聚合导航 ──────────────────────────────────────────────────────────────

@bp.route("/catalog")
@jwt_required()
def catalog():
    """左栏导航：平台四分类（恒全量，计数可 0）+ 有资料的课程（软删除课程过滤）。"""
    cat_counts = dict(db.session.query(
        StandaloneResourceModel.category, func.count(StandaloneResourceModel.id)
    ).group_by(StandaloneResourceModel.category).all())
    categories = [{'key': k, 'label': v, 'count': cat_counts.get(k, 0)}
                  for k, v in CATEGORIES.items()]

    courses = (db.session.query(
                   CourseModel.id, CourseModel.title,
                   func.count(CourseResourceModel.id))
               .join(CourseResourceModel,
                     CourseResourceModel.course_id == CourseModel.id)
               .filter(CourseModel.status == CourseModel.STATUS_NORMAL)
               .group_by(CourseModel.id, CourseModel.title)
               .order_by(func.count(CourseResourceModel.id).desc(), CourseModel.id)
               .all())
    course_items = [{'id': cid, 'title': title, 'count': cnt}
                    for cid, title, cnt in courses]

    return jsonify({"code": 200,
                    "categories": categories,
                    "courses": course_items})


# ── 平台资料·用户端 ──────────────────────────────────────────────────────

@bp.route("/standalone")
@jwt_required()
def standalone_list():
    """平台资料列表：category 可选过滤（枚举外值忽略）、keyword 名称模糊、真分页。"""
    category = request.args.get("category")
    if category and category not in CATEGORIES:
        category = None
    keyword = (request.args.get("keyword") or "").strip()
    try:
        page = max(1, int(request.args.get("page", 1)))
        per_page = min(50, max(1, int(request.args.get("per_page", 20))))
    except (ValueError, TypeError):
        return jsonify({"code": 400, "message": "分页参数错误"}), 400

    q = StandaloneResourceModel.query
    if category:
        q = q.filter_by(category=category)
    if keyword:
        q = q.filter(StandaloneResourceModel.name.like(_like(keyword), escape="\\"))
    total = q.count()
    rows = (q.order_by(StandaloneResourceModel.sort_order, StandaloneResourceModel.id)
            .offset((page - 1) * per_page).limit(per_page).all())
    pages = (total + per_page - 1) // per_page
    return jsonify({"code": 200, "data": [r.to_dict() for r in rows],
                    "total": total, "page": page, "per_page": per_page, "pages": pages})


@bp.route("/search")
@jwt_required()
def search():
    """全局搜索：跨平台资料 + 课程资料（软删除课程过滤）按名称模糊，各取前 N 条。
    条目带 source 与来源标签字段（平台→category_label，课程→course_title）。"""
    keyword = (request.args.get("keyword") or "").strip()
    if not keyword:
        return jsonify({"code": 400, "message": "请输入搜索关键词"}), 400
    try:
        limit = min(50, max(1, int(request.args.get("limit", SEARCH_LIMIT))))
    except (ValueError, TypeError):
        limit = SEARCH_LIMIT
    like = _like(keyword)

    items = []
    for r in (StandaloneResourceModel.query
              .filter(StandaloneResourceModel.name.like(like, escape="\\"))
              .order_by(StandaloneResourceModel.sort_order, StandaloneResourceModel.id)
              .limit(limit).all()):
        items.append({'source': 'standalone', 'id': r.id, 'name': r.name,
                      'size': r.size,
                      'created_at': r.created_at.strftime('%Y-%m-%d %H:%M') if r.created_at else None,
                      'category': r.category, 'tag_label': _category_label(r.category)})

    for r in (db.session.query(CourseResourceModel, CourseModel.title)
              .join(CourseModel, CourseResourceModel.course_id == CourseModel.id)
              .filter(CourseModel.status == CourseModel.STATUS_NORMAL,
                      CourseResourceModel.name.like(like, escape="\\"))
              .order_by(CourseModel.id, CourseResourceModel.sort_order, CourseResourceModel.id)
              .limit(limit).all()):
        items.append({'source': 'course', 'id': r.CourseResourceModel.id,
                      'name': r.CourseResourceModel.name, 'size': r.CourseResourceModel.size,
                      'created_at': r.CourseResourceModel.created_at.strftime('%Y-%m-%d %H:%M')
                                    if r.CourseResourceModel.created_at else None,
                      'course_id': r.CourseResourceModel.course_id,
                      'tag_label': r.title})

    return jsonify({"code": 200, "items": items})


@bp.route("/standalone/<int:rid>/token")
@jwt_required()
def standalone_token(rid):
    """换短签直连（`<a target=_blank>` 带不了 Authorization 头；2h 多次有效，
    为第二期在线预览的多次 Range 请求留路）。登录即可（对齐课程资源口径）。"""
    r = StandaloneResourceModel.query.get(rid)
    if not r:
        return jsonify({"code": 404, "message": "资料不存在"}), 404
    user = _current_user()
    if not user:
        return jsonify({"code": 401, "message": "用户未认证"}), 401
    return media_token_response('resource', rid, user.id)


@bp.route("/standalone/<int:rid>")
def standalone_download(rid):
    """代理下载（存储对象不暴露直链；响应不回 object_key）。双通道鉴权：
    media_sign 短签或常规 JWT，见 media_sign.py。"""
    r = StandaloneResourceModel.query.get(rid)
    if not r:
        return jsonify({"code": 404, "message": "资料不存在"}), 404
    user, auth_err = resolve_media_request('resource', rid)
    if auth_err:
        return auth_err
    try:
        obj = storage.get_object(r.object_key)
    except Exception:
        return jsonify({"code": 500, "message": "资料读取失败（存储服务不可用？）"}), 500
    resp = Response(obj, mimetype=r.content_type or 'application/octet-stream')
    resp.headers["Content-Disposition"] = \
        f"attachment; filename*=UTF-8''{quote(r.name)}"
    return resp


# ── 平台资料·管理端 ──────────────────────────────────────────────────────

def _validate_category(category):
    if category not in CATEGORIES:
        return jsonify({"code": 400, "message": f"category 须为 {'/'.join(CATEGORIES)}"}), 400
    return None


@bp.route("/standalone", methods=["POST"])
@jwt_required()
@audit_log(operation="上传平台资料")
def standalone_create():
    """admin 上传平台资料（multipart：category 必填 + description 选填 + Files[] 多文件）。
    object_key 规则 resources/{category}/{uuid}{ext}；单文件 100MB 上限，超限整体回滚。"""
    err = _require_admin()
    if err:
        return err
    category = (request.form.get("category") or "").strip()
    err = _validate_category(category)
    if err:
        return err
    description = (request.form.get("description") or "").strip() or None
    files = [f for f in request.files.getlist("Files") if f.filename]
    if not files:
        return jsonify({"code": 400, "message": "没有有效文件"}), 400

    user = _current_user()
    base_order = db.session.query(db.func.max(StandaloneResourceModel.sort_order)) \
        .filter_by(category=category).scalar() or 0

    saved = []
    uploaded_keys = []    # 失败时整体回收本批已落盘对象，不留孤儿
    try:
        for f in files:
            ext = os.path.splitext(f.filename)[1].lower()[:20]
            key = f"resources/{category}/{uuid.uuid4().hex}{ext}"
            storage.put_object(key, f.stream, content_type=f.mimetype or 'application/octet-stream')
            uploaded_keys.append(key)
            size = storage.stat_object(key).size
            if size > MAX_FILE_MB * 1024 * 1024:
                for k in uploaded_keys:
                    storage.remove_object(k)     # 幂等，含当前超限文件
                db.session.rollback()
                return jsonify({"code": 400, "message": f"文件 {f.filename} 超过 {MAX_FILE_MB}MB 上限"}), 400
            base_order += 1
            res = StandaloneResourceModel(
                name=f.filename[:200], description=description, category=category,
                object_key=key, size=size, content_type=f.mimetype,
                sort_order=base_order, uploader_id=user.id)
            db.session.add(res)
            saved.append(res)
        db.session.commit()
    except Exception:
        for k in uploaded_keys:
            try:
                storage.remove_object(k)
            except Exception:
                pass    # 回收尽力而为，勿掩盖原始异常
        db.session.rollback()
        return jsonify({"code": 500, "message": "文件上传失败（存储服务不可用？），请稍后重试"}), 500

    return jsonify({"code": 200, "message": "上传成功",
                    "data": [r.to_dict() for r in saved]})


@bp.route("/standalone/<int:rid>", methods=["PUT"])
@jwt_required()
@audit_log(operation="编辑平台资料")
def standalone_update(rid):
    """admin 编辑元数据（name/category/description）。category 变更不动 object_key
    （key 里的分类段只是落盘路径，归属以表为准）。"""
    err = _require_admin()
    if err:
        return err
    r = StandaloneResourceModel.query.get(rid)
    if not r:
        return jsonify({"code": 404, "message": "资料不存在"}), 404
    data = request.get_json(silent=True) or {}

    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"code": 400, "message": "名称不能为空"}), 400
    category = (data.get("category") or "").strip()
    err = _validate_category(category)
    if err:
        return err
    description = data.get("description")
    if description is not None:
        description = str(description).strip() or None

    r.name = name[:200]
    r.category = category
    r.description = description
    db.session.commit()
    return jsonify({"code": 200, "message": "已更新", "data": r.to_dict()})


@bp.route("/standalone/<int:rid>", methods=["DELETE"])
@jwt_required()
@audit_log(operation="删除平台资料")
def standalone_delete(rid):
    """admin 删除资料：对象幂等清理 + 行删除。"""
    err = _require_admin()
    if err:
        return err
    r = StandaloneResourceModel.query.get(rid)
    if not r:
        return jsonify({"code": 404, "message": "资料不存在"}), 404
    storage.remove_object(r.object_key)   # 幂等，对象缺失静默
    db.session.delete(r)
    db.session.commit()
    return jsonify({"code": 200, "message": "已删除"})


@bp.route("/standalone/sort", methods=["POST"])
@jwt_required()
@audit_log(operation="排序平台资料")
def standalone_sort():
    """admin 全量重排（与 course/resource_sort 同口径）：body 传 Resource_Ids
    目标顺序数组（本蓝图全量或分类内全量），按 idx 重写 sort_order。"""
    err = _require_admin()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    ids = data.get("Resource_Ids")
    if not isinstance(ids, list) or not ids:
        return jsonify({"code": 400, "message": "参数错误"}), 400
    try:
        ids = [int(i) for i in ids]
    except (ValueError, TypeError):
        return jsonify({"code": 400, "message": "Resource_Ids 须为整数数组"}), 400

    rows = StandaloneResourceModel.query.filter(
        StandaloneResourceModel.id.in_(ids)).all()
    by_id = {row.id: row for row in rows}
    for idx, rid in enumerate(ids):
        if rid in by_id:
            by_id[rid].sort_order = idx
    db.session.commit()
    return jsonify({"code": 200, "message": "排序已保存"})

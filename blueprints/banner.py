"""首页轮播管理蓝图：banner 表 DB 驱动，图走 storage media/banners/。

- GET /banner/list 公开（首页数据，无 JWT；只回 visible，按 sort_order）。
- 写接口全部 jwt + check_permission('banner_management')（权限由 migrate_32 seed）。
- create 一步到位（FormData 元数据 + image 同传，避免空 image_key 中间态）；
  换图走 /banner/image/update；排序全量覆写（两段式赋值避开 unique 约束）。
- 与 /camp/featured 不耦合：is_camp_frame=1 的帧由前端叠加主推营动态角标。
"""
import io
import uuid

from flask import Blueprint, jsonify, request

from exts import db
from models import BannerModel
from storage import storage
from .media import media_url
import imaging
from . import check_permission, audit_log
from flask_jwt_extended import jwt_required

bp = Blueprint("banner", __name__, url_prefix="/banner")

LINK_TYPES = ("route", "external", "none")


def _row_payload(b):
    return {
        "Banner_Id": b.id,
        "title": b.title,
        "description": b.description,
        "image": b.image_key if b.image_key.startswith("/media/") else media_url(b.image_key),
        "link_type": b.link_type,
        "link_value": b.link_value,
        "is_camp_frame": bool(b.is_camp_frame),
        "visible": bool(b.visible),
        "sort_order": b.sort_order,
    }


@bp.route("/list", methods=["GET"])
def banner_list():
    """公开：首页轮播帧（visible=1 按 sort_order；图字段为 /media 相对路径）。"""
    banners = BannerModel.query.filter_by(visible=True).order_by(BannerModel.sort_order).all()
    return jsonify({"code": 200, "data": [_row_payload(b) for b in banners]})


@bp.route("/admin/list", methods=["GET"])
@jwt_required()
@check_permission('banner_management')
def banner_admin_list():
    """管理端全量列表（含不可见帧，排序编辑用）。"""
    banners = BannerModel.query.order_by(BannerModel.sort_order).all()
    return jsonify({"code": 200, "data": [_row_payload(b) for b in banners]})


@bp.route("/create", methods=["POST"])
@jwt_required()
@check_permission('banner_management')
@audit_log(operation="创建轮播帧")
def banner_create():
    """FormData: title(必)/description/link_type/link_value/is_camp_frame + image(必, jpg/png/webp)"""
    title = (request.form.get("title") or "").strip()
    description = (request.form.get("description") or "").strip()
    link_type = request.form.get("link_type") or "route"
    link_value = (request.form.get("link_value") or "").strip()
    is_camp_frame = request.form.get("is_camp_frame") in ("1", "true", "True")
    file = request.files.get("image")

    if not title:
        return jsonify({"code": 402, "message": "标题不能为空"}), 402
    if link_type not in LINK_TYPES:
        return jsonify({"code": 402, "message": f"link_type 仅支持 {'/'.join(LINK_TYPES)}"}), 402
    if not file or not file.filename:
        return jsonify({"code": 402, "message": "缺少轮播底图 image"}), 402

    try:
        data = imaging.banner_bytes(file.stream)
    except imaging.ImageError as e:
        return jsonify({"code": 400, "message": f"底图无效：{e}"}), 400

    max_sort = db.session.query(db.func.max(BannerModel.sort_order)).scalar() or 0
    b = BannerModel(sort_order=max_sort + 1, title=title, description=description or None,
                    image_key="", link_type=link_type, link_value=link_value or None,
                    is_camp_frame=is_camp_frame, visible=False)   # 建帧默认隐藏，传完图在管理页放开
    db.session.add(b)
    db.session.flush()                                            # 拿 id 组 key

    key = f"media/banners/{b.id}/{uuid.uuid4().hex}.webp"
    try:
        storage.put_object(key, io.BytesIO(data), len(data), "image/webp")
    except Exception as e:
        db.session.rollback()
        return jsonify({"code": 500, "message": f"底图存储失败：{e}"}), 500
    b.image_key = "/" + key
    db.session.commit()
    return jsonify({"code": 200, "message": "创建成功（默认隐藏，请在列表中开启可见）",
                    "data": _row_payload(b)})


@bp.route("/update", methods=["POST"])
@jwt_required()
@check_permission('banner_management')
@audit_log(operation="编辑轮播帧")
def banner_update():
    """JSON: Banner_Id(必) + title/description/link_type/link_value/is_camp_frame/visible 任意组合"""
    data = request.get_json(silent=True) or {}
    bid = data.get("Banner_Id")
    if not bid:
        return jsonify({"code": 402, "message": "缺少 Banner_Id"}), 402
    b = BannerModel.query.get(bid)
    if not b:
        return jsonify({"code": 404, "message": "轮播帧不存在"}), 404

    if "title" in data:
        title = (data.get("title") or "").strip()
        if not title:
            return jsonify({"code": 402, "message": "标题不能为空"}), 402
        b.title = title
    if "description" in data:
        b.description = (data.get("description") or "").strip() or None
    if "link_type" in data:
        if data["link_type"] not in LINK_TYPES:
            return jsonify({"code": 402, "message": f"link_type 仅支持 {'/'.join(LINK_TYPES)}"}), 402
        b.link_type = data["link_type"]
    if "link_value" in data:
        b.link_value = (data.get("link_value") or "").strip() or None
    if "is_camp_frame" in data:
        b.is_camp_frame = bool(data["is_camp_frame"])
    if "visible" in data:
        if data["visible"] and not b.image_key:
            return jsonify({"code": 402, "message": "无底图不能设为可见"}), 402
        b.visible = bool(data["visible"])
    db.session.commit()
    return jsonify({"code": 200, "message": "更新成功", "data": _row_payload(b)})


@bp.route("/image/update", methods=["POST"])
@jwt_required()
@check_permission('banner_management')
@audit_log(operation="更换轮播底图")
def banner_image_update():
    """FormData: Banner_Id + image（转码 1600x800 WebP <=500KB）"""
    bid = request.form.get("Banner_Id")
    file = request.files.get("image")
    if not bid:
        return jsonify({"code": 402, "message": "缺少 Banner_Id"}), 402
    b = BannerModel.query.get(bid)
    if not b:
        return jsonify({"code": 404, "message": "轮播帧不存在"}), 404
    if not file or not file.filename:
        return jsonify({"code": 402, "message": "缺少底图文件 image"}), 402

    try:
        data = imaging.banner_bytes(file.stream)
    except imaging.ImageError as e:
        return jsonify({"code": 400, "message": f"底图无效：{e}"}), 400

    key = f"media/banners/{b.id}/{uuid.uuid4().hex}.webp"
    try:
        storage.put_object(key, io.BytesIO(data), len(data), "image/webp")
    except Exception as e:
        return jsonify({"code": 500, "message": f"底图存储失败：{e}"}), 500
    old_key = b.image_key.lstrip("/")
    b.image_key = "/" + key
    db.session.commit()
    if old_key.startswith("media/"):
        try:
            storage.remove_object(old_key)                       # 旧图清理，best-effort
        except Exception:
            pass
    return jsonify({"code": 200, "message": "底图已更新", "data": _row_payload(b)})


@bp.route("/delete", methods=["POST"])
@jwt_required()
@check_permission('banner_management')
@audit_log(operation="删除轮播帧")
def banner_delete():
    """JSON: Banner_Id；删行 + 删底图对象 + sort_order 补洞"""
    data = request.get_json(silent=True) or {}
    bid = data.get("Banner_Id")
    if not bid:
        return jsonify({"code": 402, "message": "缺少 Banner_Id"}), 402
    b = BannerModel.query.get(bid)
    if not b:
        return jsonify({"code": 404, "message": "轮播帧不存在"}), 404

    old_key = b.image_key.lstrip("/")
    db.session.delete(b)
    db.session.commit()
    if old_key.startswith("media/"):
        try:
            storage.remove_object(old_key)
        except Exception:
            pass
    # 补洞：剩余帧 sort_order 重排为 1..n
    remaining = BannerModel.query.order_by(BannerModel.sort_order).all()
    for idx, r in enumerate(remaining, start=1):
        r.sort_order = idx + 1_000_000                             # 先让位避开 unique
    db.session.flush()
    for idx, r in enumerate(remaining, start=1):
        r.sort_order = idx
    db.session.commit()
    return jsonify({"code": 200, "message": "已删除"})


@bp.route("/sort", methods=["POST"])
@jwt_required()
@check_permission('banner_management')
@audit_log(operation="排序轮播帧")
def banner_sort():
    """JSON: {"Banner_Ids": [...]} 全量有序（范式同 /course/resource_sort，两段式避开 unique）"""
    data = request.get_json(silent=True) or {}
    ids = data.get("Banner_Ids")
    if not isinstance(ids, list) or not ids:
        return jsonify({"code": 402, "message": "参数错误"}), 402
    banners = BannerModel.query.all()
    by_id = {b.id: b for b in banners}
    if any(i not in by_id for i in ids):
        return jsonify({"code": 404, "message": "Banner_Ids 含无效 id"}), 404

    for idx, bid in enumerate(ids):
        by_id[bid].sort_order = idx + 1_000_000
    db.session.flush()
    for idx, bid in enumerate(ids):
        by_id[bid].sort_order = idx + 1
    db.session.commit()
    return jsonify({"code": 200, "message": "排序成功"})

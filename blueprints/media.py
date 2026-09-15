"""公开媒体蓝图：只读直出 storage 中 media/ 命名空间下的图片。

设计口径（与私有附件 courses/、camp/ 物理隔离，防鉴权绕过）：
- 公开媒体 object key 一律以 media/ 开头，且必须命中 PUBLIC_MEDIA_PREFIXES 白名单；
  白名单外的 key（含私有附件前缀）即使拼进 URL 也只回 404（不回 403，避免探测信号）。
- key 含 uuid（勋章 Default 等稳定 key 除外），内容不可变 -> immutable 强缓存 + ETag 304。
- 无 JWT：img 标签带不了 token，与 /data/avatars、/camp/ms/photo 同口径（key 不可猜）。
"""
import hashlib
import os

from flask import Blueprint, Response, jsonify, request

from storage import storage

bp = Blueprint("media", __name__, url_prefix="/media")

# 公开前缀白名单：新增公开媒体类别时在此登记
PUBLIC_MEDIA_PREFIXES = (
    "media/avatars/",
    "media/mentors/",
    "media/medals/",
    "media/banners/",
    "media/course-covers/",
)

EXT_CONTENT_TYPES = {
    ".webp": "image/webp",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
}


def media_url(object_key):
    """object key -> '/media/...' 相对 URL；非 media/ 前缀返回 None。
    全站 DB 一律存这个相对 URL（不带域名），前端用 assetUrl() 拼前缀。"""
    if object_key and object_key.startswith("media/"):
        return "/" + object_key
    return None


def public_avatar_url(avatar_url):
    """UserModel.avatar_url -> 展示 URL（统一收口，替代散落各蓝图的同名拷贝）。
    - 新值 '/media/...'：原样返回
    - 旧值裸文件名 '{uid}.{ext}'：回退 '/data/avatars/{name}'（Flask static 兜底一版，下版删）
    - 空 / 外链 http(s)：原样返回"""
    if not avatar_url:
        return ""
    if avatar_url.startswith("/media/") or avatar_url.startswith("http"):
        return avatar_url
    if avatar_url.startswith("/"):
        return avatar_url
    return "/data/avatars/" + avatar_url


@bp.route("/<path:key>", methods=["GET"])
def media_get(key):
    # 1) 形态校验：路径段禁 ..，扩展名必须已知
    object_key = "media/" + key.lstrip("/")
    segments = object_key.split("/")
    if ".." in segments or segments[0] != "media":
        return jsonify({"code": 404, "message": "not found"}), 404
    ext = os.path.splitext(object_key)[1].lower()
    if ext not in EXT_CONTENT_TYPES:
        return jsonify({"code": 404, "message": "not found"}), 404

    # 2) 白名单：私有附件前缀（courses/、camp/...）从这里永远拿不到
    if not any(object_key.startswith(p) for p in PUBLIC_MEDIA_PREFIXES):
        return jsonify({"code": 404, "message": "not found"}), 404

    # 3) 存在性（stat 兼容两后端：不存在抛 FileNotFoundError / S3Error）
    try:
        st = storage.stat_object(object_key)
    except Exception:
        return jsonify({"code": 404, "message": "not found"}), 404
    size = getattr(st, "size", 0) or 0

    # 4) 条件请求：内容不可变，ETag 用 key+size 摘要即可
    etag = hashlib.sha1(f"{object_key}:{size}".encode()).hexdigest()
    if request.if_none_match.contains(etag):
        resp = Response(status=304)
        resp.set_etag(etag)
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return resp

    # 5) 整读直出（图片 <=1MB 量级，规避 minio urllib3 流生命周期坑）
    obj = None
    try:
        obj = storage.get_object(object_key)
        body = obj.read()
    except Exception:
        return jsonify({"code": 404, "message": "not found"}), 404
    finally:
        if obj is not None:
            try:
                obj.close()
            except Exception:
                pass
            try:
                obj.release_conn()
            except Exception:
                pass

    resp = Response(body, mimetype=EXT_CONTENT_TYPES[ext])
    resp.set_etag(etag)
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp

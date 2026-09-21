"""媒体直连短签（2026-09-17）：附件代理端点共用机制。

`<a>/<video>` 带不了 Authorization 头，裸链一律 401（章节材料/交付链的历史遗留缺陷）；
课程资源 Down_Code 是一次性码，视频播放的多次 Range 请求不可用。统一机制：
JWT 先换 HMAC 短签直连（短时多次有效，默认 2h），下载端点双通道鉴权（短签或 JWT 任一）。

签名绑定 (kind, 对象 id, 用户 id, 过期秒)：kind 防跨功能重放（不同功能的附件 id
撞号时签名互不可用）；uid 在服务端重查（封禁即拒），具体资源权限由各端点在解析出
用户后照常复查。
"""
import hashlib
import hmac
import time

from flask import current_app, jsonify, request
from flask_jwt_extended import verify_jwt_in_request

MEDIA_TOKEN_TTL = 2 * 60 * 60   # 短签有效期（对齐 access token 2h）

# kind → 代理下载端点路径前缀（token 回包拼直连相对 URL 用；各链独立端点）
MEDIA_PATHS = {
    'meeting': '/camp/meetings/attachments',
    'material': '/camp/materials/attachments',
    'submission': '/camp/submissions/attachments',
    'meeting_task': '/camp/meetings/task-attachments',   # 组会任务附件（详情回包内嵌直链）
    # meeting_zip：组会提交打包（路径含后缀，调 media_signed_url 时显式传 path）
    'meeting_zip': '/camp/meetings',
    'resource': '/resources/standalone',                  # 学习资源中心·平台资料
    'feedback_ticket': '/feedback-tickets/attachments',   # 用户反馈工单附件
}


def sign_media_token(kind, oid, uid, exp):
    msg = f"{kind}:{oid}:{uid}:{exp}".encode()
    return hmac.new(current_app.config["JWT_SECRET_KEY"].encode(),
                    msg, hashlib.sha256).hexdigest()


def media_signed_url(kind, oid, uid, path=None):
    """生成短签直连相对 URL（media_token_response 与详情回包内嵌直链共用）。
    path 显式给出时用完整路径（如 zip 端点带后缀），缺省按 kind 拼 {前缀}/{oid}。"""
    exp = int(time.time()) + MEDIA_TOKEN_TTL
    st = sign_media_token(kind, oid, uid, exp)
    base = path if path is not None else f"{MEDIA_PATHS[kind]}/{oid}"
    return f"{base}?u={uid}&e={exp}&st={st}"


def media_token_response(kind, oid, uid, path=None):
    """各 /token 端点统一回包：短签直连相对 URL（前端拼 API_URL）。path 缺省按 kind 拼标准端点。"""
    return jsonify({"code": 200, "expires_in": MEDIA_TOKEN_TTL,
                    "url": media_signed_url(kind, oid, uid, path)})


def resolve_media_request(kind, oid):
    """附件下载端点双通道鉴权：?u=&e=&st= 短签或常规 JWT。
    返回 (user, err_resp)；err_resp 是 (response, status) 元组，调用方直接 return。"""
    if request.args.get("st") or request.args.get("u") or request.args.get("e"):
        try:
            uid, exp = int(request.args["u"]), int(request.args["e"])
        except (KeyError, TypeError, ValueError):
            return None, (jsonify({"code": 403, "message": "附件签名无效"}), 403)
        if (exp < time.time()
                or not hmac.compare_digest(sign_media_token(kind, oid, uid, exp),
                                           request.args.get("st") or "")):
            return None, (jsonify({"code": 403, "message": "附件链接已过期，请刷新后重试"}), 403)
        from models import UserModel
        user = UserModel.query.get(uid)
        if not user or (user.status or 'active') == 'banned':
            return None, (jsonify({"code": 403, "message": "附件签名无效"}), 403)
        return user, None
    try:
        verify_jwt_in_request()
    except Exception:
        return None, (jsonify({"code": 401, "message": "未提供访问令牌"}), 401)
    from . import _current_user
    user = _current_user()
    if not user:
        return None, (jsonify({"code": 401, "message": "用户未认证"}), 401)
    return user, None

"""媒体直连短签（2026-09-17 引入；D1 安全地基升 v2）。

`<a>/<video>` 带不了 Authorization 头，裸链一律 401（章节材料/交付链的历史遗留缺陷）；
课程资源 Down_Code 是一次性码，视频播放的多次 Range 请求不可用。统一机制：
JWT 先换 HMAC 短签直连（短时多次有效，默认 2h），下载端点双通道鉴权（短签或 JWT 任一）。

签名绑定 (kind, 对象 id, 用户 id, 过期秒[, 安全版本])：kind 防跨功能重放；uid 在
服务端重查；具体资源权限由各端点在解析出用户后照常复查。

v2（D1，规格 6.4）：签名另带账号 security_version 并用独立密钥 MEDIA_SIGN_SECRET；
校验实时比对当前版本——重置密码/封禁/（D2 起）归并后旧短签立即失效。v1 旧签名
仅在「账号未置位 require_versioned_tokens 且未过 legacy 截止」时兼容（短签 TTL 2h
自然消亡，归并账号由置位语义提前拒绝）。
"""
import hashlib
import hmac
import time

from flask import current_app, jsonify, request
from flask_jwt_extended import verify_jwt_in_request

import config
from services.auth_context import AuthRejected, validate_account_lifecycle

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

_MEDIA_SECRET = None


def _media_secret():
    """独立媒体密钥（config.MEDIA_SIGN_SECRET，未设回退 JWT_SECRET_KEY 并告警）。"""
    global _MEDIA_SECRET
    if _MEDIA_SECRET is None:
        _MEDIA_SECRET = (config.MEDIA_SIGN_SECRET or current_app.config["JWT_SECRET_KEY"]).encode()
    return _MEDIA_SECRET


def sign_media_token(kind, oid, uid, exp, sv=None):
    """v2 签名绑定安全版本；sv=None 时签 v1 形态（兼容窗口内旧校验方仍可验）。"""
    if sv is None:
        msg = f"{kind}:{oid}:{uid}:{exp}".encode()
    else:
        msg = f"v2:{kind}:{oid}:{uid}:{sv}:{exp}".encode()
    return hmac.new(_media_secret(), msg, hashlib.sha256).hexdigest()


def media_signed_url(kind, oid, uid, path=None):
    """生成短签直连相对 URL（media_token_response 与详情回包内嵌直链共用）。

    D1 起签 v2：内部按 uid 查账号安全版本（签发低频，一次主键查询可接受），
    调用方（5 个业务文件）零改动。
    """
    from models import UserModel
    user = UserModel.query.get(uid)
    sv = (user.security_version or 0) if user else 0
    exp = int(time.time()) + MEDIA_TOKEN_TTL
    st = sign_media_token(kind, oid, uid, exp, sv=sv)
    base = path if path is not None else f"{MEDIA_PATHS[kind]}/{oid}"
    return f"{base}?u={uid}&e={exp}&sv={sv}&st={st}"


def media_token_response(kind, oid, uid, path=None):
    """各 /token 端点统一回包：短签直连相对 URL（前端拼 API_URL）。path 缺省按 kind 拼标准端点。"""
    return jsonify({"code": 200, "expires_in": MEDIA_TOKEN_TTL,
                    "url": media_signed_url(kind, oid, uid, path)})


def _v1_window_open(user):
    from datetime import datetime
    return (not user.require_versioned_tokens
            and datetime.now() <= config.AUTH_LEGACY_TOKEN_DEADLINE)


def resolve_media_request(kind, oid):
    """附件下载端点双通道鉴权：?u=&e=&sv=&st= 短签或常规 JWT。
    返回 (user, err_resp)；err_resp 是 (response, status) 元组，调用方直接 return。"""
    if request.args.get("st") or request.args.get("u") or request.args.get("e"):
        try:
            uid, exp = int(request.args["u"]), int(request.args["e"])
        except (KeyError, TypeError, ValueError):
            return None, (jsonify({"code": 403, "message": "附件签名无效"}), 403)
        from models import UserModel
        user = UserModel.query.get(uid)
        if not user:
            return None, (jsonify({"code": 403, "message": "附件签名无效"}), 403)
        # 账号状态实时校验（v2 起含生命周期，不再只查 banned）
        try:
            validate_account_lifecycle(user)
        except AuthRejected as exc:
            return None, (jsonify({"code": exc.status, "message": exc.message}), exc.status)

        sv_arg = request.args.get("sv")
        if sv_arg is not None:
            # v2 签名：版本漂移（重置密码/封禁 bump/后续归并）即拒
            try:
                sv = int(sv_arg)
            except ValueError:
                return None, (jsonify({"code": 403, "message": "附件签名无效"}), 403)
            expected = sign_media_token(kind, oid, uid, exp, sv=sv)
            if (exp < time.time()
                    or sv != (user.security_version or 0)
                    or not hmac.compare_digest(expected, request.args.get("st") or "")):
                return None, (jsonify({"code": 403, "message": "附件链接已失效，请刷新后重试"}), 403)
        else:
            # v1 旧签名：仅兼容窗口内且账号未置位；置位账号（已发生安全变更）立即拒绝
            if not _v1_window_open(user):
                return None, (jsonify({"code": 403, "message": "附件链接已失效，请刷新后重试"}), 403)
            if (exp < time.time()
                    or not hmac.compare_digest(sign_media_token(kind, oid, uid, exp),
                                               request.args.get("st") or "")):
                return None, (jsonify({"code": 403, "message": "附件链接已过期，请刷新后重试"}), 403)
        return user, None
    try:
        verify_jwt_in_request()
    except Exception:
        return None, (jsonify({"code": 401, "message": "未提供访问令牌"}), 401)
    from services.auth_context import current_actor
    actor = current_actor()
    if not actor:
        return None, (jsonify({"code": 401, "message": "用户未认证"}), 401)
    return actor.user, None

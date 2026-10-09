"""积分商城 SSO：登录态下发 90 秒一次性 ticket。

流程：校验登录/账号状态/开关/校园邮箱 → 三层限流 → ensure 绑定 → 申请 ticket，
前端拿到后以顶层表单 POST 到商城 /sso/callback（见外部接入接口文档 v1.2.0）。

设计要点（对应规划修订版 §4.2）：
- 错误映射：上游认证/契约类错误一律转本平台 502，绝不透传 401（会误触发
  前端静默续期/登出）；仅绑定冲突保留 409 语义。
- 限流在 JWT 验证、用户查明之后进行（视图内上下文管理器，非装饰器）：
  用户桶按 user.id（校园网共享出口 IP，按 IP 限用户会互相挤占）、IP 桶
  防入口刷量、总量桶护平台级负载；存储故障按服务不可用处理，不放行。
- 响应一律 Cache-Control: no-store（ticket 短时效敏感数据不落缓存）。
- 日志只记 request_id / 用户 / 阶段 / 上游状态与错误类别 / 耗时，
  不记 ticket（含前缀）、JWT、签名头与完整请求响应体。
"""
import time
import uuid

from flask import Blueprint, current_app, jsonify
from flask_jwt_extended import jwt_required
from redis.exceptions import RedisError

import points_center_client
from blueprints import _current_user
from exts import limiter
from services.points_identity import resolve_points_identity

try:
    from flask_limiter import RateLimitExceeded
except ImportError:  # 兼容包结构差异
    from flask_limiter.errors import RateLimitExceeded

try:
    from limits.errors import StorageError
except ImportError:  # 兼容包结构差异
    class StorageError(Exception):
        pass


bp = Blueprint("points_sso", __name__, url_prefix="/points-sso")

# 三层限流（固定窗口，与全站 limiter 同策略；键空间互相独立）
USER_BUCKET = "10/minute"     # 单用户：服务器查明的 user.id，与 jti 无关，续期不重置
IP_BUCKET = "300/minute"      # 入口防刷：按客户端 IP（校园网共享出口，额度放宽）
TOTAL_BUCKET = "20/second"    # 平台级总量：所有 worker 经 Redis 共桶（正常请求=2 次上游调用）


def _resp(code, message, error_code=None, headers=None, **extra):
    """统一响应构造：顶层字段平铺（沿用仓库惯例，不加 data 包装）+ no-store。"""
    payload = {"code": code, "message": message}
    if error_code:
        payload["error_code"] = error_code
    payload.update(extra)
    resp = jsonify(payload)
    resp.status_code = code
    resp.headers["Cache-Control"] = "no-store"
    for k, v in (headers or {}).items():
        resp.headers[k] = v
    return resp, code


def _retry_after_seconds(exc):
    """从 RateLimitExceeded 取可信的重置时间（limits 库 Limit.reset_at）。"""
    reset_at = getattr(getattr(exc, "limit", None), "reset_at", None)
    if reset_at:
        try:
            return max(1, int(float(reset_at) - time.time()))
        except (TypeError, ValueError):
            return None
    return None


def _upstream_error_response(request_id, user_id, e):
    """PointsCenterError → 本平台响应。上游 401/403/404 等属于接入/契约问题，
    映射为 502 而非 401/403，避免误伤本平台会话。"""
    category = e.category
    status = e.status_code
    if category == "HTTP":
        if status == 409:
            return _resp(409, "该账号在积分中心已绑定其他邮箱，请联系管理员处理",
                         error_code="POINTS_BINDING_CONFLICT")
        if status == 429:
            headers = {}
            if e.retry_after:
                headers["Retry-After"] = str(e.retry_after)
            return _resp(429, "积分商城繁忙，请稍后重试",
                         error_code="POINTS_UPSTREAM_BUSY", headers=headers or None)
        if status in (400, 401, 403, 404):
            return _resp(502, "积分商城接入异常，请稍后重试",
                         error_code="POINTS_INTEGRATION_ERROR")
        return _resp(502, "积分商城连接异常，请稍后重试",
                     error_code="POINTS_UPSTREAM_ERROR")
    if category == "TIMEOUT":
        return _resp(504, "积分商城响应超时，请稍后重试",
                     error_code="POINTS_UPSTREAM_TIMEOUT")
    if category == "CONFIG":
        return _resp(503, "积分商城暂未开放", error_code="POINTS_DISABLED")
    if category in ("NETWORK", "RESPONSE"):
        return _resp(502, "积分商城连接异常，请稍后重试",
                     error_code="POINTS_UPSTREAM_ERROR")
    # REQUEST / CONTRACT：本地侧或契约问题
    return _resp(502, "积分商城接入异常，请稍后重试",
                 error_code="POINTS_INTEGRATION_ERROR")


@bp.route("/eligibility", methods=["GET"])
@jwt_required()
def eligibility():
    """只读本平台资格；不创建积分绑定，不暴露核验邮箱，不签发票据。"""
    user = _current_user()
    if user is None:
        return _resp(401, "登录状态异常，请重新登录", error_code="AUTH_USER_MISSING")
    identity = resolve_points_identity(user)
    return _resp(200, identity.message, eligible=identity.eligible,
                 source=identity.source, reason=identity.error_code,
                 enabled=bool(current_app.config.get("POINTS_CENTER_ENABLED")))


@bp.route("/ticket", methods=["POST"])
@jwt_required()
def create_ticket():
    request_id = uuid.uuid4().hex[:12]
    start = time.monotonic()

    # ① 身份：JWT 已由装饰器验证；显式复核用户存在与账号状态（便于隔离测试，
    #    与全站 before_request 封禁检查共用同一 user 查询语义）
    user = _current_user()
    if user is None:
        return _resp(401, "登录状态异常，请重新登录", error_code="AUTH_USER_MISSING")
    if (user.status or "active") == "banned":
        return _resp(403, "账号已被封禁，请联系管理员", error_code="ACCOUNT_BANNED")

    # ② 开关
    if not current_app.config.get("POINTS_CENTER_ENABLED"):
        return _resp(503, "积分商城暂未开放", error_code="POINTS_DISABLED")

    # ③ 后端实时解析登录教育邮箱或正式核验邮箱，不接受浏览器指定身份。
    identity = resolve_points_identity(user)
    if not identity.eligible:
        current_app.logger.info(
            f"[points_sso] rid={request_id} uid={user.id} rejected={identity.error_code}")
        return _resp(identity.status, identity.message, error_code=identity.error_code)
    email = identity.email

    # 身份既定，生成对外的平台用户标识（与登录响应 User_Id 一致，绑定后不可改）
    platform_user_id = str(user.id).zfill(7)

    # ④ 三层限流（身份验证完成后进入）+ ⑤/⑥ ensure → ticket 串行
    try:
        with limiter.limit(USER_BUCKET, key_func=lambda: f"points_sso:user:{user.id}",
                           scope="points_sso_user"), \
             limiter.limit(IP_BUCKET, scope="points_sso_ip"), \
             limiter.limit(TOTAL_BUCKET, key_func=lambda: "points_sso:total",
                           scope="points_sso_total"):
            points_center_client.ensure_user(email, platform_user_id)
            ticket, expires_in = points_center_client.create_sso_ticket(platform_user_id)
    except RateLimitExceeded as e:
        headers = {}
        retry_after = _retry_after_seconds(e)
        if retry_after:
            headers["Retry-After"] = str(retry_after)
        current_app.logger.info(
            f"[points_sso] rid={request_id} uid={user.id} rate_limited retry_after={retry_after}")
        return _resp(429, "操作频繁或商城繁忙，请稍后重试",
                     error_code="RATE_LIMITED", headers=headers or None)
    except (StorageError, RedisError):
        # 限流存储故障 = 本路由服务不可用：不静默撤掉保护，也不调用上游
        current_app.logger.exception(
            f"[points_sso] rid={request_id} uid={user.id} limiter_storage_error")
        return _resp(503, "服务暂时不可用，请稍后重试", error_code="LIMITER_UNAVAILABLE")
    except points_center_client.PointsCenterError as e:
        detail = str(e.detail)[:200] if e.detail else None
        current_app.logger.error(
            f"[points_sso] rid={request_id} uid={user.id} upstream_error "
            f"category={e.category} status={e.status_code} cost={int((time.monotonic()-start)*1000)}ms "
            f"msg={e.message} detail={detail}")
        return _upstream_error_response(request_id, user.id, e)

    current_app.logger.info(
        f"[points_sso] rid={request_id} uid={user.id} issued "
        f"expires_in={expires_in} cost={int((time.monotonic()-start)*1000)}ms")
    # ⑥ 回调地址只来自经过初始化校验的服务端配置，路由不接受浏览器指定跳转目标
    return _resp(200, "ok",
                 ticket=ticket,
                 expires_in=expires_in,
                 store_callback_url=current_app.config.get("POINTS_STORE_CALLBACK_URL"))

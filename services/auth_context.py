"""统一账号解析与会话校验（D1 安全地基）。

所有认证入口的唯一收口：request_guard（before_request）、blueprints._current_user
（check_permission/camp_role 底座）、登录/续期/登出/媒体下载共用同一套规则。

token 双协议（规格 6.3，首期主体仍是 email）：
  v2     带声明 sid/security_version/token_schema="v2"（refresh 另带 gen）——
         每请求核对 auth_session（存在/未撤/未过期/版本三处一致）。
  legacy 部署前野外的旧 token（无声明）——仅当账号未置位 require_versioned_tokens
         且未过 AUTH_LEGACY_TOKEN_DEADLINE 时放行，amr=unknown（不可做近期认证操作）。

错误契约（前端按 machine 细分，规格第 11 章）：
  401 TOKEN_SCHEMA_REQUIRED / SESSION_REVOKED / SESSION_EXPIRED / INVALID_TOKEN / ACCOUNT_MISSING
  401 ACCOUNT_MERGED（merged 账号停普通登录，保留专用通道）
  403 ACCOUNT_BANNED / ACCOUNT_DISABLED / REAUTH_REQUIRED / MFA_REQUIRED
"""
import hmac as _hmac
import hashlib
from datetime import datetime

import config
from flask import jsonify, request
from flask_jwt_extended import get_jwt, get_jwt_identity, verify_jwt_in_request

from models import AuthSessionModel, UserModel

# 挑战摘要密钥（验证码/恢复码/refresh 摘要共用；专用密钥优先，未设由 JWT_SECRET 派生）
CHALLENGE_HMAC_SECRET = (
    config.CHALLENGE_HMAC_SECRET.encode()
    if config.CHALLENGE_HMAC_SECRET
    else _hmac.new(config.JWT_SECRET_KEY.encode(), b"challenge-hmac-v1", hashlib.sha256).digest()
)

# terminal machine 集合：前端收到即终止该端会话（不触发自动续期）
TERMINAL_MACHINES = {"SESSION_REVOKED", "SESSION_EXPIRED", "ACCOUNT_MERGED", "ACCOUNT_DISABLED"}


def challenge_digest(value: str) -> str:
    """验证码/恢复码/refresh 等一次性凭据的 HMAC-SHA256 摘要（专用密钥）。"""
    return _hmac.new(CHALLENGE_HMAC_SECRET, value.encode(), hashlib.sha256).hexdigest()


def constant_time_eq(a: str, b: str) -> bool:
    return _hmac.compare_digest(a, b)


class AuthRejected(Exception):
    """统一拒绝信号：guard 直接转响应；端点内捕获后自行决定处理。"""

    def __init__(self, message, *, status, machine):
        super().__init__(message)
        self.message = message
        self.status = status
        self.machine = machine

    def to_response(self):
        return (
            jsonify({"code": self.status, "message": self.message, "machine": self.machine}),
            self.status,
        )


class ActorContext:
    """当前请求的账号上下文（flask.g 缓存，单请求一次解析）。"""

    __slots__ = ("user", "session", "claims", "is_legacy")

    def __init__(self, user, session, claims, is_legacy):
        self.user = user
        self.session = session
        self.claims = claims
        self.is_legacy = is_legacy

    @property
    def auth_time(self):
        return None if self.is_legacy else self.session.auth_time

    @property
    def amr(self):
        return "unknown" if self.is_legacy else (self.session.amr or "unknown")

    def has_factor(self, factor):
        return factor in (self.amr or "").split("+")


def validate_account_lifecycle(user):
    """banned(status) 优先于 lifecycle；merged 停普通登录；disabled 不自动恢复。"""
    if (user.status or "active") == "banned":
        raise AuthRejected("账号已被封禁，请联系管理员", status=403, machine="ACCOUNT_BANNED")
    lifecycle = user.lifecycle or "active"
    if lifecycle == "merged":
        raise AuthRejected("该账号已合并到其他账号，请使用存续账号登录", status=401, machine="ACCOUNT_MERGED")
    if lifecycle == "disabled":
        raise AuthRejected("该账号已停用", status=403, machine="ACCOUNT_DISABLED")


def validate_session(user, claims):
    """v2 token 的会话校验：sid 存在、归属正确、未撤未过期、版本三处一致。"""
    sid = claims.get("sid")
    if not sid:
        raise AuthRejected("令牌缺少会话声明，请重新登录", status=401, machine="TOKEN_SCHEMA_REQUIRED")
    session = db_session_get(sid)
    if session is None:
        raise AuthRejected("会话不存在或已失效，请重新登录", status=401, machine="SESSION_REVOKED")
    if session.user_id != user.id:
        raise AuthRejected("会话与账号不匹配", status=401, machine="SESSION_REVOKED")
    if session.revoked_at is not None:
        raise AuthRejected("登录已失效，请重新登录", status=401, machine="SESSION_REVOKED")
    if session.expires_at <= datetime.now():
        raise AuthRejected("会话已过期，请重新登录", status=401, machine="SESSION_EXPIRED")
    token_sv = claims.get("security_version")
    if token_sv != session.security_version or session.security_version != (user.security_version or 0):
        raise AuthRejected("账号安全状态已变更，请重新登录", status=401, machine="SESSION_REVOKED")
    return session


def db_session_get(sid):
    from exts import db
    return db.session.get(AuthSessionModel, sid)


def _legacy_window_open(user):
    if user.require_versioned_tokens:
        return False
    return datetime.now() <= config.AUTH_LEGACY_TOKEN_DEADLINE


def _resolve() -> ActorContext:
    try:
        verify_jwt_in_request()
    except Exception:
        # flask_jwt_extended 的四类错误在 app.py 已统一 401；走到这里说明不在其处理器
        # 覆盖范围（如 before_request 手动 verify），按无效令牌拒绝
        raise AuthRejected("访问令牌无效", status=401, machine="INVALID_TOKEN")
    identity = get_jwt_identity()
    user = UserModel.query.filter_by(email=identity).first()
    if not user:
        raise AuthRejected("账号不存在", status=401, machine="ACCOUNT_MISSING")
    validate_account_lifecycle(user)
    claims = get_jwt()
    if claims.get("token_schema") == "v2" and claims.get("sid"):
        session = validate_session(user, claims)
        return ActorContext(user, session, claims, is_legacy=False)
    # legacy 协议（无 sid/版本声明）：窗口内且未置位才放行
    if not _legacy_window_open(user):
        raise AuthRejected("登录方式已升级，请重新登录", status=401, machine="TOKEN_SCHEMA_REQUIRED")
    return ActorContext(user, None, claims, is_legacy=True)


def resolve_actor(*, required=True):
    """解析当前请求账号；结果（含拒绝）缓存于当前请求对象，同请求只解析一次。

    缓存必须挂请求不挂 flask.g：Flask 在已有 app context 时（本仓 unittest 的
    setUp 压 context + test_client 组合）请求会复用该 context，挂 g 会把上一个
    请求的账号泄漏给下一个请求。

    required=False 时解析失败返回 None（供可选登录的公开路径）；
    失败在 required=True 下抛 AuthRejected。
    """
    cached = getattr(request, "_identity_actor", None)
    if cached is not None:
        return cached
    if getattr(request, "_identity_resolved", False):
        # 本请求已解析过且失败
        if required:
            raise request._identity_rejected
        return None
    try:
        actor = _resolve()
        request._identity_actor = actor
        return actor
    except AuthRejected as exc:
        request._identity_resolved = True
        request._identity_rejected = exc
        if required:
            raise
        return None


def current_actor():
    """已解析则复用；未解析则懒解析（失败返回 None，不抛）。"""
    return resolve_actor(required=False)


def require_recent_auth(actor, *, max_age_seconds=None, factors=None):
    """高风险操作的前置：实际认证需在窗口内，且 amr 含全部指定因子。

    legacy 会话（amr=unknown）恒不通过——旧 token 不携带可信认证时间，
    不用 iat/刷新时间推导（规格 6.2）。
    """
    window = max_age_seconds if max_age_seconds is not None else config.AUTH_RECENT_AUTH_SECONDS
    if actor is None or actor.is_legacy or actor.auth_time is None:
        raise AuthRejected("该操作需要重新认证", status=403, machine="REAUTH_REQUIRED")
    if (datetime.now() - actor.auth_time).total_seconds() > window:
        raise AuthRejected("认证已过期，请重新认证后再操作", status=403, machine="REAUTH_REQUIRED")
    if factors:
        missing = [f for f in factors if not actor.has_factor(f)]
        if missing:
            raise AuthRejected("该操作需要更强的认证因素", status=403, machine="MFA_REQUIRED")

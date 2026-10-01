"""全站账户状态、会话校验与管理端路径的入口守卫（D1 安全地基重写）。

此前 /auth/* 整段跳过且只查 banned——现为按端点分类（规格 6.1）：
  公开认证端点（登录/注册/验证码/找回/dev 面板/会话配置）：跳过，端点自查；
  续期与退出（新旧 refresh/logout）：跳过通用守卫（refresh 不是 access token，
    verify 会误判），端点内走 auth_sessions 全量校验；
  其余全部路径（含受限 /auth/*）：带 Bearer 即严格校验（解析+生命周期+会话版本），
    无 Bearer 放行（公开业务由各端点装饰器自治；管理端仍要求持 token）。

会话/安全版本的实时真相源在数据库（auth_session + user.security_version），
Redis blocklist 只是补充——这里不再依赖它做准入判断。
"""
import config
from sqlalchemy.exc import OperationalError

from flask import jsonify, request

# 公开认证端点（端点内部自行限流/自查）
PUBLIC_AUTH_PATHS = {
    "/auth/login",
    "/auth/admin_login",
    "/auth/login/mfa",
    "/auth/admin_login/mfa",
    "/auth/register",
    "/auth/captcha/email",
    "/auth/find_password",
    "/auth/dev_accounts",
    "/auth/session/config",
}
# 续期/退出端点：不跑本守卫的 access 校验，端点内做全量校验（含 CSRF）
REFRESH_LOGOUT_PATHS = {
    "/auth/refresh",
    "/auth/logout",
    "/auth/user/refresh",
    "/auth/user/logout",
    "/auth/admin/refresh",
    "/auth/admin/logout",
}


def enforce_request_access():
    """每次请求：分类跳过或统一解析；管理端路径只接受平台管理员。"""
    if request.method == "OPTIONS":
        return None
    path = request.path
    if path in PUBLIC_AUTH_PATHS or path in REFRESH_LOGOUT_PATHS:
        return None

    is_admin_api = path.startswith("/admin/")
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        # 公开业务无 token 放行（装饰器自治）；管理端必须持 token
        return (jsonify({"code": 401, "message": "未提供访问令牌"}), 401) if is_admin_api else None

    from services.auth_context import AuthRejected, resolve_actor

    try:
        actor = resolve_actor(required=True)
    except AuthRejected as exc:
        # 带了 Bearer 就严格校验：无效/过期/被撤一律明确拒绝（旧版对非管理端静默放行）
        return exc.to_response()
    except OperationalError:
        # DB 不可用：受保护操作 503，不用缓存授权兜底（规格 6.2）
        return jsonify({"code": 503, "message": "服务暂不可用，请稍后重试",
                        "machine": "SERVICE_UNAVAILABLE"}), 503

    if is_admin_api and not actor.user.is_admin():
        return jsonify({"code": 403, "message": "无管理端访问权限"}), 403
    # D1 管理端 MFA 强制开关（默认关）：/admin/* 会话须含 totp 因子；
    # legacy 会话 amr=unknown 不含 totp，开关开启时同样被拦（不能绕过，规格 6.5）
    if is_admin_api and config.MFA_ENFORCE_FOR_ADMIN and not actor.has_factor("totp"):
        return jsonify({"code": 403, "message": "请先完成多因素认证后再访问管理端",
                        "machine": "MFA_REQUIRED"}), 403
    return None

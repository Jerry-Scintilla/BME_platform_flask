from flask import Blueprint, render_template, request, redirect

from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
# import app

from .forms import RegisterForm, LoginForm
from models import UserModel
from exts import db, mail, redis_client, limiter, revoke_token
from flask import jsonify
from flask_jwt_extended import (
    JWTManager, create_access_token, create_refresh_token,
    jwt_required, get_jwt_identity, get_jwt, decode_token,
)
from datetime import datetime, timezone

from flask_mail import Message
import hmac
import os
import secrets

import config
from models import AuditLog
from services import auth_mfa, auth_sessions
from services.auth_challenges import issue_captcha, verify_captcha
from services.auth_context import (
    AuthRejected, challenge_digest, constant_time_eq, validate_account_lifecycle,
)
from . import check_permission, get_user_permissions

bp = Blueprint("auth", __name__, url_prefix="/auth")

# 导入api文档模块
from flasgger import swag_from

# 导入审计装饰器
from . import audit_log


# 注册端口
@bp.route("/register", methods=["POST"])
@limiter.limit("5/minute")
@swag_from('../apidocs/user/register.yaml')
@audit_log(operation="用户注册", is_login=True)
def register():
    form = RegisterForm()
    if form.validate():
        email = form.User_Email.data
        password = form.User_Password.data
        username = form.User_Name.data
        captcha = form.User_Captcha.data
        user = UserModel.query.filter_by(email=email).first()

        # D1：验证码用途限定（register），一次性消费；Redis 故障 503（不静默放行）
        try:
            captcha_ok = verify_captcha(email, "register", captcha)
        except AuthRejected as exc:
            return exc.to_response()
        if not captcha_ok:
            data = {
                "code": 400,
                "message": "验证码错误",
            }
            return jsonify(data), 400

        if user:
            data = {
                "code": 401,
                "message": "邮箱已存在",
            }
            return jsonify(data), 401

        else:
            user = UserModel(email=email, username=username, study_stage="未分流")
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            # D1：注册即签发 v2 会话（sid+版本声明；auth_time=注册认证时刻）
            data = {
                "code": 200,
                "message": "注册成功",
                "User_Name": username,
            }
            issued, refresh, csrf_token = _issue_with_optional_cookie(
                user, client_type="user", amr="pwd")
            data.update(issued)
            resp = _finalize_auth_response(data, "user", refresh, csrf_token)
            return resp, 200
    else:
        data = {
            "code": 402,
            "message": form.errors,
        }
        return jsonify(data), 402


# 登录端口
@bp.route("/login", methods=["POST"])
@limiter.limit("10/minute")
@swag_from('../apidocs/user/login.yaml')
@audit_log(operation="用户登录", is_login=True)
def login():
    form = LoginForm()
    if form.validate():
        email = form.User_Email.data
        password = form.User_Password.data
        user = UserModel.query.filter_by(email=email).first()
        # 封禁拦截（2026-09-11 用户管理）：存量 token 由 app.before_request 统一拦
        if user and (user.status or 'active') == 'banned':
            return jsonify({"code": 403, "message": "账号已被封禁，请联系管理员"}), 403
        # 用户不存在与密码错误同一话术（2026-09-16 加固）：防注册邮箱枚举探测
        if not user:
            return jsonify({"code": 402, "message": "邮箱或密码错误"}), 402
        try:
            User_Email = user.email
            User_Medal = user.medal
            User_Stage = user.study_stage
            join_time = user.join_time
            User_Time = join_time.strftime('%Y-%m-%d')
            User_Id = str(user.id).zfill(7)
            Student_Id = user.student_id
            Introduction = user.introduction
            User_Sex = user.sex
            Institute = user.institute
            Major = user.major
            Github_Id = user.github_id
            Skill_Tags = user.skill_tags


            if user.check_password(password):
                # 历史遗留的明文(MD5)密码，登录成功后自动升级为加盐哈希
                if not user.password_is_hashed:
                    user.set_password(password)
                    db.session.commit()
                # D1：账号已绑定 TOTP → 一律走二步验证（不发 token，5 分钟一次性票据）
                mfa_gate = _mfa_gate(user, "user-login-mfa")
                if mfa_gate is not None:
                    return mfa_gate
                code = 200
                msg = "登录成功"
                User_Name = user.username
                data = {
                    "code": code,
                    "message": msg,
                    "User_Name": User_Name,
                    "role": user.role,
                    "role_rank": user.role_rank,
                    "level": user.level,
                    "permissions": get_user_permissions(user.id),
                    "User_Email": User_Email,
                    "User_Medal": User_Medal,
                    "User_Stage": User_Stage,
                    "join_time": User_Time,
                    "User_Id": User_Id,
                    "Student_Id": Student_Id,
                    "Introduction": Introduction,
                    "User_Sex": User_Sex,
                    "Institute": Institute,
                    "Major": Major,
                    "Github_Id": Github_Id,
                    "Skill_Tags": Skill_Tags,
                }
                # D1：v2 会话签发（client_type=user；refresh 按 cookie 开关决定去向）
                issued, refresh, csrf_token = _issue_with_optional_cookie(
                    user, client_type="user", amr="pwd")
                data.update(issued)
                if user.is_staff() and config.MFA_ENFORCE_FOR_ADMIN:
                    # 强制开关开启但未绑定：发 token 但管理端会被 MFA_REQUIRED 拦，
                    # 提示前端引导先到用户端完成绑定（无公开 bootstrap 后门，规格 6.5）
                    data["mfa_enrollment_required"] = True
                return _finalize_auth_response(data, "user", refresh, csrf_token)

            else:
                code = 402
                data = {
                    "code": code,
                    # 与「用户不存在」同话术，防邮箱枚举（2026-09-16 加固）
                    "message": "邮箱或密码错误",
                }
                return jsonify(data),402



            # data = {
            #     "code": code,
            #     "message": msg,
            #     "token": token,
            #     "User_Name": User_Name,
            #     "User_Email": user.email,
            #     "User_Medal": user.medal,
            #     "User_Stage": user.study_stage,
            #     "join_time": user.join_time.strftime("%Y-%m-%d %H:%M:%S"),
            #     "User_Id": user.id,
            #     "Student_Id": user.student_id,
            #     "Introduction": user.introduction,
            #     "User_Sex": user.sex,
            #     "Institute": user.institute,
            #     "Major": user.major,
            #     "Github_Id": user.github_id,
            #     "Skill_Tags": user.skill_tags,
            # }

        except Exception:
            # 走到这里只剩服务端异常（用户不存在已前置）；话术保持中性防信息泄露
            return jsonify({
                "code": 400,
                "message": "登录失败，请稍后重试",
                "token": "Null",
                "User_Name": "Null",
            }), 400

    else:
        data = {
            "code": 403,
            "message": form.errors,
        }
        return jsonify(data), 403


@bp.route("/admin_login", methods=["POST"])
@limiter.limit("10/minute")
@swag_from('../apidocs/user/admin_login.yaml')
@audit_log(operation="管理员登录", is_login=True)
def admin_login():
    form = LoginForm()
    if form.validate():
        email = form.User_Email.data
        password = form.User_Password.data
        admin = UserModel.query.filter_by(email=email).first()

        # 用户不存在与密码错误同一话术（2026-09-16 加固）：防邮箱枚举
        if not admin:
            return jsonify({
                "code": 402,
                "message": "邮箱或密码错误",
                "token": "Null",
                "User_Name": "Null",
            }), 402

        # 封禁拦截（2026-09-17 补洞：/auth/* 不走全局 before_request，此处自查。
        # 此前漏检——被封禁账号虽被业务端点拦，但仍能从管理端登录页成功换新 token）
        if (admin.status or 'active') == 'banned':
            return jsonify({
                "code": 403,
                "message": "账号已被封禁，请联系管理员"
            }), 403

        try:
            if not admin.check_password(password):
                return jsonify({
                    "code": 402,
                    'msg': "邮箱或密码错误",
                    'token' : "Null",
                    'User_Name' : "Null"
                }),402

            else:
                # 先验证凭据再判断准入，避免借不同状态码探测普通用户邮箱。
                # 单项业务权限不等于管理端准入；营期老师使用用户端工作台。
                if not admin.is_staff():
                    return jsonify({
                        "code": 403,
                        'message': "无管理端访问权限"
                    }), 403
                # 历史遗留的明文(MD5)密码，登录成功后自动升级为加盐哈希
                if not admin.password_is_hashed:
                    admin.set_password(password)
                    db.session.commit()
                # D1：已绑定 TOTP → 二步验证（票据 purpose 区分两端）
                mfa_gate = _mfa_gate(admin, "admin-login-mfa")
                if mfa_gate is not None:
                    return mfa_gate
                data = {
                    'code': 200,
                    'msg': "登录成功",
                    'User_Name': admin.username,
                    'role': admin.role,
                    'role_rank': admin.role_rank,
                    'permissions': get_user_permissions(admin.id),
                }
                if config.MFA_ENFORCE_FOR_ADMIN and not auth_mfa.has_active_totp(admin):
                    data['mfa_enrollment_required'] = True
                # D1：v2 会话签发（client_type=admin，续期端点核对双端隔离）
                issued, refresh, csrf_token = _issue_with_optional_cookie(
                    admin, client_type="admin", amr="pwd")
                data.update(issued)
                return _finalize_auth_response(data, "admin", refresh, csrf_token), 200


        except Exception as e:
            print(f"Login error: {e}")
            return jsonify({
                "code": 400,
                "message": "登录失败，请稍后重试",
                "token": "Null",
                "User_Name": "Null",
            }), 400

    else:
        data = {
            "code": 403,
            "message": form.errors,
        }
        return jsonify(data),403


# ── 令牌续期与吊销（2026-09-16 安全加固）──
# access 2h / refresh 14d：前端 packages/api 对 401 静默调 /auth/refresh 续期并重放原请求；
# 每次刷新轮换 refresh（旧的即时吊销），退出时 access+refresh 双吊销。

def _revoke_claims(claims):
    """按 JWT claims 吊销令牌（jti + exp）。"""
    revoke_token(claims["jti"], datetime.fromtimestamp(claims["exp"], tz=timezone.utc))


# ── D1 安全地基：cookie 会话 / CSRF / 原子轮换核心 ──────────────────────

def _cookie_names(client_type):
    return f"bme-{client_type}-rt", f"bme-{client_type}-csrf"


def _set_auth_cookies(resp, client_type, refresh, csrf_token):
    """refresh 落 HttpOnly cookie（Path 限定到本端续期端点，双端隔离）；
    csrf 为可读 cookie 供双提交校验。SameSite=Lax：生产同源、dev 127.0.0.1
    跨端口仍属同站，可随 XHR 发送（不需要 SameSite=None）。"""
    rt, csrf = _cookie_names(client_type)
    max_age = int(config.JWT_REFRESH_TOKEN_EXPIRES.total_seconds())
    common = dict(max_age=max_age, samesite="Lax", secure=config.AUTH_COOKIE_SECURE,
                  path=f"/auth/{client_type}")
    resp.set_cookie(rt, refresh, httponly=True, **common)
    resp.set_cookie(csrf, csrf_token, httponly=False, **common)


def _clear_auth_cookies(resp, client_type):
    rt, csrf = _cookie_names(client_type)
    for name in (rt, csrf):
        resp.set_cookie(name, "", max_age=0, path=f"/auth/{client_type}")


def _csrf_ok(client_type):
    """双提交校验：X-CSRF-Token 头 == 可读 csrf cookie（恒时比较）。"""
    _rt, csrf_name = _cookie_names(client_type)
    header = request.headers.get("X-CSRF-Token", "")
    cookie = request.cookies.get(csrf_name, "")
    return bool(header and cookie) and hmac.compare_digest(header, cookie)


def _origin_ok():
    """cookie 凭证（环境凭据）时的来源校验：Origin 白名单/同源；
    无 Origin 时按 Fetch-Metadata 的 sec-fetch-site 判定。"""
    origin = request.headers.get("Origin")
    if origin:
        allowed = {o.strip() for o in (os.getenv("CORS_ORIGINS") or "").split(",") if o.strip()}
        if origin in allowed or origin == request.host_url.rstrip("/"):
            return True
        return False
    site = request.headers.get("Sec-Fetch-Site", "")
    return site in ("same-origin", "same-site", "none") if site else True


def _issue_with_optional_cookie(user, *, client_type, amr):
    """登录/注册统一签发：返回 (data_dict, csrf_token)。cookie 开关关时
    data 含 refresh_token（兼容模式），开时由调用方 Set-Cookie 且 body 不含。"""
    access, refresh, _session = auth_sessions.issue_session(
        user, client_type=client_type, amr=amr)
    csrf_token = secrets.token_hex(16)
    data = {"token": access}
    data["_refresh_token_body"] = refresh  # cookie 开关关闭时提升为 refresh_token
    data["_csrf_token"] = csrf_token
    return data, refresh, csrf_token


def _finalize_auth_response(data, client_type, refresh, csrf_token):
    """按开关决定 refresh 去向：cookie 模式=Set-Cookie（body 不含），
    兼容模式=body 返回（现有前端/脚本契约不变）。"""
    data.pop("_refresh_token_body", None)
    data.pop("_csrf_token", None)
    if config.AUTH_REFRESH_COOKIE_ENABLED:
        resp = jsonify(data)
        _set_auth_cookies(resp, client_type, refresh, csrf_token)
        return resp
    data["refresh_token"] = refresh
    return jsonify(data)


def _refresh_core(raw_token, claims, *, client_type=None):
    """续期核心事务（锁序 user → auth_session，规格 8.2）：

    v2：sid 行锁 → digest/generation 比对（不匹配=已轮换的重放 → 撤会话）→
        同 sid 轮换，auth_time/amr 原值不动；
    legacy：窗口内一次性兑换（UNIQUE 兜底），产物 amr=unknown。
    返回 (access, refresh, user)。
    """
    from sqlalchemy.exc import IntegrityError

    identity = claims.get("sub")
    base_user = UserModel.query.filter_by(email=identity).first()
    if not base_user:
        raise AuthRejected("账号不存在", status=401, machine="ACCOUNT_MISSING")
    try:
        user = auth_sessions.lock_user(base_user.id)
        if not user:
            raise AuthRejected("账号不存在", status=401, machine="ACCOUNT_MISSING")
        validate_account_lifecycle(user)
        if claims.get("token_schema") == "v2" and claims.get("sid"):
            session = auth_sessions.lock_session(claims["sid"])
            if session is None or session.user_id != user.id:
                raise AuthRejected("会话不存在或已失效，请重新登录",
                                   status=401, machine="SESSION_REVOKED")
            if session.revoked_at is not None:
                raise AuthRejected("登录已失效，请重新登录",
                                   status=401, machine="SESSION_REVOKED")
            if session.expires_at <= datetime.now():
                raise AuthRejected("会话已过期，请重新登录",
                                   status=401, machine="SESSION_EXPIRED")
            raw_digest = challenge_digest(raw_token)
            if not constant_time_eq(raw_digest, session.refresh_digest or ""):
                # 同 sid 的旧 refresh（已被轮换）——并发或重放无法区分：撤会话（规格 6.2）
                session.revoked_at = datetime.now()
                db.session.commit()
                raise AuthRejected("凭证已失效，请重新登录",
                                   status=401, machine="SESSION_REVOKED")
            access, refresh = auth_sessions.rotate_refresh(user, session, client_type=client_type)
        else:
            # legacy 协议：窗口/置位检查 + 一次性兑换
            if user.require_versioned_tokens or datetime.now() > config.AUTH_LEGACY_TOKEN_DEADLINE:
                raise AuthRejected("登录方式已升级，请重新登录",
                                   status=401, machine="TOKEN_SCHEMA_REQUIRED")
            access, refresh, _new = auth_sessions.consume_legacy_refresh(
                user, claims, client_type=client_type or "user")
        db.session.commit()
    except IntegrityError:
        # legacy 兑换 UNIQUE 冲突（并发双发）：只有一个成功
        db.session.rollback()
        raise AuthRejected("该凭证已使用，请重新登录",
                           status=401, machine="SESSION_REVOKED")
    except AuthRejected:
        db.session.rollback()
        raise
    # blocklist 旧 refresh jti（补充防线，Redis 故障仅日志）
    if claims.get("jti") and claims.get("exp"):
        try:
            _revoke_claims(claims)
        except Exception:
            pass
    return access, refresh, user


def _typed_refresh(client_type):
    """/auth/{user,admin}/refresh：cookie（CSRF+Origin 强制）或 Bearer refresh。"""
    _rt_name, _csrf_name = _cookie_names(client_type)
    raw = request.cookies.get(_rt_name)
    via_cookie = bool(raw)
    if not raw:
        header = request.headers.get("Authorization", "")
        if header.startswith("Bearer "):
            raw = header[7:]
    if not raw:
        return jsonify({"code": 401, "message": "未提供访问令牌",
                        "machine": "ACCESS_TOKEN_MISSING"}), 401
    if via_cookie and not (_csrf_ok(client_type) and _origin_ok()):
        return jsonify({"code": 403, "message": "请求来源校验失败",
                        "machine": "CSRF_REJECTED"}), 403
    try:
        claims = decode_token(raw)
        if claims.get("type") != "refresh":
            raise ValueError("not a refresh token")
    except Exception:
        resp = jsonify({"code": 401, "message": "访问令牌无效",
                        "machine": "INVALID_TOKEN"})
        _clear_auth_cookies(resp, client_type)
        return resp, 401
    try:
        access, refresh, user = _refresh_core(raw, claims, client_type=client_type)
    except AuthRejected as exc:
        resp, status = exc.to_response()
        _clear_auth_cookies(resp, client_type)
        return resp, status
    data = {"code": 200, "token": access, "role": user.role,
            "permissions": get_user_permissions(user.id), "level": user.level}
    if config.AUTH_REFRESH_COOKIE_ENABLED:
        resp = jsonify(data)
        _rt, csrf_cookie = _cookie_names(client_type)
        csrf_token = request.cookies.get(csrf_cookie) or secrets.token_hex(16)
        _set_auth_cookies(resp, client_type, refresh, csrf_token)
        return resp
    data["refresh_token"] = refresh
    return jsonify(data)


def _typed_logout(client_type):
    """/auth/{user,admin}/logout：DB 撤 sid + 清 cookie，幂等。"""
    _rt_name, _ = _cookie_names(client_type)
    raw = request.cookies.get(_rt_name)
    via_cookie = bool(raw)
    if not raw:
        header = request.headers.get("Authorization", "")
        if header.startswith("Bearer "):
            raw = header[7:]
    if via_cookie and not _csrf_ok(client_type):
        resp = jsonify({"code": 403, "message": "请求来源校验失败",
                        "machine": "CSRF_REJECTED"})
        _clear_auth_cookies(resp, client_type)
        return resp, 403
    for candidate in (raw, request.headers.get("Authorization", "")[7:] or None):
        if not candidate:
            continue
        try:
            claims = decode_token(candidate)
        except Exception:
            continue
        if claims.get("sid"):
            auth_sessions.revoke_session(claims["sid"])
        if claims.get("jti") and claims.get("exp"):
            try:
                _revoke_claims(claims)
            except Exception:
                pass
    db.session.commit()
    resp = jsonify({"code": 200, "message": "已退出"})
    _clear_auth_cookies(resp, client_type)
    return resp


@bp.route("/dev_accounts", methods=["GET"])
def dev_accounts():
    """开发测试账号面板（登录页 dev 快捷登录的数据源）：返回 @seed.dev 域账号的
    昵称/角色/标签，密码约定统一 12345678（见两仓 README 开发测试账号节）。
    门禁：仅 debug 模式或显式 DEV_TEST_ACCOUNTS 配置时可见，生产一律 404——
    不构成生产后门（登录本身仍走 /auth/login 正常校验）。"""
    from flask import current_app
    if not (current_app.debug or current_app.config.get("DEV_TEST_ACCOUNTS")):
        return jsonify({"code": 404, "message": "not found"}), 404
    rows = (UserModel.query
            .filter(UserModel.email.like("%@seed.dev"))
            .order_by(UserModel.role.asc(), UserModel.id.asc()).all())
    return jsonify({"code": 200, "data": {"accounts": [
        {"email": u.email, "username": u.username, "role": u.role, "admin_tag": u.admin_tag}
        for u in rows]}})


@bp.route("/session/config", methods=["GET"])
def session_config():
    """会话模式发现（公开，前端 facade 的探测端点）：

    refresh_cookie_enabled 决定前端走 HttpOnly cookie 模式还是兼容模式
    （localStorage 双键）；legacy_deadline / mfa_enforced_for_admin 供提示。
    no-store：模式判定不做缓存共享（规格第 11 章 Cache-Control 要求）。
    """
    from flask import current_app
    resp = jsonify({
        "code": 200,
        "refresh_cookie_enabled": bool(current_app.config.get("AUTH_REFRESH_COOKIE_ENABLED")),
        "legacy_deadline": config.AUTH_LEGACY_TOKEN_DEADLINE.isoformat(),
        "mfa_enforced_for_admin": bool(current_app.config.get("MFA_ENFORCE_FOR_ADMIN")),
    })
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _mfa_gate(user, ticket_purpose):
    """已绑定 TOTP 的账号：登录第一步只发 5 分钟一次性票据，不发 token。

    返回 None 表示无 MFA 门槛（继续正常签发）；否则返回二步响应。
    密码已经验过——票据即「凭据已证明」的短期凭证；Redis 故障 503（fail-closed）。
    """
    if not auth_mfa.has_active_totp(user):
        return None
    try:
        ticket = auth_mfa.issue_login_ticket(user, ticket_purpose)
    except AuthRejected as exc:
        resp, status = exc.to_response()
        return resp, status
    return jsonify({"code": 200, "mfa_required": True, "mfa_token": ticket,
                    "message": "请输入动态验证码"}), 200


def _login_mfa_verify(client_type):
    """/auth/{login,admin_login}/mfa：票据 + TOTP 或恢复码 → 签 amr=pwd+totp 会话。"""
    body = request.get_json(silent=True) or {}
    try:
        uid = auth_mfa.verify_login_ticket(body.get("mfa_token") or "",
                                           f"{client_type}-login-mfa")
    except AuthRejected as exc:
        return exc.to_response()
    user = UserModel.query.get(uid)
    if not user:
        return jsonify({"code": 401, "message": "账号不存在"}), 401
    try:
        validate_account_lifecycle(user)
    except AuthRejected as exc:
        return exc.to_response()
    proved = False
    factor = auth_mfa.active_totp_factor(user)
    if factor and body.get("code"):
        proved = auth_mfa.verify_totp_code(factor, body["code"])
    if not proved and body.get("recovery_code"):
        proved = auth_mfa.consume_recovery_code(user, body["recovery_code"])
    if not proved:
        db.session.rollback()
        return jsonify({"code": 402, "message": "验证码错误",
                        "machine": "MFA_CODE_INVALID"}), 402
    data = {"code": 200, "User_Name": user.username, "role": user.role,
            "role_rank": user.role_rank, "level": user.level,
            "permissions": get_user_permissions(user.id)}
    issued, refresh, csrf_token = _issue_with_optional_cookie(
        user, client_type=client_type, amr="pwd+totp")
    data.update(issued)
    return _finalize_auth_response(data, client_type, refresh, csrf_token), 200


@bp.route("/login/mfa", methods=["POST"])
@limiter.limit("10/minute")
@audit_log(operation="用户登录二步验证", is_login=True)
def login_mfa():
    return _login_mfa_verify("user")


@bp.route("/admin_login/mfa", methods=["POST"])
@limiter.limit("10/minute")
@audit_log(operation="管理员登录二步验证", is_login=True)
def admin_login_mfa():
    return _login_mfa_verify("admin")


@bp.route("/user/refresh", methods=["POST"])
def user_refresh():
    """用户端续期（cookie 模式主通道；Bearer 兼容）。client_type 强制匹配。"""
    return _typed_refresh("user")


@bp.route("/admin/refresh", methods=["POST"])
def admin_refresh():
    """管理端续期（与用户端 cookie/会话完全隔离）。"""
    return _typed_refresh("admin")


@bp.route("/user/logout", methods=["POST"])
def user_logout():
    """用户端退出：DB 撤 sid + 清本端 cookie。幂等。"""
    return _typed_logout("user")


@bp.route("/admin/logout", methods=["POST"])
def admin_logout():
    """管理端退出：DB 撤 sid + 清本端 cookie。幂等。"""
    return _typed_logout("admin")


@bp.route("/refresh", methods=["POST"])
@jwt_required(refresh=True)
def refresh():
    """旧端点（兼容期保留，下线计划见 D1 文档）：v2 token 走同轮换事务
    （不校验 client_type——Bearer 显式凭证无跨端 cookie 问题）；legacy token
    一次性兑换为 v2 会话。身份随行（2026-09-17）语义不变。"""
    raw = request.headers.get("Authorization", "")
    raw = raw[7:] if raw.startswith("Bearer ") else ""
    try:
        access, refresh_token, user = _refresh_core(raw, get_jwt(), client_type=None)
    except AuthRejected as exc:
        return exc.to_response()
    return jsonify({
        "code": 200,
        "token": access,
        "refresh_token": refresh_token,
        "role": user.role,
        "permissions": get_user_permissions(user.id),
        "level": user.level,
    }), 200


@bp.route("/logout", methods=["POST"])
def logout():
    """旧退出端点（兼容期保留）：吊销 Bearer access 与 body refresh_token；
    v2 token 额外撤销对应 auth_session（数据库真相源）。

    幂等设计——不挂 @jwt_required：token 已过期/已吊销时退出仍应成功，
    前端本地清理不依赖本端点结果（fire-and-forget）。
    """
    candidates = []
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        candidates.append(auth_header[7:])
    body = request.get_json(silent=True) or {}
    refresh_raw = body.get("refresh_token")
    if refresh_raw and isinstance(refresh_raw, str):
        candidates.append(refresh_raw)

    for raw in candidates:
        try:
            claims = decode_token(raw)
        except Exception:
            continue  # 无效/过期：无甚可吊销
        if claims.get("sid"):
            auth_sessions.revoke_session(claims["sid"])
        if claims.get("jti") and claims.get("exp"):
            try:
                _revoke_claims(claims)
            except Exception:
                pass
    db.session.commit()
    return jsonify({"code": 200, "message": "已退出"}), 200


# @bp.route("/mail/test")
# def mail_test():
#     messages = Message(subject="mail test", recipients=["jerrycaocao@126.com"], body="mail test")
#     mail.send(messages)
#     return "mail send succeed"


# 邮件验证码获取端口
@bp.route("/captcha/email", methods=["POST"])
@limiter.limit("1/minute")
@swag_from('../apidocs/user/get_email_captcha.yaml')
@audit_log(operation="获取邮件验证码", is_login=True)
def get_email_captcha():
    """D1：验证码用途限定（purpose=register|findpwd 必填）。

    生成走 services.auth_challenges（secrets 安全随机、HMAC 摘要存储、
    60s 冷却 + 小时/日限次）；旧 random.sample 生成器与注册/找回共用 key 均退役。
    两仓前端同批发布带 purpose 字段；陈旧 bundle 会收到 400 与明确提示。
    """
    payload = request.get_json(silent=True) or {}
    email = (payload.get("User_Email") or "").strip()
    purpose = payload.get("purpose")
    if not email:
        return jsonify({"code": 400, "message": "缺少邮箱"}), 400
    if purpose not in ("register", "findpwd"):
        return jsonify({"code": 400,
                        "message": "缺少 purpose 参数（register/findpwd），请刷新页面后重试"}), 400
    try:
        captcha = issue_captcha(email, purpose)
    except AuthRejected as exc:
        resp, status = exc.to_response()
        if exc.machine == "CHALLENGE_COOLDOWN":
            resp.headers["Retry-After"] = "60"
        return resp, status
    try:
        mail.send(Message(subject="BME卓越工程师在线教育平台",
                          recipients=[email], body=f"您的验证码是:{captcha}"))
    except Exception:
        # 邮件发送失败：challenge 不算成功（删除登记，用户可立即重发）
        from exts import redis_client as _rc
        try:
            _rc.delete(f"captcha:{purpose}:{(email or '').strip().lower()}")
            _rc.delete(f"captcha:cd:{purpose}:{(email or '').strip().lower()}")
        except Exception:
            pass
        return jsonify({"code": 503, "message": "邮件发送失败，请稍后重试"}), 503
    return jsonify({"code": 200, "message": "邮件发送成功"})


@bp.route("/find_password", methods=["POST"])
@limiter.limit("5/minute")
@swag_from('../apidocs/user/find_password.yaml')
@audit_log(operation="找回密码", is_login=True)
def find_password():
    data = request.get_json()
    email = data['User_Email']
    password = data['Password']
    captcha = data['Captcha']
    user = UserModel.query.filter_by(email=email).first()
    if user is None:
        # 中性话术（2026-09-16 加固）：防邮箱枚举；找回流程由邮箱验证码把关
        return jsonify({
            "code": 400,
            "message": "账号或验证码有误"
        }), 400

    # D1：findpwd 专用验证码；通过后重置密码并撤回全部旧会话（安全版本+1）
    try:
        captcha_ok = verify_captcha(email, "findpwd", captcha)
    except AuthRejected as exc:
        return exc.to_response()
    if not captcha_ok:
        return jsonify({
            "code": 402,
            "message": "验证码错误"
        }), 402

    user = auth_sessions.lock_user(user.id)
    user.set_password(password)
    auth_sessions.bump_security_version(user, reason="找回密码重置")
    db.session.commit()
    return jsonify({
        "code": 200,
        "message": "密码修改成功，所有旧登录已失效，请使用新密码重新登录"
    })

# 管理员获取审计日志记录
@bp.route("/audit_records", methods=["GET"])
@jwt_required()
@check_permission('system_management')
@swag_from('../apidocs/user/audit_records.yaml')
def get_admin_audit_logs():
    # 获取分页参数
    page = request.args.get('page', 1, type=int)
    per_page = min(request.args.get('per_page', 10, type=int), 100)
    
    # 获取筛选参数
    user_id = request.args.get('user_id', type=int)
    operation = request.args.get('operation', type=str)
    
    # 查询审计日志
    query = AuditLog.query
    
    # 应用筛选条件
    if user_id:
        query = query.filter_by(user_id=user_id)
    
    if operation:
        query = query.filter(AuditLog.operation.contains(operation))
    
    logs_pagination = query.order_by(AuditLog.timestamp.desc()).paginate(
        page=page, per_page=per_page, error_out=False)
    
    # 格式化返回数据
    logs_data = []
    for log in logs_pagination.items:
        user = UserModel.query.get(log.user_id)
        logs_data.append({
            "id": log.id,
            "user_id": log.user_id,
            "username": log.username,
            "ip_address": log.ip_address,
            "user_agent": log.user_agent,
            "operation": log.operation,
            "operation_url": log.operation_url,
            "operation_data": log.operation_data,
            "result": log.result,
            "timestamp": log.timestamp.isoformat() if log.timestamp else None
        })
    
    return jsonify({
        "code": 200,
        "message": "查询成功",
        "data": {
            "logs": logs_data,
            "pagination": {
                "page": logs_pagination.page,
                "per_page": logs_pagination.per_page,
                "total": logs_pagination.total,
                "pages": logs_pagination.pages
            }
        }
    })

"""管理员 MFA 端点（D1，规格 6.5）：绑定/停用/恢复码 + 状态查询。

权限：仅 super_admin（is_staff）可 enroll——D1 不开放普通用户 MFA。
enroll/confirm 与 disable/regenerate 均走 JWT；disable 与恢复码重发要求
近期认证（require_recent_auth，legacy 会话恒不通过）。

安全语义：
- confirm 成功 = 因子激活 + 恢复码一次性下发 + bump(except_sid=当前会话)
  并随响应换发新 access（版本已变，旧 access 即刻失效）；
- 停用需「近期认证 + 当前 TOTP 码或一个未用恢复码」，防单凭会话接管；
- 响应含 secret/恢复码，审计脱敏规则已覆盖（secret/recovery_code/otpauth）。
"""
from datetime import datetime
from functools import wraps

from flask import Blueprint, jsonify, request
from flask_jwt_extended import create_access_token, jwt_required

import config
from exts import db
from models import AuthFactorModel, UserModel  # noqa: F401 (UserModel 供类型引用)
from services import auth_mfa, auth_sessions
from services.auth_context import AuthRejected, current_actor, require_recent_auth
from . import audit_log

bp = Blueprint("auth_mfa", __name__, url_prefix="/auth/mfa")


def _mfa_endpoint(f):
    """AuthRejected 统一转响应（401/403/503 语义见 auth_context 错误契约）。"""
    @wraps(f)
    def wrapper(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except AuthRejected as exc:
            return exc.to_response()
    return wrapper


def _require_staff_actor():
    actor = current_actor()
    if not actor:
        raise AuthRejected("未认证", status=401, machine="ACCESS_TOKEN_MISSING")
    if not actor.user.is_staff():
        raise AuthRejected("仅管理员可使用 MFA", status=403, machine="FORBIDDEN")
    return actor


def _fresh_access(actor):
    """bump 后随响应换发的新 access（except_sid 会话已同步新版本快照）。"""
    if not actor.session:
        return None
    return create_access_token(
        identity=actor.user.email,
        additional_claims={"sid": actor.session.sid,
                           "security_version": actor.session.security_version,
                           "token_schema": "v2"})


@bp.route("/status", methods=["GET"])
@jwt_required()
@_mfa_endpoint
def mfa_status():
    actor = _require_staff_actor()
    user = actor.user
    pending = AuthFactorModel.query.filter_by(
        user_id=user.id, factor_type='totp', state='pending').count()
    return jsonify({
        "code": 200,
        "enabled": auth_mfa.has_active_totp(user),
        "pending": bool(pending),
        "recovery_codes_remaining": auth_mfa.recovery_codes_remaining(user),
        "enforced": config.MFA_ENFORCE_FOR_ADMIN,
    })


@bp.route("/totp/enroll/start", methods=["POST"])
@jwt_required()
@audit_log(operation="MFA 开始绑定")
@_mfa_endpoint
def totp_enroll_start():
    actor = _require_staff_actor()
    uri, secret, _factor = auth_mfa.start_totp_enrollment(actor.user)
    db.session.commit()
    return jsonify({"code": 200, "otpauth_uri": uri, "secret": secret})


@bp.route("/totp/enroll/confirm", methods=["POST"])
@jwt_required()
@audit_log(operation="MFA 绑定确认")
@_mfa_endpoint
def totp_enroll_confirm():
    """验证首码激活因子；一次性下发恢复码；bump 保留当前会话并换发新 access。"""
    actor = _require_staff_actor()
    user = actor.user
    code = (request.get_json(silent=True) or {}).get("code", "")
    factor = (AuthFactorModel.query
              .filter_by(user_id=user.id, factor_type='totp', state='pending')
              .order_by(AuthFactorModel.id.desc()).first())
    if not factor:
        return jsonify({"code": 400, "message": "没有待确认的绑定，请先开始绑定"}), 400
    if not auth_mfa.verify_totp_code(factor, code):
        db.session.rollback()
        return jsonify({"code": 402, "message": "验证码错误"}), 402
    factor.state = 'active'
    factor.confirmed_at = datetime.now()
    codes = auth_mfa.generate_recovery_codes(user)
    current_sid = actor.session.sid if actor.session else None
    auth_sessions.bump_security_version(
        auth_sessions.lock_user(user.id), except_sid=current_sid,
        reason="MFA 绑定确认")
    db.session.commit()
    data = {"code": 200, "message": "MFA 绑定成功", "recovery_codes": codes}
    fresh = _fresh_access(actor)
    if fresh:
        data["token"] = fresh
    return jsonify(data)


@bp.route("/totp/disable", methods=["POST"])
@jwt_required()
@audit_log(operation="MFA 停用")
@_mfa_endpoint
def totp_disable():
    """停用需近期认证 + 当前 TOTP 码或一个未用恢复码（防单凭会话接管）。"""
    actor = _require_staff_actor()
    try:
        require_recent_auth(actor)
    except AuthRejected as exc:
        return exc.to_response()
    body = request.get_json(silent=True) or {}
    user = actor.user
    factor = auth_mfa.active_totp_factor(user)
    proved = False
    if factor and body.get("code"):
        proved = auth_mfa.verify_totp_code(factor, body["code"])
    if not proved and body.get("recovery_code"):
        proved = auth_mfa.consume_recovery_code(user, body["recovery_code"])
    if not proved:
        db.session.rollback()
        return jsonify({"code": 402, "message": "需要当前验证码或恢复码"}), 402
    if factor:
        factor.state = 'revoked'
    current_sid = actor.session.sid if actor.session else None
    auth_sessions.bump_security_version(
        auth_sessions.lock_user(user.id), except_sid=current_sid, reason="MFA 停用")
    db.session.commit()
    data = {"code": 200, "message": "MFA 已停用"}
    fresh = _fresh_access(actor)
    if fresh:
        data["token"] = fresh
    return jsonify(data)


@bp.route("/recovery-code/regenerate", methods=["POST"])
@jwt_required()
@audit_log(operation="MFA 恢复码重发")
@_mfa_endpoint
def recovery_codes_regenerate():
    actor = _require_staff_actor()
    try:
        require_recent_auth(actor)
    except AuthRejected as exc:
        return exc.to_response()
    codes = auth_mfa.generate_recovery_codes(actor.user)
    db.session.commit()
    return jsonify({"code": 200, "message": "恢复码已重置（旧码全部作废）",
                    "recovery_codes": codes})

"""管理员 MFA（D1，规格 6.5）：TOTP 因子 + 一次性恢复码 + 登录二步票据。

- TOTP 用 pyotp（成熟库实现），secret 经 Fernet(MFA_ENC_SECRET) 加密存储；
  last_accepted_counter 拒绝同一时间步重放（RFC 6238 时间窗内已用的码不可再用）。
- 恢复码只存 HMAC 摘要，一次性消费；明文仅在生成响应里出现一次。
- 登录二步票据：itsdangerous 签名（5 分钟）+ Redis nonce 一次性消费；
  Redis 不可达 → 503（挑战类流程 fail-closed，规格 6.2）。
- 普通密码重置不清除 MFA（规格 6.5）；MFA 丢失的独立恢复流程属 D3，本期不提供
  自助清除——密钥丢失只能由离线维护流程处理（MFA_ENFORCE_FOR_ADMIN 默认关）。
"""
import secrets
from datetime import datetime

import pyotp
from cryptography.fernet import Fernet, InvalidToken
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

import config
from exts import db, redis_client
from models import AuthFactorModel, AuthRecoveryCodeModel
from services.auth_context import AuthRejected, challenge_digest

RECOVERY_CODE_COUNT = 10
TICKET_TTL = 300  # 登录二步票据 5 分钟


def _fernet():
    if not config.MFA_ENC_SECRET:
        raise AuthRejected("MFA 服务未配置加密密钥", status=503, machine="SERVICE_UNAVAILABLE")
    return Fernet(config.MFA_ENC_SECRET.encode())


def active_totp_factor(user):
    return (AuthFactorModel.query
            .filter_by(user_id=user.id, factor_type='totp', state='active')
            .order_by(AuthFactorModel.id.desc()).first())


def has_active_totp(user):
    return active_totp_factor(user) is not None


def start_totp_enrollment(user):
    """生成新 secret 建 pending 因子（旧 pending 作废）；返回 (otpauth_uri, secret)。"""
    f = _fernet()
    AuthFactorModel.query.filter_by(user_id=user.id, factor_type='totp', state='pending')\
        .update({'state': 'revoked'})
    secret = pyotp.random_base32()
    factor = AuthFactorModel(user_id=user.id, factor_type='totp',
                             encrypted_secret=f.encrypt(secret.encode()).decode(),
                             state='pending')
    db.session.add(factor)
    db.session.flush()
    uri = pyotp.totp.TOTP(secret).provisioning_uri(
        name=user.email, issuer_name="BME")
    return uri, secret, factor


def verify_totp_code(factor, code):
    """校验 6 位 TOTP 码（valid_window=1）并推进防重放计数。

    返回 True/False；不落库（调用方决定事务）。
    """
    try:
        secret = _fernet().decrypt(factor.encrypted_secret.encode()).decode()
    except InvalidToken:
        return False  # 密钥轮换后旧密文不可解：视为验证失败，走恢复码
    totp = pyotp.TOTP(secret)
    ok = totp.verify(code or "", valid_window=1)
    if ok:
        counter = int(datetime.now().timestamp()) // totp.interval
        if factor.last_accepted_counter is not None and counter <= factor.last_accepted_counter:
            return False  # 同一时间步重放
        factor.last_accepted_counter = counter
    return ok


def generate_recovery_codes(user):
    """重置恢复码：旧的作废，生成 N 个（明文仅本次返回，库存摘要）。"""
    AuthRecoveryCodeModel.query.filter_by(user_id=user.id, used_at=None)\
        .delete(synchronize_session=False)
    codes = [secrets.token_hex(4).upper() for _ in range(RECOVERY_CODE_COUNT)]
    for code in codes:
        db.session.add(AuthRecoveryCodeModel(user_id=user.id,
                                             code_digest=challenge_digest(code)))
    return codes


def consume_recovery_code(user, code):
    """一次性消费恢复码（按唯一摘要索引定位；命中即置 used_at）。"""
    digest = challenge_digest((code or '').strip().upper())
    row = AuthRecoveryCodeModel.query.filter_by(code_digest=digest, used_at=None).first()
    if row and row.user_id == user.id:
        row.used_at = datetime.now()
        return True
    return False


def recovery_codes_remaining(user):
    return AuthRecoveryCodeModel.query.filter_by(user_id=user.id, used_at=None).count()


# ── 登录二步票据 ─────────────────────────────────────────────

_serializer = None


def _ticket_serializer():
    global _serializer
    if _serializer is None:
        _serializer = URLSafeTimedSerializer(
            config.JWT_SECRET_KEY, salt="mfa-login-ticket-v1")
    return _serializer


def issue_login_ticket(user, purpose):
    """签 5 分钟一次性票据（Redis nonce 兜一次性；nonce 不入签名字段外泄）。"""
    nonce = secrets.token_hex(16)
    try:
        redis_client.setex(f"mfa:ticket:{nonce}", TICKET_TTL, str(user.id))
    except Exception as e:
        raise AuthRejected("认证服务暂不可用，请稍后重试",
                           status=503, machine="SERVICE_UNAVAILABLE") from e
    return _ticket_serializer().dumps({"uid": user.id, "nonce": nonce, "purpose": purpose})


def verify_login_ticket(mfa_token, expected_purpose):
    """验签 + 有效期 + Redis nonce 一次性消费；返回 user_id。"""
    try:
        payload = _ticket_serializer().loads(mfa_token, max_age=TICKET_TTL)
    except SignatureExpired:
        raise AuthRejected("验证已超时，请重新登录", status=401, machine="MFA_TICKET_EXPIRED")
    except BadSignature:
        raise AuthRejected("验证票据无效", status=401, machine="INVALID_TOKEN")
    if payload.get("purpose") != expected_purpose:
        raise AuthRejected("验证票据无效", status=401, machine="INVALID_TOKEN")
    nonce = payload.get("nonce", "")
    try:
        held = redis_client.getdel(f"mfa:ticket:{nonce}")
    except Exception as e:
        raise AuthRejected("认证服务暂不可用，请稍后重试",
                           status=503, machine="SERVICE_UNAVAILABLE") from e
    if held is None:
        raise AuthRejected("验证票据已使用或失效，请重新登录",
                           status=401, machine="MFA_TICKET_USED")
    held_uid = held.decode() if isinstance(held, bytes) else str(held)
    if held_uid != str(payload.get("uid")):
        raise AuthRejected("验证票据已使用或失效，请重新登录",
                           status=401, machine="MFA_TICKET_USED")
    return payload["uid"]

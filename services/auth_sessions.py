"""会话写路径（D1 安全地基）：签发/轮换/legacy 兑换/统一撤权原语。

锁序约定（规格 8.2）：先 user（按 id）后 auth_session（按 sid）；
bump_security_version 必须在调用方已持 user 行锁的事务内执行。

撤权不依赖 Redis blocklist：v2 token 三处版本比对 + auth_session 撤销位即时生效，
blocklist 只作为旧 jti 的补充防线（历史端点仍在用）。
"""
import uuid
from datetime import datetime, timedelta

from flask_jwt_extended import create_access_token, create_refresh_token

from exts import db
from models import AuthLegacyRefreshConsumptionModel, AuthSessionModel, UserModel
from services.auth_context import AuthRejected, challenge_digest

import config


def lock_user(user_id):
    """按 id 行锁用户（锁序第一步；SQLite 测试下为无锁直查）。"""
    return db.session.get(UserModel, user_id, with_for_update=True)


def lock_session(sid):
    """按 sid 行锁会话（锁序第二步：必须在 lock_user 之后）。"""
    return db.session.get(AuthSessionModel, sid, with_for_update=True)


def _v2_claims(sid, security_version):
    return {"sid": sid, "security_version": security_version, "token_schema": "v2"}


def issue_session(user, *, client_type, amr, auth_time=None):
    """签发 v2 token 对并建 auth_session（不 commit，随调用方事务）。

    auth_time 只由实际认证产生（登录/二步验证）；legacy 兑换经 amr='unknown' 标记
    低保证，refresh 轮换不刷新该值（规格 6.2）。
    """
    now = datetime.now()
    sid = uuid.uuid4().hex
    claims = _v2_claims(sid, user.security_version or 0)
    refresh = create_refresh_token(identity=user.email, additional_claims={**claims, "gen": 1})
    session = AuthSessionModel(
        sid=sid,
        user_id=user.id,
        client_type=client_type,
        security_version=user.security_version or 0,
        refresh_digest=challenge_digest(refresh),
        generation=1,
        auth_time=auth_time or now,
        amr=amr,
        expires_at=now + config.JWT_REFRESH_TOKEN_EXPIRES,
        user_agent=_trim_user_agent(),
    )
    db.session.add(session)
    access = create_access_token(identity=user.email, additional_claims=claims)
    return access, refresh, session


def rotate_refresh(user, session, *, client_type=None):
    """轮换：同 sid、generation+1、覆写摘要；auth_time/amr 原值不动（不 commit）。

    client_type 传入时校验会话归属端（cookie 双端隔离，规格 6.5）。
    """
    if client_type and session.client_type != client_type:
        raise AuthRejected("会话与客户端类型不匹配", status=403, machine="CLIENT_TYPE_MISMATCH")
    claims = _v2_claims(session.sid, user.security_version or 0)
    new_gen = session.generation + 1
    refresh = create_refresh_token(identity=user.email, additional_claims={**claims, "gen": new_gen})
    session.generation = new_gen
    session.refresh_digest = challenge_digest(refresh)
    session.security_version = user.security_version or 0
    session.last_used_at = datetime.now()
    access = create_access_token(identity=user.email, additional_claims=claims)
    return access, refresh


def consume_legacy_refresh(user, old_claims, *, client_type):
    """旧协议 refresh 一次性兑换为 v2 会话（不 commit）。

    UNIQUE(old_jti_digest) 是防重放的最终裁决——重复消费（并发双发/重放）直接
    SESSION_REVOKED；兑换产物 amr='unknown'（require_recent_auth 恒不通过）。
    """
    jti = old_claims.get("jti")
    if not jti:
        raise AuthRejected("访问令牌无效", status=401, machine="INVALID_TOKEN")
    digest = challenge_digest(jti)
    consumed = db.session.get(AuthLegacyRefreshConsumptionModel, digest)
    if consumed is not None:
        raise AuthRejected("该凭证已使用，请重新登录", status=401, machine="SESSION_REVOKED")
    access, refresh, session = issue_session(user, client_type=client_type, amr="unknown")
    db.session.add(AuthLegacyRefreshConsumptionModel(
        old_jti_digest=digest,
        user_id=user.id,
        new_sid=session.sid,
        expires_at=datetime.now() + timedelta(days=14),
    ))
    return access, refresh, session


def bump_security_version(user, *, except_sid=None, reason=""):
    """统一撤权原语（必须在 lock_user 事务内调用，不 commit）：

    security_version+1、require_versioned_tokens=1（旧协议 token 即刻全拒）、
    撤销该用户全部会话；except_sid 指定的会话保留并同步版本快照（给「刚完成
    敏感操作的当前页」随响应换新 access 续命），其余旧 access/refresh 立即失效。
    """
    user.security_version = (user.security_version or 0) + 1
    user.require_versioned_tokens = True
    now = datetime.now()
    kept = 0
    for session in AuthSessionModel.query.filter_by(user_id=user.id, revoked_at=None).all():
        if except_sid is not None and session.sid == except_sid:
            session.security_version = user.security_version
            kept += 1
        else:
            session.revoked_at = now
    if reason:
        print(f"[auth] security_version -> {user.security_version}（{reason}，撤会话 "
              f"{AuthSessionModel.query.filter_by(user_id=user.id).count() - kept} 保留 {kept}）")
    return user.security_version


def revoke_session(sid, *, replaced_by=None):
    """撤销单个会话（logout / 疑似重放兜底）。幂等。"""
    session = db.session.get(AuthSessionModel, sid)
    if session and session.revoked_at is None:
        session.revoked_at = datetime.now()
    return session


def _trim_user_agent():
    from flask import request
    ua = request.headers.get("User-Agent", "") if request else ""
    return ua[:255] or None

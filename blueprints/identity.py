"""用户端身份中心 API（D3a，规格 5.1/12.1）。

端点前缀 /identity，全部走统一解析（resolve_actor：v2 严格校验；全局守卫
带 Bearer 即拦）。开关语义（规格 13.1）：IDENTITY_VERIFICATION_ENABLED 关=
停新申请/新挑战（已提交仍可查询）；IDENTITY_UI_ENABLED 是前端入口总闸，后端
写端点同时校验两闸，读端点（status）常开供前端展示状态页。

挑战短码绝不回 API 响应——issue 只经邮件发送（规格 8.1）。
"""
from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required
from flask_mail import Message
from flask_limiter import Limiter

import config
from exts import db, limiter, mail
from services.auth_context import AuthRejected, current_actor
from services.identity import verification

bp = Blueprint("identity", __name__, url_prefix="/identity")


def _identity_endpoint(f):
    """AuthRejected 统一转响应（错误契约与 auth_mfa 同款）。"""
    from functools import wraps

    @wraps(f)
    def wrapper(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except AuthRejected as exc:
            return exc.to_response()
    return wrapper


def _actor():
    actor = current_actor()
    if not actor:
        raise AuthRejected("未认证", status=401, machine="ACCESS_TOKEN_MISSING")
    return actor


def _require_writes_open():
    if not config.IDENTITY_VERIFICATION_ENABLED or not config.IDENTITY_UI_ENABLED:
        raise AuthRejected("身份核验暂未开放，请留意公告", status=403,
                           machine="IDENTITY_DISABLED")


def _load_app(actor, app_id):
    app = db.session.get(verification.IdentityApplicationModel, app_id)
    if app is None or app.applicant_user_id != actor.user.id:
        # 不区分不存在/无权，防枚举（规格 16 隐私行）
        raise AuthRejected("申请不存在", status=404, machine="NOT_FOUND")
    return app


@bp.route("/status", methods=["GET"])
@jwt_required()
@_identity_endpoint
def status():
    """人员核验态 + 我的申请 + 开放学校清单（读端点常开，供状态页）。"""
    actor = _actor()
    data = verification.application_overview(actor.user)
    data['verification_enabled'] = config.IDENTITY_VERIFICATION_ENABLED
    data['ui_enabled'] = config.IDENTITY_UI_ENABLED
    return jsonify({"code": 200, **data})


@bp.route("/applications", methods=["POST"])
@jwt_required()
@_identity_endpoint
def create_application():
    """建/改草稿：{school_id, claimed_name, claimed_identifier, contact_email}。"""
    _require_writes_open()
    actor = _actor()
    payload = request.get_json(silent=True) or {}
    app = verification.create_or_update_application(
        actor.user,
        school_id=payload.get("school_id") or "",
        claimed_name=payload.get("claimed_name") or "",
        claimed_identifier=payload.get("claimed_identifier") or "",
        contact_email=payload.get("contact_email") or "")
    db.session.commit()
    return jsonify({"code": 200, "message": "已保存草稿",
                    "application": verification._app_brief(app)})


@bp.route("/applications/<int:app_id>/challenge", methods=["POST"])
@jwt_required()
@_identity_endpoint
def send_challenge(app_id):
    """发送核验验证码到申请邮箱（短码只走邮件，不回响应）。"""
    _require_writes_open()
    actor = _actor()
    app = _load_app(actor, app_id)
    code, ch = verification.issue_challenge(actor.user, app)
    try:
        mail.send(Message(subject="BME 身份核验验证码",
                          recipients=[app.contact_email],
                          body=f"您的核验验证码是：{code}（5 分钟内有效）。"
                               f"如非本人操作请忽略本邮件。"))
    except Exception:
        db.session.rollback()
        return jsonify({"code": 503, "message": "邮件发送失败，请稍后重试"}), 503
    db.session.commit()
    return jsonify({"code": 200, "message": f"验证码已发送至 {app.contact_email}"})


@bp.route("/applications/<int:app_id>/verify", methods=["POST"])
@jwt_required()
@_identity_endpoint
def verify(app_id):
    """校验验证码：{code}。一次性消费；错 5 次锁死重新发送。"""
    _require_writes_open()
    actor = _actor()
    app = _load_app(actor, app_id)
    payload = request.get_json(silent=True) or {}
    ok = verification.verify_challenge(actor.user, app, payload.get("code") or "")
    db.session.commit()
    if not ok:
        return jsonify({"code": 400, "message": "验证码错误或已失效"}), 400
    return jsonify({"code": 200, "message": "邮箱验证通过，可提交审核"})


@bp.route("/applications/<int:app_id>/submit", methods=["POST"])
@jwt_required()
@_identity_endpoint
def submit(app_id):
    _require_writes_open()
    actor = _actor()
    app = _load_app(actor, app_id)
    verification.submit_application(actor.user, app)
    db.session.commit()
    return jsonify({"code": 200, "message": "已提交，等待核验负责人审核"})


@bp.route("/applications/<int:app_id>/withdraw", methods=["POST"])
@jwt_required()
@_identity_endpoint
def withdraw(app_id):
    _require_writes_open()
    actor = _actor()
    app = _load_app(actor, app_id)
    verification.withdraw_application(actor.user, app)
    db.session.commit()
    return jsonify({"code": 200, "message": "已撤回"})


# ── D3b 双账号认领与空壳归并（规格 7 章；开关独立于核验通道）─────────

def _require_link_open():
    if not config.ACCOUNT_LINK_APPLY_ENABLED or not config.IDENTITY_UI_ENABLED:
        raise AuthRejected("账号认领暂未开放，请留意公告", status=403,
                           machine="IDENTITY_DISABLED")


def _case_brief(case):
    from models import UserModel
    b = db.session.get(UserModel, case.account_b) if case.account_b else None
    return {
        "id": case.id, "state": case.state, "version": case.version,
        "has_target": bool(case.account_b),
        "target_email_masked": _mask_email(b.email) if b else None,
        "surviving_person_id": case.surviving_person_id,
        "preview_ready": case.preview_digest is not None,
        "collection_expires_at": case.collection_expires_at.strftime('%Y-%m-%d %H:%M')
        if case.collection_expires_at else None,
        "approval_expires_at": case.approval_expires_at.strftime('%Y-%m-%d %H:%M')
        if case.approval_expires_at else None,
    }


def _mask_email(email):
    local, _, domain = (email or '').partition('@')
    if not domain:
        return email
    shown = local[:2] + '***' if len(local) > 2 else local[0] + '***'
    return f'{shown}@{domain}'


@bp.route("/link-cases", methods=["GET"])
@jwt_required()
@_identity_endpoint
def my_cases():
    actor = _actor()
    from models import AccountLinkCaseModel
    rows = AccountLinkCaseModel.query.filter_by(
        account_a=actor.user.id).order_by(
        AccountLinkCaseModel.created_at.desc()).limit(50).all()
    return jsonify({"code": 200, "cases": [_case_brief(c) for c in rows]})


@bp.route("/link-cases", methods=["POST"])
@jwt_required()
@_identity_endpoint
def create_link_case():
    """发起认领（7.1.1）：actor 固定当前账号；服务器生成随机 case_id。"""
    _require_link_open()
    from services.identity import linking
    actor = _actor()
    case = linking.create_case(actor)
    db.session.commit()
    return jsonify({"code": 200, "message": "案例已创建",
                    "case": _case_brief(case)})


@bp.route("/link-cases/<case_id>", methods=["GET"])
@jwt_required()
@_identity_endpoint
def link_case_detail(case_id):
    actor = _actor()
    from services.identity import linking
    case = linking.get_case_for(actor, case_id)
    return jsonify({"code": 200, "case": _case_brief(case)})


@bp.route("/link-cases/<case_id>/prove-initiator", methods=["POST"])
@jwt_required()
@_identity_endpoint
def prove_initiator(case_id):
    """A 端近期认证证明（不替换当前登录态）。"""
    _require_link_open()
    from services.identity import linking
    actor = _actor()
    case = linking.get_case_for(actor, case_id)
    linking.prove_initiator(actor, case)
    db.session.commit()
    return jsonify({"code": 200, "message": "发起端证明完成（5 分钟内有效）"})


@bp.route("/link-cases/<case_id>/prove-target", methods=["POST"])
@jwt_required()
@_identity_endpoint
def prove_target(case_id):
    """B 端独立凭据证明：{target_email, password, totp?}。

    错误统一 422 TARGET_PROOF_FAILED——不触发 A 主会话登出（规格 11 错误契约）。
    """
    _require_link_open()
    from services.identity import linking
    actor = _actor()
    case = linking.get_case_for(actor, case_id)
    payload = request.get_json(silent=True) or {}
    linking.prove_target(
        actor, case,
        target_email=payload.get('target_email') or '',
        password=payload.get('password') or '',
        totp=payload.get('totp'))
    db.session.commit()
    return jsonify({"code": 200, "message": "目标端证明完成（5 分钟内有效）",
                    "case": _case_brief(case)})


@bp.route("/link-cases/<case_id>/preview", methods=["POST"])
@jwt_required()
@_identity_endpoint
def preview_link(case_id):
    """生成最终预览 + 10 分钟完成收据（收据仅内存保存，勿持久化）。"""
    _require_link_open()
    from services.identity import linking
    actor = _actor()
    case = linking.get_case_for(actor, case_id)
    out = linking.build_preview(actor, case)
    db.session.commit()
    return jsonify({"code": 200,
                    "case_state": out['case_state'],
                    "preview_digest": out['digest'],
                    "plan": out['plan'],
                    "authorization_expires_at":
                        out['authorization_expires_at'].strftime('%Y-%m-%d %H:%M:%S'),
                    "receipt": out['receipt']})


@bp.route("/link-cases/<case_id>/confirm", methods=["POST"])
@jwt_required()
@_identity_endpoint
def confirm_link(case_id):
    """确认执行：{preview_digest}。幂等；结果按实际状态返回。

    归并会 bump 发起端安全版本——保留的当前会话随响应换发新 access
    （同 MFA 敏感操作模式），客户端应替换内存中的 access。
    """
    _require_link_open()
    from services.identity import linking
    actor = _actor()
    case = linking.get_case_for(actor, case_id)
    payload = request.get_json(silent=True) or {}
    result = linking.confirm_link(actor, case,
                                  preview_digest=payload.get('preview_digest') or '')
    db.session.commit()
    fresh = None
    if not result['replay'] and actor.session is not None:
        from flask_jwt_extended import create_access_token
        fresh = create_access_token(
            identity=actor.user.email,
            additional_claims={"sid": actor.session.sid,
                               "security_version": actor.session.security_version,
                               "token_schema": "v2"})
    return jsonify({"code": 200, "message": "归并已完成" if not result['replay']
                    else "归并此前已完成（幂等）",
                    "fresh_access": fresh, **result})


@bp.route("/link-cases/<case_id>/withdraw", methods=["POST"])
@jwt_required()
@_identity_endpoint
def withdraw_link(case_id):
    from services.identity import linking
    actor = _actor()
    case = linking.get_case_for(actor, case_id)
    linking.withdraw_case(actor, case)
    db.session.commit()
    return jsonify({"code": 200, "message": "案例已撤回"})


@bp.route("/operation-status", methods=["POST"])
@limiter.limit("10/minute")
@_identity_endpoint
def operation_status():
    """完成收据的最小结果查询（POST body 提交收据，不入 URL；限流）。"""
    from services.identity import linking
    payload = request.get_json(silent=True) or {}
    out = linking.check_receipt(payload.get('receipt') or '')
    return jsonify({"code": 200, **out})


@bp.route("/reauth", methods=["POST"])
@jwt_required()
@limiter.limit("5/minute")
@_identity_endpoint
def reauth():
    """当前账号操作证明（规格 11）：{password}（前端 MD5 后传输，登录协议同款）。

    密码重验即实际认证——更新当前会话 auth_time（规格 6.2），不返回常规会话、
    不替换当前登录态。prove-initiator 的近期认证前置在超窗后经此恢复。
    """
    from datetime import datetime
    actor = _actor()
    payload = request.get_json(silent=True) or {}
    password = payload.get("password") or ""
    if not password or not actor.user.check_password(password):
        raise AuthRejected("密码验证失败", status=422, machine="REAUTH_FAILED")
    if actor.session is None:
        raise AuthRejected("当前会话不支持操作证明，请重新登录",
                           status=403, machine="REAUTH_REQUIRED")
    actor.session.auth_time = datetime.now()
    db.session.commit()
    return jsonify({"code": 200, "message": "已完成操作认证（5 分钟内有效）"})

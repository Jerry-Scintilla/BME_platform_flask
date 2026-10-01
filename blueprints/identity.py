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

import config
from exts import db, mail
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

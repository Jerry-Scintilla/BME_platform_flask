"""管理端身份审核 API（D3a，规格 5.1/12.2）。

端点前缀 /admin/identity。两类门槛分开：
- 队列/详情/配置读：super_admin（管理端准入，is_staff）；
- approve/reject：必须是该学校配置的核验负责人（reviewer_user_ids，
  super_admin 不旁路——审核责任人可追溯，规格 9.3/§18）；
- 配置写（域清单/审核人名单）：super_admin；reviewer_user_ids 全量替换，
  生产上线前在此补 ≥2 名真人（dev 种子 74 占位）。

审核人就绪度：schools 列表带 reviewers_ready（≥2 名）——不足不拦功能，
只在管理台标红提醒（运营前置，规格 §18）。
"""
from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required

from exts import db
from models import IdentityApplicationModel, PersonIdentityModel, UserModel
from services.auth_context import AuthRejected, current_actor
from services.identity import events, school, verification

bp = Blueprint("admin_identity", __name__, url_prefix="/admin/identity")


def _admin_endpoint(f):
    from functools import wraps

    @wraps(f)
    def wrapper(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except AuthRejected as exc:
            return exc.to_response()
        except school.IdentityError as exc:
            return jsonify({"code": 400, "message": str(exc)}), 400
    return wrapper


def _staff_actor():
    actor = current_actor()
    if not actor:
        raise AuthRejected("未认证", status=401, machine="ACCESS_TOKEN_MISSING")
    if not actor.user.is_staff():
        raise AuthRejected("仅管理员可访问", status=403, machine="FORBIDDEN")
    return actor


def _app_detail(app):
    applicant = db.session.get(UserModel, app.applicant_user_id)
    ident = PersonIdentityModel.query.filter_by(
        issuer=app.school_id, kind='netid',
        canonical_key=app.claimed_identifier).first()
    return {
        'id': app.id, 'school_id': app.school_id,
        'claimed_name': app.claimed_name,
        'claimed_identifier': app.claimed_identifier,
        'contact_email': app.contact_email,
        'status': app.status,
        'challenge_verified': bool(app.challenge_verified_at),
        'challenge_verified_at': app.challenge_verified_at.strftime('%Y-%m-%d %H:%M')
        if app.challenge_verified_at else None,
        'reviewed_by': app.reviewed_by,
        'reviewed_at': app.reviewed_at.strftime('%Y-%m-%d %H:%M') if app.reviewed_at else None,
        'reject_reason': app.reject_reason,
        'created_at': app.created_at.strftime('%Y-%m-%d %H:%M') if app.created_at else None,
        'applicant': {
            'user_id': applicant.id, 'username': applicant.username,
            'email': applicant.email, 'account_kind': applicant.account_kind,
            'person_id': applicant.person_id,
        } if applicant else None,
        # 冲突提示：该 key 是否已被登记、归属谁（仅审核视角可见，规格 8.2.4）
        'identifier_registered_to': ident.person_id if ident else None,
    }


@bp.route("/queue", methods=["GET"])
@jwt_required()
@_admin_endpoint
def queue():
    """待审队列（?school_id=&status=submitted 默认；含 draft 供负责人预检）。"""
    _staff_actor()
    status = request.args.get('status') or 'submitted'
    q = IdentityApplicationModel.query.order_by(IdentityApplicationModel.id.asc())
    if status != 'all':
        q = q.filter(IdentityApplicationModel.status == status)
    school_id = request.args.get('school_id')
    if school_id:
        q = q.filter(IdentityApplicationModel.school_id == school_id)
    rows = q.limit(200).all()
    return jsonify({"code": 200,
                    "applications": [_app_detail(a) for a in rows]})


@bp.route("/applications/<int:app_id>", methods=["GET"])
@jwt_required()
@_admin_endpoint
def detail(app_id):
    _staff_actor()
    app = db.session.get(IdentityApplicationModel, app_id)
    if app is None:
        return jsonify({"code": 404, "message": "申请不存在"}), 404
    return jsonify({"code": 200, "application": _app_detail(app)})


@bp.route("/applications/<int:app_id>/approve", methods=["POST"])
@jwt_required()
@_admin_endpoint
def approve(app_id):
    """批准：需该学校核验负责人（配置名单内）；key 冲突转可恢复 409。"""
    actor = _staff_actor()
    app = db.session.get(IdentityApplicationModel, app_id)
    if app is None:
        return jsonify({"code": 404, "message": "申请不存在"}), 404
    payload = request.get_json(silent=True) or {}
    verification.approve_application(app, actor.user, note=payload.get('note') or '')
    db.session.commit()
    return jsonify({"code": 200, "message": "已核验通过",
                    "application": _app_detail(app)})


@bp.route("/applications/<int:app_id>/reject", methods=["POST"])
@jwt_required()
@_admin_endpoint
def reject(app_id):
    actor = _staff_actor()
    app = db.session.get(IdentityApplicationModel, app_id)
    if app is None:
        return jsonify({"code": 404, "message": "申请不存在"}), 404
    payload = request.get_json(silent=True) or {}
    verification.reject_application(app, actor.user, reason=payload.get('reason') or '')
    db.session.commit()
    return jsonify({"code": 200, "message": "已驳回",
                    "application": _app_detail(app)})


@bp.route("/schools", methods=["GET"])
@jwt_required()
@_admin_endpoint
def schools():
    """学校配置清单（含审核人就绪度与名单——审核人姓名仅管理端可见）。"""
    _staff_actor()
    from models import IdentitySchoolConfigModel
    out = []
    for c in IdentitySchoolConfigModel.query.order_by(
            IdentitySchoolConfigModel.school_id).all():
        reviewers = school.resolve_reviewers(c)
        out.append({
            'school_id': c.school_id, 'name': c.name,
            'personal_email_domains': c.personal_email_domains,
            'excluded_email_domains': c.excluded_email_domains,
            'email_local_matches_identifier': bool(c.email_local_matches_identifier),
            'config_version': c.config_version,
            'reviewers': [{'user_id': u.id, 'username': u.username} for u in reviewers],
            'reviewers_ready': c.reviewers_ready,
        })
    return jsonify({"code": 200, "schools": out})


@bp.route("/schools/<school_id>/config", methods=["PUT"])
@jwt_required()
@_admin_endpoint
def update_config(school_id):
    """改学校配置（config_version+1；不追溯改写旧记录）。

    reviewer_user_ids 传全量名单；生产上线前在此补 ≥2 名真人。
    """
    actor = _staff_actor()
    cfg = school.get_school_config(school_id)
    payload = request.get_json(silent=True) or {}
    school.update_school_config(
        cfg,
        name=payload.get('name'),
        personal_email_domains=payload.get('personal_email_domains'),
        excluded_email_domains=payload.get('excluded_email_domains'),
        email_local_matches_identifier=payload.get('email_local_matches_identifier'),
        reviewer_user_ids=payload.get('reviewer_user_ids'))
    events.record_event(
        'identity.school_config.update', actor_user_id=actor.user.id,
        target_ids=None,
        evidence_refs={'note': f'school={school_id} v{cfg.config_version}'},
        reason='管理端更新学校核验配置')
    db.session.commit()
    return jsonify({"code": 200, "message": "配置已更新",
                    "config_version": cfg.config_version,
                    "reviewers_ready": cfg.reviewers_ready})

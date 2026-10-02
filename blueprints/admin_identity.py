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

from exts import db, mail
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
    actor = _require_identity_role('identity_review')
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
    actor = _require_identity_role('identity_review')
    app = db.session.get(IdentityApplicationModel, app_id)
    if app is None:
        return jsonify({"code": 404, "message": "申请不存在"}), 404
    payload = request.get_json(silent=True) or {}
    verification.reject_application(app, actor.user, reason=payload.get('reason') or '')
    db.session.commit()
    return jsonify({"code": 200, "message": "已驳回",
                    "application": _app_detail(app)})


@bp.route("/link-cases", methods=["GET"])
@jwt_required()
@_admin_endpoint
def link_case_queue():
    """关联案例队列（?state=awaiting_review 默认）。"""
    _staff_actor()
    from models import AccountLinkCaseModel
    state = request.args.get('state') or 'awaiting_review'
    q = AccountLinkCaseModel.query
    if state != 'all':
        q = q.filter(AccountLinkCaseModel.state == state)
    rows = q.order_by(AccountLinkCaseModel.created_at.asc()).limit(200).all()
    from services.identity import linking
    out = []
    for c in rows:
        b = db.session.get(UserModel, c.account_b) if c.account_b else None
        scan = linking.shell_scan_summary(b) if b else {'is_shell': False,
                                                        'blockers': ['no_target']}
        _b = db.session.get(UserModel, c.account_b) if c.account_b else None
        out.append({
            'id': c.id, 'state': c.state, 'version': c.version,
            'account_a': c.account_a, 'account_b': c.account_b,
            'b_lifecycle': _b.lifecycle if _b else None,
            'surviving_person_id': c.surviving_person_id,
            'created_at': c.created_at.strftime('%Y-%m-%d %H:%M') if c.created_at else None,
            'scan': scan,
        })
    return jsonify({"code": 200, "cases": out})


@bp.route("/link-cases/<case_id>/decision", methods=["POST"])
@jwt_required()
@_admin_endpoint
def link_case_decision(case_id):
    """案例审核决策：{decision: approved|rejected, reason}。

    审核人须非当事人；有阻断项（特权/扫描异常）需 ≥2 名不同审核人批准。
    """
    actor = _require_identity_role('identity_review')
    from models import AccountLinkCaseModel
    from services.identity import linking
    case = db.session.get(AccountLinkCaseModel, case_id)
    if case is None:
        return jsonify({"code": 404, "message": "案例不存在"}), 404
    payload = request.get_json(silent=True) or {}
    linking.review_case(case, actor.user,
                         decision=payload.get('decision') or '',
                         reason=payload.get('reason') or '')
    db.session.commit()
    return jsonify({"code": 200, "message": "决策已记录", "state": case.state})


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
    actor = _require_identity_role('identity_admin')
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


# ── D3c 外校名册管理（规格 5.2）──────────────────────────────────

@bp.route("/roster", methods=["GET"])
@jwt_required()
@_admin_endpoint
def roster_list():
    """名册列表（?school_id= 过滤；含认领状态）。"""
    _staff_actor()
    from models import IdentityRosterModel, IdentitySchoolConfigModel
    q = IdentityRosterModel.query
    school_id = request.args.get('school_id')
    if school_id:
        q = q.filter_by(school_id=school_id)
    rows = q.order_by(IdentityRosterModel.id.desc()).limit(300).all()
    names = {c.school_id: c.name for c in IdentitySchoolConfigModel.query.all()}
    return jsonify({"code": 200, "roster": [{
        'id': r.id, 'school_id': r.school_id, 'school_name': names.get(r.school_id, r.school_id),
        'roster_ref': r.roster_ref, 'name': r.name,
        'contact_email': r.contact_email, 'institution_id': r.institution_id,
        'claimed_person_id': r.claimed_person_id, 'status': r.status,
    } for r in rows]})


@bp.route("/roster/import", methods=["POST"])
@jwt_required()
@_admin_endpoint
def roster_import():
    """导入/更新名册：{school_id, rows:[{roster_ref,name,contact_email,institution_id?}],
    dry_run?}——按 (school, roster_ref) upsert，人员不因新批次重建（规格 5.2）。"""
    actor = _staff_actor()
    payload = request.get_json(silent=True) or {}
    from services.identity import roster
    out = roster.import_roster(
        actor.user, school_id=payload.get('school_id') or '',
        rows=payload.get('rows') or [], dry_run=bool(payload.get('dry_run')))
    return jsonify({"code": 200, **out,
                    "message": "干跑完成，确认后去掉 dry_run 提交" if payload.get('dry_run')
                    else f"导入完成：新建 {out['created']} / 更新 {out['updated']}"})


@bp.route("/roster/<int:roster_id>/invite", methods=["POST"])
@jwt_required()
@_admin_endpoint
def roster_invite(roster_id):
    """发认领邀请（一次性令牌 7 天，邮件链接）——旧未用令牌作废。"""
    actor = _staff_actor()
    from services.identity import roster
    invite, link = roster.issue_invite(actor.user, roster_id)
    from models import IdentityRosterModel
    entry = db.session.get(IdentityRosterModel, roster_id)
    try:
        from flask_mail import Message
        mail.send(Message(
            subject='BME 外校身份认领邀请',
            recipients=[entry.contact_email],
            body=f'你好 {entry.name}：\n请使用以下链接在 BME 平台完成身份认领'
                 f'（7 天内有效，一次性使用）：\n{link}\n'
                 f'如非本人请忽略本邮件。'))
    except Exception:
        db.session.rollback()
        return jsonify({"code": 503, "message": '邮件发送失败，邀请未发出'}), 503
    db.session.commit()
    return jsonify({"code": 200, "message": "邀请已发送",
                    "invite_link": link})


# ── D3c 恢复案例管理（规格 7.4）──────────────────────────────────

@bp.route("/recovery-cases", methods=["GET"])
@jwt_required()
@_admin_endpoint
def recovery_queue():
    _staff_actor()
    from models import IdentityRecoveryCaseModel
    status = request.args.get('status') or 'submitted'
    q = IdentityRecoveryCaseModel.query
    if status != 'all':
        q = q.filter(IdentityRecoveryCaseModel.status == status)
    rows = q.order_by(IdentityRecoveryCaseModel.id.desc()).limit(200).all()
    return jsonify({"code": 200, "cases": [{
        'id': c.id, 'kind': c.kind,
        'target_email': c.target_email, 'contact_email': c.contact_email,
        'contact_verified': bool(c.contact_verified_at),
        'statement': c.statement, 'status': c.status, 'require_two': bool(c.require_two),
        'cooldown_until': c.cooldown_until.strftime('%Y-%m-%d %H:%M') if c.cooldown_until else None,
        'decision_note': c.decision_note,
        'created_at': c.created_at.strftime('%Y-%m-%d %H:%M') if c.created_at else None,
    } for c in rows]})


@bp.route("/recovery-cases/<int:case_id>/decision", methods=["POST"])
@jwt_required()
@_admin_endpoint
def recovery_decision(case_id):
    """决策：{decision: approved|rejected, note}。特权案例需两名不同审核人。"""
    actor = _require_identity_role('identity_recover')
    from models import IdentityRecoveryCaseModel
    from services.identity import recovery
    case = db.session.get(IdentityRecoveryCaseModel, case_id)
    if case is None:
        return jsonify({"code": 404, "message": "案例不存在"}), 404
    payload = request.get_json(silent=True) or {}
    recovery.decide_case(case, actor.user,
                         decision=payload.get('decision') or '',
                         note=payload.get('note') or '')
    db.session.commit()
    return jsonify({"code": 200, "state": case.status,
                    "message": "已记录（待第二复核人）" if case.status == 'submitted'
                    else "已决策"})


@bp.route("/recovery-cases/<int:case_id>/complete", methods=["POST"])
@jwt_required()
@_admin_endpoint
def recovery_complete(case_id):
    """冷静期届满后标记执行完毕（凭据重置/撤会话在既有运维通道完成）。"""
    actor = _require_identity_role('identity_recover')
    from models import IdentityRecoveryCaseModel
    from services.identity import recovery
    case = db.session.get(IdentityRecoveryCaseModel, case_id)
    if case is None:
        return jsonify({"code": 404, "message": "案例不存在"}), 404
    recovery.complete_case(case, actor.user)
    db.session.commit()
    return jsonify({"code": 200, "state": case.status})


# ── 岗位能力权限位（P2-10，规格 11）───────────────────────────────
# super_admin 直通；普通账号可经 UserPermission 授予（营期老师按范围审核等）。
def _require_identity_role(permission_name):
    actor = _staff_actor()
    from models import PermissionModel, UserPermissionModel
    if actor.user.is_admin():
        return actor
    perm = PermissionModel.query.filter_by(name=permission_name).first()
    if perm and UserPermissionModel.query.filter_by(
            user_id=actor.user.id, permission_id=perm.id).first():
        return actor
    raise AuthRejected(f"需要 {permission_name} 权限（或管理员）",
                       status=403, machine="FORBIDDEN")


# ── 辅助账号授权（P2-9，规格 9.3）────────────────────────────────
@bp.route("/auxiliary-grants", methods=["GET"])
@jwt_required()
@_admin_endpoint
def auxiliary_list():
    """辅助账号授权列表（含临期/过期状态）。"""
    _require_identity_role('identity_admin')
    from services.identity import auxiliary
    return jsonify({"code": 200, "grants": auxiliary.list_grants()})


@bp.route("/auxiliary-grants", methods=["POST"])
@jwt_required()
@_admin_endpoint
def auxiliary_approve():
    """批准辅助账号：{user_id, purpose, scope?, days?}（默认 90 天复核）。"""
    actor = _require_identity_role('identity_admin')
    payload = request.get_json(silent=True) or {}
    from services.identity import auxiliary
    row = auxiliary.approve_grant(
        actor.user, user_id=payload.get('user_id'),
        purpose=payload.get('purpose') or '',
        scope=payload.get('scope'),
        days=int(payload.get('days') or auxiliary.GRANT_DAYS))
    db.session.commit()
    return jsonify({"code": 200, "message": "辅助账号已批准（到期实时失效）",
                    "id": row.id})


@bp.route("/auxiliary-grants/<int:grant_id>/revoke", methods=["POST"])
@jwt_required()
@_admin_endpoint
def auxiliary_revoke(grant_id):
    actor = _require_identity_role('identity_admin')
    payload = request.get_json(silent=True) or {}
    from services.identity import auxiliary
    auxiliary.revoke_grant(actor.user, grant_id, reason=payload.get('reason') or '')
    db.session.commit()
    return jsonify({"code": 200, "message": "已撤销"})


@bp.route("/link-cases/<case_id>/freeze", methods=["POST"])
@jwt_required()
@_admin_endpoint
def link_case_freeze(case_id):
    """续办期满冻结（7.3）：非空壳归并的 B 账号在交接完成后转 merged+撤会话。"""
    actor = _require_identity_role('identity_review')
    from models import AccountLinkCaseModel
    from services.identity import linking
    case = db.session.get(AccountLinkCaseModel, case_id)
    if case is None:
        return jsonify({"code": 404, "message": "案例不存在"}), 404
    linking.freeze_merged_account(case, actor.user)
    db.session.commit()
    return jsonify({"code": 200, "message": "已冻结（merged+撤会话+撤续办宽限）"})

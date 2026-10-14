"""Training identity provider for the independent workbench, separately provisioned."""
import base64
import hashlib
import hmac
import os
import re
import secrets
from datetime import date, datetime, timedelta
from functools import wraps
from urllib.parse import urlencode

from flask import Blueprint, current_app, jsonify, request
from flask_jwt_extended import jwt_required
from sqlalchemy import select, update
from exts import db, limiter
from models import AuthSessionModel, ClubGroup, ClubMembership, ClubOfficer, ClubPosition, ClubOrgEvent, PersonModel, UserModel
from services.club_history import canonical_person_id
from services.auth_context import AuthRejected, resolve_actor
from workbench_sso_models import WorkbenchSSOCode, WorkbenchAdmission

bp = Blueprint('workbench_sso', __name__, url_prefix='/workbench-sso')


def setting(name, default=''):
    return current_app.config.get(name, os.getenv(name, default))


def configured():
    return (str(setting('WORKBENCH_SSO_ENABLED', 'false')).lower() == 'true'
            and all(setting(k) for k in ('WORKBENCH_CLIENT_ID', 'WORKBENCH_CLIENT_SECRET', 'WORKBENCH_REDIRECT_URI')))


def failure(code, message, status):
    return jsonify(error=code, message=message), status


def client_required(func):
    @wraps(func)
    def wrapped(*args, **kwargs):
        if not configured():
            return failure('UNAVAILABLE', '工作台接入未开启', 503)
        auth = request.authorization
        if (not auth or auth.type.lower() != 'basic'
                or not hmac.compare_digest((auth.username or '').encode(), setting('WORKBENCH_CLIENT_ID').encode())
                or not hmac.compare_digest((auth.password or '').encode(), setting('WORKBENCH_CLIENT_SECRET').encode())):
            return failure('INVALID_CLIENT', '客户端认证失败', 401)
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or body.get('audience') != setting('WORKBENCH_CLIENT_ID'):
            return failure('INVALID_AUDIENCE', '客户端用途不匹配', 400)
        return func(*args, **kwargs)
    return wrapped


def identity(user):
    person = db.session.get(PersonModel, user.person_id) if user and user.person_id else None
    active = bool(user and user.status != 'banned' and (user.lifecycle or 'active') == 'active'
                  and user.account_kind != 'service')
    person_active = bool(person and person.record_status == 'active'
                         and person.verification_status not in ('disputed', 'revoked'))
    today = date.today()
    officer = (ClubOfficer.query.filter_by(user_id=user.id, status='active')
               .filter(ClubOfficer.term_start <= today,
                       (ClubOfficer.term_end.is_(None)) | (ClubOfficer.term_end >= today)).first()) if user else None
    membership = (db.session.query(ClubMembership.id).join(ClubGroup, ClubGroup.id == ClubMembership.group_id)
                  .filter(ClubMembership.user_id == user.id, ClubGroup.status == 'active').first()) if user else None
    enrollment = db.session.get(WorkbenchAdmission, user.id) if user else None
    internal = bool(enrollment and enrollment.status == 'active' and user.person_id == enrollment.person_id
                    and person_active and person.verification_status == 'verified')
    return {
        'issuer': setting('WORKBENCH_ISSUER', 'bme-training'),
        'audience': setting('WORKBENCH_CLIENT_ID'),
        'subject': str(user.id), 'person_id': str(user.person_id or 'missing'),
        'display_name': (person.verified_name if person and person.verified_name else user.username).strip()[:100],
        'security_version': str(user.security_version or 0),
        'account_active': active, 'person_active': person_active,
        'community_active': bool(officer or membership or internal),
    }


def admitted(claims):
    return all(claims[k] for k in ('account_active', 'person_active', 'community_active'))


def directory_snapshot():
    """Minimal organization projection, no passwords, tokens, email or education data."""
    groups = ClubGroup.query.all()
    positions = ClubPosition.query.all()
    officers = ClubOfficer.query.all()
    memberships = ClubMembership.query.all()
    events = ClubOrgEvent.query.filter(ClubOrgEvent.officer_id.in_([o.id for o in officers])).all()
    ids = {o.user_id for o in officers} | {m.user_id for m in memberships} | {e.operator_id for e in events if e.operator_id}
    ids.update(a.user_id for a in WorkbenchAdmission.query.all())
    def rows(items):
        return [{c.name: (getattr(row, c.name).isoformat() if isinstance(getattr(row, c.name), (date, datetime))
                         else getattr(row, c.name)) for c in row.__table__.columns} for row in items]
    users = []
    for user in UserModel.query.filter(UserModel.id.in_(ids)).all() if ids else []:
        person = db.session.get(PersonModel, user.person_id) if user.person_id else None
        display_name = person.verified_name if person and person.verification_status == 'verified' and person.verified_name else user.username
        users.append({'id': user.id, 'username': display_name, 'person_id': canonical_person_id(user.person_id),
                      'avatar_url': user.avatar_url, 'status': user.status or 'active', 'lifecycle': user.lifecycle or 'active',
                      'verification_status': person.verification_status if person else 'unverified'})
    projected_officers = rows(officers)
    for row in projected_officers:
        user = db.session.get(UserModel, row['user_id'])
        row['person_id'] = canonical_person_id(row['person_id'] or (user.person_id if user else None))
    return {'groups': rows(groups), 'positions': rows(positions), 'officers': projected_officers,
            'memberships': rows(memberships), 'users': users, 'officer_events': rows(events)}


@bp.get('/config')
def public_config():
    return jsonify(enabled=bool(configured()), workbench_url=setting('WORKBENCH_PUBLIC_URL'))


@bp.post('/authorize')
@jwt_required()
@limiter.limit('10/minute')
def authorize():
    if not configured():
        return failure('UNAVAILABLE', '工作台接入未开启', 503)
    body = request.get_json(silent=True) or {}
    if (body.get('client_id') != setting('WORKBENCH_CLIENT_ID')
            or body.get('redirect_uri') != setting('WORKBENCH_REDIRECT_URI')
            or body.get('response_type') != 'code' or body.get('scope') != 'workbench:access'
            or body.get('code_challenge_method') != 'S256'
            or not re.fullmatch(r'[A-Za-z0-9_-]{43}', str(body.get('code_challenge', '')))
            or not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', str(body.get('state', '')))):
        return failure('INVALID_REQUEST', '登录请求不符合已登记的工作台配置', 400)
    try:
        actor = resolve_actor(required=True)
    except AuthRejected as exc:
        return exc.to_response()
    if not actor.session:
        return failure('LOGIN_REQUIRED', '请重新登录训练营后再进入工作台', 401)
    claims = identity(actor.user)
    if not admitted(claims):
        return failure('ADMISSION_DENIED', '需要有效社团身份才能进入工作台', 403)
    raw = secrets.token_urlsafe(32)
    WorkbenchSSOCode.query.filter(WorkbenchSSOCode.expires_at < datetime.now()-timedelta(days=1)).delete(synchronize_session=False)
    db.session.add(WorkbenchSSOCode(digest=hashlib.sha256(raw.encode()).hexdigest(),
        user_id=actor.user.id, session_id=actor.session.sid, person_id=actor.user.person_id,
        security_version=actor.user.security_version or 0, challenge=body['code_challenge'],
        redirect_uri=body['redirect_uri'], expires_at=datetime.now() + timedelta(seconds=90)))
    db.session.commit()
    return jsonify(redirect_url=body['redirect_uri'] + '?' + urlencode({'code': raw, 'state': body['state']}))


@bp.post('/exchange')
@client_required
@limiter.limit('60/minute')
def exchange():
    body = request.get_json()
    code, verifier = body.get('code'), body.get('code_verifier')
    if (not isinstance(code, str) or not 32 <= len(code) <= 128
            or not isinstance(verifier, str) or not re.fullmatch(r'[A-Za-z0-9._~-]{43,128}', verifier)
            or body.get('grant_type') != 'authorization_code'
            or body.get('redirect_uri') != setting('WORKBENCH_REDIRECT_URI')):
        return jsonify(error='INVALID_CODE'), 400
    digest = hashlib.sha256(code.encode()).hexdigest()
    row = db.session.get(WorkbenchSSOCode, digest)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    if (not row or row.consumed_at or row.expires_at <= datetime.now()
            or row.redirect_uri != body['redirect_uri'] or not hmac.compare_digest(row.challenge, challenge)):
        return jsonify(error='INVALID_CODE'), 400
    changed = db.session.execute(update(WorkbenchSSOCode).where(WorkbenchSSOCode.digest == digest,
        WorkbenchSSOCode.consumed_at.is_(None), WorkbenchSSOCode.expires_at > datetime.now()).values(consumed_at=datetime.now()))
    if changed.rowcount != 1:
        db.session.rollback()
        return jsonify(error='INVALID_CODE'), 400
    uid, sid, pid, version = row.user_id, row.session_id, row.person_id, row.security_version
    db.session.commit()
    user, session = db.session.get(UserModel, uid), db.session.get(AuthSessionModel, sid)
    if (not user or not session or session.revoked_at or session.expires_at <= datetime.now()
            or session.user_id != uid or user.person_id != pid
            or (user.security_version or 0) != version or session.security_version != version):
        return jsonify(error='INVALID_CODE'), 400
    claims = identity(user)
    if not admitted(claims):
        return jsonify(error='INVALID_CODE'), 400
    claims['directory'] = directory_snapshot()
    return jsonify(claims)


@bp.post('/status')
@client_required
def current_status():
    body = request.get_json()
    subject = body.get('subject')
    if (body.get('issuer') != setting('WORKBENCH_ISSUER', 'bme-training')
            or not isinstance(subject, str) or not re.fullmatch(r'[1-9][0-9]{0,10}', subject)):
        return failure('INVALID_SUBJECT', '人员标识无效', 400)
    user = db.session.get(UserModel, int(subject))
    if not user:
        return jsonify(issuer=setting('WORKBENCH_ISSUER', 'bme-training'), audience=setting('WORKBENCH_CLIENT_ID'),
            subject=subject, person_id='missing', display_name='已失效账号', security_version='deleted',
            account_active=False, person_active=False, community_active=False)
    claims = identity(user)
    if admitted(claims):
        claims['directory'] = directory_snapshot()
    return jsonify(claims)


@bp.after_request
def private_response(response):
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Referrer-Policy'] = 'no-referrer'
    return response


@bp.post('/directory')
@client_required
def directory():
    return jsonify(directory=directory_snapshot())


@bp.post('/candidates')
@client_required
def member_candidates():
    from services.identity.matcher import resolve_batch
    names = request.get_json().get('names')
    if not isinstance(names, list) or not names or len(names) > 200 or any(not isinstance(n, str) or len(n) > 100 for n in names):
        return failure('INVALID_NAMES', '请提供至多200个姓名', 400)
    return jsonify(matches=resolve_batch([{'real_name': n} for n in names]))


@bp.post('/admission')
@client_required
def admit_internal_member():
    body = request.get_json()
    subject, person_id = body.get('subject'), body.get('person_id')
    operator, reason = body.get('operator_subject'), str(body.get('reason') or '').strip()
    if (body.get('issuer') != setting('WORKBENCH_ISSUER', 'bme-training')
            or any(not isinstance(v, str) or not re.fullmatch(r'[1-9][0-9]{0,10}', v) for v in (subject, person_id, operator))
            or not reason or len(reason) > 200):
        return failure('INVALID_ADMISSION', '准入登记参数无效', 400)
    user = db.session.get(UserModel, int(subject))
    actor = db.session.get(UserModel, int(operator))
    person = db.session.get(PersonModel, user.person_id) if user else None
    if (not user or str(user.person_id) != person_id or not person
            or person.record_status != 'active' or person.verification_status != 'verified'
            or user.status != 'active' or (user.lifecycle or 'active') != 'active'
            or (user.account_kind or 'standard') != 'standard'
            or not actor or actor.status != 'active' or (actor.lifecycle or 'active') != 'active'):
        return failure('VERIFIED_MEMBER_REQUIRED', '仅可登记身份一致且已认证的有效普通账号', 403)
    row = db.session.get(WorkbenchAdmission, user.id)
    if row and row.person_id != user.person_id:
        return failure('PERSON_CHANGED', '账号人员身份已变化，需人工处理原准入', 409)
    if row is None:
        row = WorkbenchAdmission(user_id=user.id, person_id=user.person_id, granted_by=actor.id,
            reason=reason, status='active', created_at=datetime.now())
        db.session.add(row)
        from services.work.events import record_org_event
        record_org_event('workbench_admitted', user_id=user.id, operator_id=actor.id,
                         detail={'reason': reason, 'client': setting('WORKBENCH_CLIENT_ID')})
    elif row.status != 'active':
        return failure('ADMISSION_REVOKED', '准入已撤销，普通成员导入不能恢复', 409)
    db.session.commit()
    claims = identity(user)
    claims['directory'] = directory_snapshot()
    return jsonify(claims)


@bp.get('/me')
@jwt_required()
def workbench_access():
    try:
        actor=resolve_actor(required=True)
    except AuthRejected as exc:
        return exc.to_response()
    claims=identity(actor.user)
    return jsonify(code=200,data={'eligibility':{'kind':'club'} if admitted(claims) else None,
        'external_url':setting('WORKBENCH_PUBLIC_URL') if configured() else None,
        'workspaces':[], 'is_governance':False, 'todo':{}})


@bp.post('/projection')
@client_required
def projection():
    body = request.get_json()
    subject = body.get('subject')
    if not isinstance(subject, str) or not re.fullmatch(r'[1-9][0-9]{0,10}', subject):
        return failure('INVALID_SUBJECT', '人员标识无效', 400)
    user = db.session.get(UserModel, int(subject))
    if not user or not admitted(identity(user)):
        return jsonify(exists=False, projection=None)
    from services.work.projections import get_provider
    import services.work.adapters
    source_id = body.get('source_id')
    provider = get_provider(body.get('source_type'))
    if not provider or type(source_id) is not int or source_id < 1:
        return failure('INVALID_OBJECT', '对象类型或标识无效', 400)
    return jsonify(exists=provider.exists(source_id), projection=provider.summarize(user, source_id))

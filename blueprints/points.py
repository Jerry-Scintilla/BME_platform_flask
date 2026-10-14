"""个人积分与营期奖励管理。所有身份和管理范围均在服务端复核。"""
from datetime import datetime, timedelta
from functools import wraps
import re
from flask import Blueprint, jsonify, request, current_app
from flask_jwt_extended import jwt_required
from exts import db
from models import UserModel, CampSession, CampMember, CampStaff
from points_models import PointsRule, PointsRuleVersion, PointsReward, PointsEvent
from services import points as service
from services.points_identity import resolve_points_identity
from services.camp_groups import training_access
import points_center_client as center
from . import _current_user
from .camp_staff import has_camp_access
from .points_sso import _upstream_error_response

bp = Blueprint('points', __name__, url_prefix='/points')


@bp.after_request
def no_cache(response):
    response.headers['Cache-Control'] = 'no-store'
    return response


@bp.errorhandler(service.PointsError)
def handle_error(exc):
    db.session.rollback()
    return jsonify(code=exc.status, message=exc.message), exc.status


def rewards_enabled(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not current_app.config.get('POINTS_REWARDS_ENABLED'):
            raise service.PointsError('积分奖励暂未开放', 503)
        return fn(*args, **kwargs)
    return wrapped


def actor():
    user = _current_user()
    if not user:
        raise service.PointsError('请先登录', 401)
    if user.status != 'active' or user.lifecycle != 'active':
        raise service.PointsError('账号不可用', 403)
    return user


def manager_ids(user):
    if user.is_admin():
        return [c.id for c in CampSession.query.filter(CampSession.status != 'deleted')]
    ids = {s.camp_session_id for s in CampStaff.query.filter_by(user_id=user.id, status='active')}
    ids.update(m.camp_session_id for m in CampMember.query.filter_by(user_id=user.id) if m.is_mentor)
    return sorted(ids)


def require_camp(user, sid, permission='propose', target=None):
    camp = db.session.get(CampSession, sid)
    if not camp:
        raise service.PointsError('营期不存在', 404)
    if camp.status in ('archived', 'deleted') or camp.training_migration_paused:
        raise service.PointsError('营期当前只读', 409)
    allowed = user.is_admin() if permission == 'rules' else has_camp_access(user, sid, 'reward.issue')
    if not allowed and permission == 'propose' and target is not None:
        allowed = target in training_access.taught_students(sid, user.id)
    if not allowed:
        raise service.PointsError('无权操作该营期积分', 403)
    return camp


def positive(value, name, maximum=10000):
    if type(value) is not int or value < 1 or value > maximum:
        raise service.PointsError(f'{name}必须为 1–{maximum} 的整数')
    return value


def body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise service.PointsError('请求格式无效')
    return data


def note(data, key='reason'):
    value = data.get(key)
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 512:
        raise service.PointsError('请填写 1–512 字的原因或依据')
    return value.strip()


@bp.get('/me')
@jwt_required()
def me():
    user = actor()
    identity = resolve_points_identity(user)
    result = dict(code=200, status='unavailable', balance=None,
        rewards_enabled=bool(current_app.config.get('POINTS_REWARDS_ENABLED')),
        store_history_available=False)
    if not identity.eligible:
        return jsonify(**result, message=identity.message, error_code=identity.error_code, needs_identity=True)
    if not current_app.config.get('POINTS_CENTER_ENABLED'):
        return jsonify(**result, message='积分商城暂未开放')
    try:
        account = center.get_user_points(str(user.id).zfill(7), identity.email)
        result.update(status='ready', balance=account['balance'], updated_at=datetime.now().isoformat())
    except center.PointsCenterError as exc:
        if exc.category == 'HTTP' and exc.status_code == 404:
            result['status'] = 'unlinked'
        else:
            return _upstream_error_response('points-read', user.id, exc)
    return jsonify(**result)


@bp.post('/connect')
@jwt_required()
def connect():
    user = actor(); identity = resolve_points_identity(user)
    if not identity.eligible:
        raise service.PointsError(identity.message, identity.status)
    if not current_app.config.get('POINTS_CENTER_ENABLED'):
        raise service.PointsError('积分商城暂未开放', 503)
    try:
        center.ensure_user(identity.email, str(user.id).zfill(7))
    except center.PointsCenterError as exc:
        return _upstream_error_response('points-connect', user.id, exc)
    return jsonify(code=200)


@bp.get('/tasks')
@jwt_required()
def tasks():
    actor()
    # 平台常驻途径尚未配置分值；目录不与任何营期规则混用，也不触发发放。
    return jsonify(code=200, scope='platform', items=[
        dict(id='daily_checkin', scope='platform', kind='daily_checkin', title='每日打卡',
             enabled=False, amount=None, status='unconfigured', frequency='每日任务',
             help='平台每日打卡积分规则尚未配置，开放后展示奖励和上限。'),
        dict(id='community_post', scope='platform', kind='community_post', title='发布帖子',
             enabled=False, amount=None, status='unconfigured', frequency='社区参与',
             help='社区发帖积分规则尚未配置，开放后展示有效发帖条件和奖励上限。'),
    ])


def readable_camp(user, sid):
    camp = db.session.get(CampSession, sid)
    if not camp or camp.status == 'deleted':
        raise service.PointsError('营期不存在', 404)
    member = CampMember.query.filter_by(camp_session_id=sid, user_id=user.id).first()
    if not member and not has_camp_access(user, sid, 'reward.issue'):
        raise service.PointsError('无权查看该营期积分', 403)
    return camp


@bp.get('/camps/<int:sid>/tasks')
@jwt_required()
def camp_tasks(sid):
    user = actor()
    camp = readable_camp(user, sid)
    if not current_app.config.get('POINTS_REWARDS_ENABLED'):
        return jsonify(code=200, items=[], enabled=False)
    items = []
    for r in PointsRule.query.filter_by(camp_id=sid).all():
        data = service.rule_data(r)
        if not data or not data['enabled']:
            continue
        data['scope'] = 'camp'
        data['action_path'] = f'/camp?sid={sid}&tab='+('attendance' if r.kind == 'attendance' else 'study')
        if camp.status == 'archived': data['action_path'] = None
        items.append(data)
    return jsonify(code=200, items=items, enabled=True)


@bp.get('/camps/<int:sid>/rewards')
@jwt_required()
def camp_rewards(sid):
    user = actor()
    readable_camp(user, sid)
    if not current_app.config.get('POINTS_REWARDS_ENABLED'):
        return jsonify(code=200, items=[], total=0, enabled=False)
    q = PointsReward.query.filter_by(user_id=user.id, camp_id=sid)
    page = max(1, request.args.get('page', 1, type=int))
    rows = q.order_by(PointsReward.id.desc()).offset((page-1)*20).limit(20).all()
    return jsonify(code=200, items=[service.reward_data(r) for r in rows], total=q.count(), enabled=True)


@bp.get('/records')
@jwt_required()
def records():
    user = actor()
    if not current_app.config.get('POINTS_REWARDS_ENABLED'):
        return jsonify(code=200, items=[], total=0, scope='training')
    q = PointsReward.query.filter_by(user_id=user.id, status='completed')
    kind = request.args.get('kind', 'all')
    if kind == 'earned': q = q.filter(PointsReward.amount > 0)
    elif kind == 'spent': q = q.filter(PointsReward.amount < 0)
    elif kind != 'all': raise service.PointsError('记录类型无效')
    page = max(1, request.args.get('page', 1, type=int))
    total = q.count()
    rows = q.order_by(PointsReward.completed_at.desc(), PointsReward.id.desc()).offset((page-1)*20).limit(20).all()
    return jsonify(code=200, items=[service.reward_data(r) for r in rows], total=total, scope='training')


@bp.get('/pending')
@jwt_required()
def pending():
    # 兼容旧入口必须显式提供营期，不再提供个人中心的跨营待办汇总。
    sid = request.args.get('camp_id', type=int)
    if not sid:
        raise service.PointsError('请选择营期')
    user = actor()
    readable_camp(user, sid)
    if not current_app.config.get('POINTS_REWARDS_ENABLED'):
        return jsonify(code=200, items=[])
    rows = PointsReward.query.filter(PointsReward.user_id == user.id, PointsReward.camp_id == sid,
        PointsReward.status != 'completed').order_by(PointsReward.id.desc()).limit(100)
    return jsonify(code=200, items=[service.reward_data(r) for r in rows])


@bp.get('/manage/meta')
@jwt_required()
@rewards_enabled
def meta():
    user = actor()
    ids = manager_ids(user)
    if 'camp_id' in request.args:
        sid = request.args.get('camp_id', type=int)
        if sid not in ids:
            raise service.PointsError('无权管理该营期积分', 403)
        ids = [sid]
    camps = CampSession.query.filter(CampSession.id.in_(ids)).all()
    return jsonify(code=200, camps=[dict(id=c.id, name=c.name,
        can_review=has_camp_access(user, c.id, 'reward.issue'), can_rules=user.is_admin(),
        readonly=c.status in ('archived', 'deleted') or c.training_migration_paused) for c in camps],
        kinds=[dict(value=k, label=v) for k,v in service.KINDS.items()])


@bp.get('/manage/rules')
@jwt_required()
@rewards_enabled
def rule_list():
    user = actor(); sid = request.args.get('camp_id', type=int)
    if sid not in manager_ids(user): raise service.PointsError('无权查看', 403)
    return jsonify(code=200, items=[service.rule_data(r, latest=True) for r in PointsRule.query.filter_by(camp_id=sid)])


@bp.post('/manage/rules')
@jwt_required()
@rewards_enabled
def save_rule():
    user = actor(); data = body(); sid = positive(data.get('camp_id'), '营期')
    camp = require_camp(user, sid, 'rules'); kind = data.get('kind')
    if kind not in service.KINDS: raise service.PointsError('奖励类型无效')
    from .camp import _attendance_mode
    if kind == 'attendance' and _attendance_mode(camp) != 'daily':
        raise service.PointsError('该营不是每日承诺考勤，不能启用每日达标奖励')
    if kind == 'chapter' and camp.category != 'learning':
        raise service.PointsError('章节认证奖励仅适用于培训营')
    amount = positive(data.get('amount'), '积分', 10000)
    limit = positive(data.get('period_limit', 1), '次数', 100) if kind == 'contribution' else 1
    if type(data.get('enabled')) is not bool: raise service.PointsError('启用状态无效')
    try:
        effective = datetime.fromisoformat(data.get('effective_at', ''))
        if effective.tzinfo is not None or effective < datetime.now()-timedelta(minutes=1): raise ValueError()
    except (TypeError, ValueError):
        raise service.PointsError('生效时间必须为当前或未来的本地时间')
    # 营期锁同时保护首次创建规则。
    db.session.query(CampSession).filter_by(id=sid).with_for_update().one()
    r = PointsRule.query.filter_by(camp_id=sid, kind=kind).first()
    expected = data.get('version', 0)
    if type(expected) is not int or expected != (r.version if r else 0):
        raise service.PointsError('规则已更新，请刷新后重试', 409)
    if not r:
        r = PointsRule(camp_id=sid, kind=kind, version=0); db.session.add(r); db.session.flush()
    if r.version:
        latest = PointsRuleVersion.query.filter_by(rule_id=r.id, version=r.version).one()
        if effective < latest.effective_at:
            raise service.PointsError('生效时间不能早于上一版本', 409)
    r.version += 1
    db.session.add(PointsRuleVersion(rule_id=r.id, version=r.version, amount=amount,
        period_limit=limit, enabled=data['enabled'], effective_at=effective, created_by=user.id))
    db.session.commit()
    return jsonify(code=200, item=service.rule_data(r, latest=True))


@bp.get('/manage/members')
@jwt_required()
@rewards_enabled
def members():
    user=actor(); sid=request.args.get('camp_id', type=int)
    if sid not in manager_ids(user): raise service.PointsError('无权查看', 403)
    q=CampMember.query.filter_by(camp_session_id=sid).filter(CampMember.is_learner)
    if not has_camp_access(user,sid,'reward.issue'):
        q=q.filter(CampMember.user_id.in_(training_access.taught_students(sid,user.id)))
    users=UserModel.query.filter(UserModel.id.in_([m.user_id for m in q])).all()
    return jsonify(code=200,items=[dict(id=u.id,name=u.username) for u in users])


@bp.get('/manage/rewards')
@jwt_required()
@rewards_enabled
def manage_rewards():
    user=actor(); sid=request.args.get('camp_id',type=int)
    if sid not in manager_ids(user): raise service.PointsError('无权查看',403)
    q=PointsReward.query.filter_by(camp_id=sid)
    if not has_camp_access(user,sid,'reward.issue'): q=q.filter_by(proposed_by=user.id)
    status=request.args.get('status')
    if status: q=q.filter_by(status=status)
    page=max(1,request.args.get('page',1,type=int)); total=q.count()
    rows=q.order_by(PointsReward.id.desc()).offset((page-1)*20).limit(20).all()
    return jsonify(code=200,items=[service.reward_data(r) for r in rows],total=total)


@bp.post('/manage/rewards')
@jwt_required()
@rewards_enabled
def propose():
    user=actor(); data=body(); sid=positive(data.get('camp_id'),'营期'); uid=positive(data.get('user_id'),'学员')
    camp=require_camp(user,sid,target=uid); reason=note(data)
    if uid == user.id: raise service.PointsError('不能为自己提报奖励',403)
    kind=data.get('kind')
    if kind not in ('contribution','completion'): raise service.PointsError('该奖励由系统校验，不支持人工提报')
    if kind=='completion' and camp.end_date >= datetime.now().date(): raise service.PointsError('营期尚未结束')
    key=data.get('request_key','')
    if not isinstance(key,str) or not re.fullmatch(r'[a-zA-Z0-9-]{16,64}',key): raise service.PointsError('请求标识无效')
    rule=PointsRule.query.filter_by(camp_id=sid,kind=kind).first()
    if not rule: raise service.PointsError('请先配置并启用对应奖励规则')
    event_key=f'{kind}:{sid}:{uid}:{key}'
    r=service.enqueue(rule,uid,event_key,datetime.now(),reason,source_id=str(sid),
        source_path=f'/camp?sid={sid}',actor=user.id,review=True)
    if not r: raise service.PointsError('奖励规则尚未生效')
    db.session.commit()
    return jsonify(code=200,item=service.reward_data(r))


@bp.post('/manage/rewards/<int:rid>/<action>')
@jwt_required()
@rewards_enabled
def change_reward(rid,action):
    user=actor(); data=body()
    r=PointsReward.query.filter_by(id=rid).with_for_update().first()
    if not r: raise service.PointsError('记录不存在',404)
    require_camp(user,r.camp_id,'review')
    if action in ('approve','reject'):
        if r.status!='review': raise service.PointsError('该记录已处理，请刷新',409)
        if user.id == r.user_id or user.id == r.proposed_by: raise service.PointsError('不能审批自己提报或自己的奖励',403)
        if action=='approve':
            member=CampMember.query.filter_by(camp_session_id=r.camp_id,user_id=r.user_id).first()
            if not member or not member.is_learner: raise service.PointsError('学员已退出本营，请退回申请',409)
        r.review_note=note(data) if action=='reject' else str(data.get('reason') or '').strip()[:512]
        r.reviewed_by=user.id; r.reviewed_at=datetime.now(); r.status='pending' if action=='approve' else 'rejected'
        service.event(r,action,user.id,r.review_note)
    elif action=='reverse':
        r=service.reverse(r,user.id,note(data))
    elif action=='retry':
        if r.status!='failed': raise service.PointsError('只有未确认入账记录可以重试',409)
        r.next_attempt_at=datetime.now(); service.event(r,'retry_requested',user.id)
    else: raise service.PointsError('操作不存在',404)
    db.session.commit()
    if action in ('approve','retry','reverse'): service.dispatch(r.id)
    return jsonify(code=200,item=service.reward_data(db.session.get(PointsReward,r.id)))

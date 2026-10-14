"""积分奖励业务。规则按事件时刻取版本；远端到账确认前绝不记为成功。"""
from datetime import datetime, timedelta, time
import uuid
from flask import current_app
from sqlalchemy import or_
from exts import db
from models import UserModel, CampSession, CampMember, CampChapterCertification, CampAttendancePlan, CheckRecord
from points_models import PointsRule, PointsRuleVersion, PointsReward, PointsEvent
from services.points_identity import resolve_points_identity
import points_center_client as center

KINDS = {'attendance': '考勤达标', 'chapter': '章节认证', 'contribution': '营期贡献', 'completion': '营期结业'}
HELP = {'attendance': '每日承诺考勤在次日按本营规则结算；迟到但时长达标仍按现有考勤口径计入。按周考勤不使用此规则。',
        'chapter': '首次完成章节认证后发放。同一章节跨营不重复奖励；撤销认证会撤回相应积分。',
        'contribution': '导生提交贡献依据，由本营老师审核；每月次数由规则确定。',
        'completion': '营期结束后由本营老师核实结业并审批，每人每营一次。'}


class PointsError(Exception):
    def __init__(self, message, status=400):
        self.message, self.status = message, status


def version_at(rule, when=None):
    return PointsRuleVersion.query.filter(PointsRuleVersion.rule_id == rule.id,
        PointsRuleVersion.effective_at <= (when or datetime.now())).order_by(
        PointsRuleVersion.effective_at.desc(), PointsRuleVersion.version.desc()).first()


def event(reward, action, actor_id=None, note=None):
    db.session.add(PointsEvent(reward_id=reward.id, actor_id=actor_id, action=action, note=note))


def rule_data(rule, latest=False):
    v = (PointsRuleVersion.query.filter_by(rule_id=rule.id).order_by(PointsRuleVersion.version.desc()).first()
         if latest else version_at(rule))
    if not v:
        return None
    camp = db.session.get(CampSession, rule.camp_id)
    return dict(id=rule.id, camp_id=rule.camp_id, camp_name=camp.name, kind=rule.kind,
        title=KINDS[rule.kind], amount=v.amount, period_limit=v.period_limit, enabled=v.enabled,
        version=v.version, effective_at=v.effective_at.isoformat(), help=HELP[rule.kind])


def reward_data(r):
    user = db.session.get(UserModel, r.user_id)
    camp = db.session.get(CampSession, r.camp_id)
    return dict(id=r.id, user_id=r.user_id, username=user.username if user else '',
        camp_id=r.camp_id, camp_name=camp.name if camp else '', kind=r.kind, title=r.title,
        amount=r.amount, status=r.status, reason=r.reason, review_note=r.review_note,
        source_id=r.source_id, source_path=r.source_path, parent_id=r.parent_id,
        rule_version=r.rule_version, transaction_uuid=r.transaction_uuid,
        balance_after=float(r.balance_after) if r.balance_after is not None else None,
        balance_before=float(r.balance_after)-r.amount if r.balance_after is not None else None,
        created_at=r.created_at.isoformat(), completed_at=r.completed_at.isoformat() if r.completed_at else None,
        attempts=r.attempts, last_error=r.last_error)


def enqueue(rule, uid, key, when, reason, *, source_id=None, source_path=None, actor=None, review=False):
    # 按人锁定，覆盖跨营同章竞争及每月次数限制；唯一键另作数据库兜底。
    user = UserModel.query.filter_by(id=uid).with_for_update().first()
    if not user or user.status != 'active' or user.lifecycle != 'active':
        raise PointsError('该学员账号不可用')
    existing = PointsReward.query.filter_by(event_key=key).with_for_update().first()
    if existing:
        if review and existing.reason != reason:
            raise PointsError('该提报已提交，请刷新记录', 409)
        return existing
    v = version_at(rule, when)
    if not v or not v.enabled:
        return None
    member = CampMember.query.filter_by(camp_session_id=rule.camp_id, user_id=uid).first()
    if not member or not member.is_learner:
        raise PointsError('该用户不是本营有效学员')
    period = when.strftime('%Y-%m') if rule.kind == 'contribution' else (when.date().isoformat() if rule.kind == 'attendance' else str(source_id or rule.camp_id))
    if rule.kind in ('contribution', 'completion'):
        used = PointsReward.query.filter(PointsReward.user_id == uid, PointsReward.rule_id == rule.id,
            PointsReward.period == period, PointsReward.amount > 0,
            PointsReward.status.notin_(['rejected', 'cancelled'])).with_for_update().all()
        if len(used) >= (v.period_limit if rule.kind == 'contribution' else 1):
            raise PointsError('本月奖励次数已达上限' if rule.kind == 'contribution' else '本营结业奖励已提报或发放')
    r = PointsReward(user_id=uid, camp_id=rule.camp_id, rule_id=rule.id, rule_version=v.version,
        kind=rule.kind, title=KINDS[rule.kind], amount=v.amount, period=period,
        event_key=key, idempotency_key='camp:'+uuid.uuid4().hex, status='review' if review else 'pending',
        reason=reason, source_id=str(source_id) if source_id is not None else None,
        source_path=source_path, proposed_by=actor, created_at=when)
    db.session.add(r)
    db.session.flush()
    event(r, 'proposed' if review else 'queued', actor, reason)
    return r


def reverse(r, actor, reason):
    original = PointsReward.query.filter_by(id=r.id).with_for_update().one()
    if original.amount <= 0 or original.status in ('rejected', 'cancelled'):
        raise PointsError('该记录不可撤回')
    existing = PointsReward.query.filter_by(parent_id=original.id).first()
    if existing:
        return existing
    if original.status in ('review', 'pending', 'failed') and (not original.attempts or original.center_user_id is None):
        original.status = 'cancelled'
        event(original, 'cancelled', actor, reason)
        return original
    # 已尝试的请求可能在远端成功；先确认原发放，再提交反向扣减，不直接丢弃。
    undo = PointsReward(user_id=original.user_id, camp_id=original.camp_id, rule_id=original.rule_id,
        rule_version=original.rule_version, kind=original.kind, title='奖励撤回 · '+original.title,
        amount=-original.amount, period=original.period, event_key='reverse:'+str(original.id),
        idempotency_key='camp:reverse:'+uuid.uuid4().hex, status='pending', reason=reason,
        parent_id=original.id, source_id=original.source_id, source_path=original.source_path,
        proposed_by=actor, reviewed_by=actor, reviewed_at=datetime.now(),
        center_email=original.center_email, center_user_id=original.center_user_id)
    db.session.add(undo); db.session.flush(); event(undo, 'reversal_queued', actor, reason)
    return undo


def dispatch(reward_id):
    if not current_app.config.get('POINTS_REWARDS_ENABLED') or not current_app.config.get('POINTS_CENTER_ENABLED'):
        return False
    now = datetime.now()
    r = PointsReward.query.filter_by(id=reward_id).with_for_update().first()
    if not r or r.status not in ('pending', 'failed', 'processing'):
        db.session.rollback(); return False
    if r.status == 'processing' and r.lease_until and r.lease_until > now:
        db.session.rollback(); return False
    if r.parent_id:
        parent = db.session.get(PointsReward, r.parent_id)
        if parent.status != 'completed':
            db.session.rollback(); return False
    uid, key = r.user_id, r.idempotency_key
    r.status = 'processing'; r.lease_until = now + timedelta(minutes=2); r.attempts += 1
    attempt = r.attempts
    db.session.commit()  # 请求前持久化尝试，进程中断后仍以同一个幂等键恢复。
    try:
        user = db.session.get(UserModel, uid)
        identity = resolve_points_identity(user)
        if not identity.eligible:
            raise PointsError(identity.message)
        platform_id = str(uid).zfill(7)
        account = center.ensure_user(identity.email, platform_id)
        center_uid = account.get('user_id')
        if type(center_uid) is not int:
            raise PointsError('积分账户关联结果未确认')
        if r.center_user_id is not None and (r.center_user_id != center_uid or r.center_email.lower() != identity.email.lower()):
            raise PointsError('积分身份已变化，请联系管理员核对')
        r.center_user_id = center_uid; r.center_email = identity.email
        db.session.commit()
        result = center.change_points(platform_id, r.amount, r.reason, key)
        r = PointsReward.query.filter_by(id=reward_id).populate_existing().with_for_update().one()
        if r.attempts != attempt:
            db.session.rollback(); return False
        r.transaction_uuid = result['transaction_uuid']; r.balance_after = result['balance_after']
        r.status = 'completed'; r.completed_at = datetime.now(); r.last_error = None
        r.lease_until = None; r.next_attempt_at = None
        event(r, 'completed'); db.session.commit(); return True
    except (center.PointsCenterError, PointsError) as exc:
        db.session.rollback()
        r = PointsReward.query.filter_by(id=reward_id).populate_existing().with_for_update().one()
        if r.attempts != attempt or r.status == 'completed':
            db.session.rollback(); return False
        r.status = 'failed'; r.lease_until = None
        r.last_error = exc.message if isinstance(exc, PointsError) else ({403: '积分中心未授予本平台发放或扣减权限', 402: '账户积分不足，暂无法撤回', 409: '积分中心返回冲突，请核对原发放记录'}.get(exc.status_code, '积分中心暂未确认入账'))
        r.next_attempt_at = datetime.now()+timedelta(minutes=min(60, 2**min(attempt, 6)))
        event(r, 'failed', note=r.last_error); db.session.commit(); return False


def capture_certification(cert):
    if not current_app.config.get('POINTS_REWARDS_ENABLED'):
        return
    rule = PointsRule.query.filter_by(camp_id=cert.camp_session_id, kind='chapter').first()
    if rule:
        enqueue(rule, cert.student_user_id, f'chapter:{cert.student_user_id}:{cert.chapter_id}',
            cert.certified_at, '章节认证奖励', source_id=cert.chapter_id,
            source_path=f'/course/chapter/{cert.course_id}?sid={cert.camp_session_id}&lessonId={cert.chapter_id}',
            actor=cert.mentor_user_id)


def revoke_certification(sid, uid, chapter_id, actor_id):
    if not current_app.config.get('POINTS_REWARDS_ENABLED'):
        return
    r = PointsReward.query.filter_by(camp_id=sid, user_id=uid,
        event_key=f'chapter:{uid}:{chapter_id}').first()
    if r and r.status not in ('cancelled', 'rejected'):
        reverse(r, actor_id, '章节认证撤销')


def collect_automatic():
    """按生效规则扫描可验证事件；开关启用前的旧行为不追溯赠分。"""
    from blueprints.camp import _eval_day, _approved_leave_dates, _attendance_mode
    now = datetime.now()
    for rule in PointsRule.query.filter(PointsRule.kind.in_(['chapter', 'attendance'])).all():
        camp = db.session.get(CampSession, rule.camp_id)
        if camp.status in ('archived', 'deleted') or camp.training_migration_paused:
            continue
        first = PointsRuleVersion.query.filter_by(rule_id=rule.id).order_by(PointsRuleVersion.effective_at).first()
        if not first or first.effective_at > now:
            continue
        if rule.kind == 'chapter':
            certs = CampChapterCertification.query.filter_by(camp_session_id=camp.id).filter(
                CampChapterCertification.certified_at >= first.effective_at).all()
            for cert in certs:
                try:
                    enqueue(rule, cert.student_user_id, f'chapter:{cert.student_user_id}:{cert.chapter_id}',
                        cert.certified_at, '章节认证奖励', source_id=cert.chapter_id,
                        source_path=f'/course/chapter/{cert.course_id}?sid={camp.id}&lessonId={cert.chapter_id}', actor=cert.mentor_user_id)
                    db.session.commit()
                except PointsError:
                    db.session.rollback()
        elif _attendance_mode(camp) == 'daily':
            plans = CampAttendancePlan.query.filter_by(camp_session_id=camp.id).filter(
                CampAttendancePlan.date >= first.effective_at.date(), CampAttendancePlan.date < now.date(),
                CampAttendancePlan.date >= camp.start_date, CampAttendancePlan.date <= camp.end_date).all()
            leaves = _approved_leave_dates(camp.id, camp.start_date, camp.end_date)
            for plan in plans:
                member = CampMember.query.filter_by(camp_session_id=camp.id, user_id=plan.user_id).first()
                if not member or not member.needs_attendance:
                    continue
                records = CheckRecord.query.filter_by(user_id=plan.user_id, date=plan.date).all()
                if _eval_day(records, plan, (plan.user_id, plan.date) in leaves)['status'] not in ('present', 'late'):
                    continue
                try:
                    enqueue(rule, plan.user_id, f'attendance:{camp.id}:{plan.user_id}:{plan.date}',
                        datetime.combine(plan.date, time.max), '考勤达标奖励', source_id=plan.date.isoformat(),
                        source_path=f'/camp?sid={camp.id}&tab=attendance')
                    db.session.commit()
                except PointsError:
                    db.session.rollback()
    # 已发章节资格被撤销时自动撤回；改分不触发新的奖励。
    for r in PointsReward.query.filter_by(kind='chapter').filter(PointsReward.amount > 0,
            PointsReward.status.notin_(['cancelled', 'rejected'])).all():
        valid = CampChapterCertification.query.filter_by(camp_session_id=r.camp_id,
            student_user_id=r.user_id, chapter_id=int(r.source_id)).first()
        if not valid:
            reverse(r, None, '章节认证撤销'); db.session.commit()


def run_worker():
    collect_automatic()
    now = datetime.now()
    rows = PointsReward.query.filter(PointsReward.status.in_(['pending', 'failed', 'processing']),
        or_(PointsReward.next_attempt_at.is_(None), PointsReward.next_attempt_at <= now)).order_by(PointsReward.id).limit(100).all()
    ids = [r.id for r in rows]
    db.session.rollback()
    for rid in ids:
        dispatch(rid)

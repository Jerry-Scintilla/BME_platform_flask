"""管理辅助账号授权（D5 收尾 P2-9，规格 9.3）+ 身份巡检告警（P2-11，规格 15）。

辅助账号：批准即建 grant 行（默认 90 天）+ user.account_kind=management_aux；
到期实时检查（has_valid_grant 看 valid_until，不依赖清理任务）；撤销留痕。
同人关系不替代角色授权（规格 3.2）；到期复核由巡检提醒。

巡检（P0 不变量，规格 15）：
  - merged 账号仍有活会话/生产工作区授权
  - 同营锚点与成员漂移（active 成员无 active 锚点）
  - 同一人员多个 active 正式参与账号
  - outbox 积压超 24 小时 / 辅助授权到期临期（7 天）与过期未复核
发现即 identity_event(action=identity.patrol.alarm) 留痕 + 超管理员站内告警
（按「类型+日」去重防打扰）。
"""
from datetime import datetime, timedelta

from exts import db
from models import (AuxiliaryAccountGrantModel, AuthSessionModel,
                    IdentityOutboxModel, NotificationModel, PersonModel,
                    PersonPrimaryAccountModel, UserModel,
                    CampPersonParticipationModel, CampMember,
                    WorkAccessGrant)
from services.identity import enforcement, events

GRANT_DAYS = 90
REMIND_DAYS = 7


# ── 辅助账号授权 ────────────────────────────────────────────────

def approve_grant(operator, *, user_id, purpose, scope=None, days=GRANT_DAYS):
    """批准辅助账号（不 commit）：建 grant + account_kind=management_aux。

    用途必填（规格 9.3「用途与维护责任人明确」）；绑定归属人员=批准人所在
    人员档案（后续可改绑）；重复批准=延期（新行，旧行自然过期）。
    """
    target = db.session.get(UserModel, user_id)
    if target is None:
        from services.auth_context import AuthRejected
        raise AuthRejected('目标账号不存在', status=404, machine='NOT_FOUND')
    purpose = (purpose or '').strip()
    if not purpose:
        from services.auth_context import AuthRejected
        raise AuthRejected('请填写辅助账号用途', status=400, machine='BAD_REQUEST')
    if target.account_kind == 'service':
        from services.auth_context import AuthRejected
        raise AuthRejected('服务号不能转为辅助账号', status=400, machine='BAD_REQUEST')
    # R0 核验门槛（2026-10-02 收紧批）：辅助账号是权限位——目标账号须已核验
    _ok, _reason, _status = enforcement.check_verified(target, 'appoint')
    if not _ok:
        from services.auth_context import AuthRejected
        raise AuthRejected(_reason, status=403,
                           machine='IDENTITY_VERIFICATION_REQUIRED')
    row = AuxiliaryAccountGrantModel(
        user_id=target.id,
        owner_person_id=operator.person_id,
        purpose=purpose[:100], scope=(scope or '').strip()[:100] or None,
        valid_until=datetime.now() + timedelta(days=days),
        approved_by=operator.id)
    db.session.add(row)
    target.account_kind = 'management_aux'
    events.record_event(
        'identity.auxiliary.approve', actor_user_id=operator.id,
        target_ids={'user_id': target.id},
        after=None,
        evidence_refs={'note': purpose[:64]},
        reason=f'辅助账号批准（{days} 天）')
    return row


def revoke_grant(operator, grant_id, *, reason=''):
    from services.auth_context import AuthRejected
    row = db.session.get(AuxiliaryAccountGrantModel, grant_id)
    if row is None:
        raise AuthRejected('授权不存在', status=404, machine='NOT_FOUND')
    row.state = 'revoked'
    # 无其他有效授权则回落 standard
    if not has_valid_grant(row.user_id):
        u = db.session.get(UserModel, row.user_id)
        if u and u.account_kind == 'management_aux':
            u.account_kind = 'standard'
    events.record_event(
        'identity.auxiliary.revoke', actor_user_id=operator.id,
        target_ids={'user_id': row.user_id},
        evidence_refs={'note': (reason or '')[:64]},
        reason='辅助账号撤销')
    return row


def has_valid_grant(user_id):
    return AuxiliaryAccountGrantModel.query.filter_by(
        user_id=user_id, state='active').filter(
        AuxiliaryAccountGrantModel.valid_until > datetime.now()).first() is not None


def list_grants():
    now = datetime.now()
    rows = AuxiliaryAccountGrantModel.query.order_by(
        AuxiliaryAccountGrantModel.id.desc()).limit(200).all()
    out = []
    for r in rows:
        u = db.session.get(UserModel, r.user_id)
        out.append({
            'id': r.id, 'user_id': r.user_id,
            'username': u.username if u else '-', 'email': u.email if u else '-',
            'purpose': r.purpose, 'scope': r.scope,
            'valid_until': r.valid_until.strftime('%Y-%m-%d %H:%M'),
            'state': r.state if r.state != 'active' or r.valid_until > now else 'expired',
            'expiring_soon': (r.state == 'active' and now < r.valid_until
                              and r.valid_until < now + timedelta(days=REMIND_DAYS)),
        })
    return out


# ── 巡检（P2-11）────────────────────────────────────────────────

def run_patrol(app=None):
    """跑一轮巡检（可独立调用；调度入口见 init_patrol_scheduler）。

    返回告警列表；每类告警按「类型+日」给超管发一次站内通知（防打扰）。
    """
    alarms = []
    now = datetime.now()

    # A1: merged 账号仍有活会话（P0：撤权必须彻底）
    rows = (AuthSessionModel.query.join(UserModel, UserModel.id == AuthSessionModel.user_id)
            .filter(UserModel.lifecycle == 'merged',
                    AuthSessionModel.revoked_at.is_(None),
                    AuthSessionModel.expires_at > now).all())
    if rows:
        alarms.append(('merged_live_session',
                       f'{len(rows)} 个已合并账号仍有活会话（应已全部撤销）'))

    # A2: merged 账号仍有 active 生产工作区授权（P0：防复活）
    rows = (WorkAccessGrant.query.join(UserModel, UserModel.id == WorkAccessGrant.user_id)
            .filter(UserModel.lifecycle == 'merged',
                    WorkAccessGrant.status == 'active').all())
    if rows:
        alarms.append(('merged_active_grant',
                       f'{len(rows)} 条已合并账号的 active 工作区授权（应清理）'))

    # A3: active 成员无 active 锚点（锚点漂移——回填/接线遗漏或 shadow 冲突保留）
    drift = (CampMember.query.filter_by(status='active')
             .outerjoin(CampPersonParticipationModel,
                        CampPersonParticipationModel.camp_member_id == CampMember.id)
             .filter(db.or_(CampPersonParticipationModel.camp_member_id.is_(None),
                            CampPersonParticipationModel.state != 'active')).count())
    if drift:
        alarms.append(('anchor_drift', f'{drift} 个 active 营期成员无 active 参与锚点'))

    # A4: 同一人员多个 active 正式参与账号（ppa 之外还有指向同 person 的 active 号）
    dup = (db.session.query(UserModel.person_id, db.func.count(UserModel.id))
           .filter(UserModel.person_id.isnot(None),
                   UserModel.lifecycle == 'active',
                   UserModel.status == 'active')
           .group_by(UserModel.person_id).having(db.func.count(UserModel.id) > 1).count())
    if dup:
        alarms.append(('multi_active_accounts', f'{dup} 个人员档案挂多个 active 账号（归并后正常为副号，请核对）'))

    # A5: outbox 积压 > 24h（消费者故障）
    stuck = IdentityOutboxModel.query.filter_by(
        delivery_state='pending').filter(
        IdentityOutboxModel.created_at < now - timedelta(hours=24)).count()
    if stuck:
        alarms.append(('outbox_stuck', f'{stuck} 条身份通知积压超 24 小时（消费者异常）'))

    # A6: 辅助授权临期/过期未复核
    expiring = AuxiliaryAccountGrantModel.query.filter_by(state='active').filter(
        AuxiliaryAccountGrantModel.valid_until < now + timedelta(days=REMIND_DAYS)).count()
    if expiring:
        alarms.append(('auxiliary_expiring',
                       f'{expiring} 条辅助账号授权 7 天内到期或已过期（规格 9.3：到期复核）'))

    if alarms:
        events.record_event(
            'identity.patrol.alarm', actor_user_id=0,
            evidence_refs={'note': ';'.join(a[0] for a in alarms)},
            reason=f'巡检发现 {len(alarms)} 类告警')
        _notify_admins(alarms, now)
    return alarms


def _notify_admins(alarms, now):
    """按「类型+日」去重给 super_admin 发告警通知（防打扰）。"""
    day_key = now.strftime('%Y%m%d')
    for code, message in alarms:
        dedup = f'patrol:{code}:{day_key}'
        if NotificationModel.query.filter_by(source_type=dedup).first():
            continue
        for admin in UserModel.query.filter_by(role='super_admin', status='active').all():
            db.session.add(NotificationModel(
                user_id=admin.id, title='身份体系巡检告警',
                content=f'{message}。详见 identity_event 巡检记录或盘点脚本第九节。',
                category='system', source_type=dedup))
    db.session.commit()


# ── 调度（每小时；fcntl 单实例同 outbox 制式）───────────────────

_scheduler = None


def init_patrol_scheduler(app):
    global _scheduler
    if _scheduler is not None:
        return
    import fcntl
    import os
    lock_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             '..', '..', 'log', '.identity_patrol.lock')
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.interval import IntervalTrigger
    _scheduler = BackgroundScheduler(daemon=True)
    _scheduler.add_job(
        lambda: _run_with_app(app), trigger=IntervalTrigger(hours=1),
        id='identity_patrol', max_instances=1, coalesce=True)
    _scheduler.start()
    _run_with_app(app)
    print("[identity-patrol] 巡检调度已启动（1h 间隔，fcntl 单实例）")


def _run_with_app(app):
    with app.app_context():
        try:
            alarms = run_patrol()
            if alarms:
                print(f"[identity-patrol] {len(alarms)} 类告警："
                      + ';'.join(a[0] for a in alarms))
        except Exception as e:  # noqa: BLE001
            print(f"[identity-patrol] 巡检失败: {e}")

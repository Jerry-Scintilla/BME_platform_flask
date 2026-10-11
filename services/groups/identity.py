"""小组身份桥接：沿用人员层核验模式与宽限，不引入社团或工作区授权。"""
from exts import db
from models import PersonModel, PersonPrimaryAccountModel
from services.identity import enforcement, events

from .errors import GroupError


def require_live_account(user):
    if user is None or user.status != 'active' or user.lifecycle != 'active':
        raise GroupError('账号不存在、已停用或已合并', machine='GROUP_ACCOUNT_INACTIVE', status=403)
    if user.person_id:
        person = db.session.get(PersonModel, user.person_id)
        if person is None or person.record_status != 'active':
            raise GroupError('人员档案不可用于当前参与', machine='GROUP_PERSON_INACTIVE', status=403)


def require_participant_identity(user, decision):
    require_live_account(user)
    if enforcement.mode() == 'off':
        return
    reason = None
    if not user.person_id:
        reason = 'person_missing'
    else:
        primary = db.session.get(PersonPrimaryAccountModel, user.person_id)
        if (primary is None or primary.user_id != user.id) and not enforcement.exception_valid(
                user, decision.participation_scope, decision.participation_scope_id):
            reason = 'nonprimary'
    if reason:
        if enforcement.mode() == 'enforce':
            raise GroupError('请使用正式参与账号，或先处理人员参与宽限',
                             machine='GROUP_IDENTITY_REQUIRED', status=409)
        events.record_event(
            'identity.enforcement.shadow', actor_user_id=user.id,
            target_ids={'user_id': user.id, 'person_id': user.person_id},
            evidence_refs={'note': 'group_join:' + reason}, reason='小组参与人员规则记录（shadow）')


def require_appointment(user, decision):
    ok, reason, _ = enforcement.check_verified(
        user, 'appoint', decision.participation_scope_id)
    if not ok:
        raise GroupError(reason, machine='IDENTITY_VERIFICATION_REQUIRED', status=403)


def eligible_now(user, decision, *, appointment=False):
    """实时读取候选职责；不写核验日志，不把历史任命视为永久授权。"""
    try:
        require_live_account(user)
    except GroupError:
        return False
    if enforcement.mode() == 'enforce':
        if not user.person_id:
            return False
        primary = db.session.get(PersonPrimaryAccountModel, user.person_id)
        if (primary is None or primary.user_id != user.id) and not enforcement.exception_valid(
                user, decision.participation_scope, decision.participation_scope_id):
            return False
    if not appointment or enforcement.gate_mode('appoint') != 'enforce':
        return True
    if user.is_admin() or user.account_kind in ('service', 'test'):
        return True
    if enforcement.exception_valid(user, 'verify_gate'):
        return True
    person = db.session.get(PersonModel, user.person_id) if user.person_id else None
    return person is not None and person.verification_status == 'verified'

"""旧招生端点的写入分流：已切换营只调用统一服务；旧营保留原账本语义。

调用方继续负责既有选导生窗口、名额、操作人权限校验；本模块不提交事务。
"""
from exts import db
from models import CampSession, CampMentorMatch
from services.groups import GroupError
from . import training, training_access


def assign(member, mentor_id, actor_id, *, source='admin', daily=False, allow_over=False, write_ledger=True):
    sid = member.camp_session_id
    camp = db.session.get(CampSession, sid)
    if not camp or camp.status in ('archived', 'deleted'):
        raise GroupError('营期已归档或不存在，只读', machine='GROUP_READ_ONLY')
    if training_access.enabled(sid):
        return training.recruit(sid, member.user_id, mentor_id, actor_id,
                                source=source, daily=daily, allow_over=allow_over)
    member.team_mentor_id = mentor_id
    if not write_ledger:
        return None
    ledger = CampMentorMatch.query.filter_by(camp_session_id=sid, student_user_id=member.user_id).first()
    if mentor_id is None:
        if ledger:
            db.session.delete(ledger)
    else:
        if ledger is None:
            ledger = CampMentorMatch(camp_session_id=sid, student_user_id=member.user_id,
                                     mentor_user_id=mentor_id)
            db.session.add(ledger)
        ledger.mentor_user_id, ledger.source, ledger.round = mentor_id, source, None
    return None

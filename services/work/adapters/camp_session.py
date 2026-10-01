"""营期域投影适配器（X2 通用投影架构）。

权限口径平移自 integrations.py 原 camp_session 分支：仅营内成员∨超管可见
摘要，其余回 None（不可访问占位）。stewardable=False：营期是流程对象，
维护责任不在工作区侧。
"""
from models import CampMember, CampSession
from services.work.projections import WorkProjectionProvider, register_projection

SESSION_STATUS_LABELS = {'preparing': '筹备中', 'running': '进行中', 'finished': '已结束'}


class CampSessionProvider(WorkProjectionProvider):
    source_type = 'camp_session'
    label = '营期'
    stewardable = False

    def exists(self, source_id):
        return CampSession.query.get(source_id) is not None

    def summarize(self, user, source_id):
        session = CampSession.query.get(source_id)
        if not session:
            return None
        member = CampMember.query.filter_by(
            camp_session_id=session.id, user_id=user.id).first()
        if not member and not user.is_admin():
            return None
        start = session.start_date.isoformat() if session.start_date else None
        end = session.end_date.isoformat() if session.end_date else None
        return {
            'title': session.name,
            '状态': SESSION_STATUS_LABELS.get(session.status, session.status),
            '起止': f'{start} ~ {end}' if start else None,
        }


register_projection(CampSessionProvider())

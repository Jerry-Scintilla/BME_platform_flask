"""工单域投影适配器（X2 通用投影架构）。

权限口径平移自 integrations.py 原 feedback_ticket 分支：仅报告人/经办人∨
超管可见摘要。stewardable=False：工单是流转对象，不是责任物。
"""
from models import FeedbackTicket
from services.work.projections import WorkProjectionProvider, register_projection

TICKET_STATUS_LABELS = {
    'new': '待处理', 'triaged': '已分诊', 'in_progress': '处理中',
    'waiting_user': '等用户', 'resolved': '已解决', 'closed': '已关闭',
    'rejected': '已拒绝', 'reopened': '已重开',
}


class FeedbackTicketProvider(WorkProjectionProvider):
    source_type = 'feedback_ticket'
    label = '工单'
    stewardable = False

    def exists(self, source_id):
        return FeedbackTicket.query.get(source_id) is not None

    def summarize(self, user, source_id):
        ticket = FeedbackTicket.query.get(source_id)
        if not ticket:
            return None
        if user.id not in (ticket.reporter_user_id, ticket.assignee_user_id) \
                and not user.is_admin():
            return None
        return {
            'title': ticket.title,
            '状态': TICKET_STATUS_LABELS.get(ticket.status, ticket.status),
            '优先级': ticket.priority,
        }


register_projection(FeedbackTicketProvider())

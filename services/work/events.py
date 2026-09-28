"""内部工作台·事件与组织变更留痕（设计方案 §10.1/§3.3/§17.1）。

全部函数只 db.session.add 不自行 commit——与业务变更同一事务，由蓝图/调度层
提交（create_notification 同事务语义同理）。组织变更挂钩点在 club_admin.py 与
officers.py 的写端点内，集中走本模块，单人/批量/归档共用一套规则。
"""
import json

from exts import db
from models import ClubOrgEvent, WorkEvent

# 治理类事件：时间线接口只对协调员/治理身份展示（§13 按可见范围过滤治理信息）
GOVERNANCE_EVENT_TYPES = {'emergency_access'}


def record_org_event(kind, *, user_id=None, membership_id=None, officer_id=None,
                     from_group_id=None, to_group_id=None, operator_id=None, detail=None):
    """组织变更事件：补 ClubMembership 原地覆盖缺失的历史（§3.3）。
    kind ∈ membership_set/membership_batch/group_archived/officer_appointed/
    officer_edited/officer_ended。detail 传 dict，序列化为最小必要字段。"""
    row = ClubOrgEvent(
        kind=kind, user_id=user_id, membership_id=membership_id, officer_id=officer_id,
        from_group_id=from_group_id, to_group_id=to_group_id, operator_id=operator_id,
        detail_json=json.dumps(detail, ensure_ascii=False) if detail else None,
    )
    db.session.add(row)
    return row


def record_event(item, event_type, *, actor_user_id=None, actor_snapshot=None,
                 diff=None, reason=None, request_id=None):
    """追加工作时间线（不可变）。须在持有事项行锁（FOR UPDATE）的事务内调用，
    seq = 当前最大值+1，uq_work_event_item_seq 兜底并发。"""
    last = (db.session.query(db.func.max(WorkEvent.seq))
            .filter(WorkEvent.item_id == item.id).scalar() or 0)
    row = WorkEvent(
        item_id=item.id, seq=last + 1, actor_user_id=actor_user_id,
        actor_snapshot=actor_snapshot, event_type=event_type,
        diff_json=json.dumps(diff, ensure_ascii=False) if diff else None,
        reason=reason, request_id=request_id,
    )
    db.session.add(row)
    return row


# 通知文案（最小信息原则 §10.2：不含事项标题/正文/文件名，点击后端重查权限）
NOTIFY_TEXTS = {
    'reply_to_me': '你参与的工作事项有新回复',
    'response_requested': '有一项工作事项等待你回复',
    'mentioned': '你被邀请参与一项工作事项',
    'published': '你的工作区有新的工作事项',
    'status_changed': '你参与的工作事项状态有更新',
    'assigned': '有一项工作任务分配给你',
    'transfer_pending': '有一项任务转交等待你确认',
    'review_requested': '有一项任务提交等待你验收',
    'due_soon': '你有一项工作即将到期',
    'overdue': '你有一项工作已逾期',
    'follow_up': '你有一项受阻工作到了跟进时间',
}


def fan_out(item, event, targets):
    """为事件生成站内通知（与业务变更同事务，§10.2）。

    targets: [(user_id, notify_type)]，调用方已过滤操作者本人与失资格者。
    每接收人先写 WorkNotificationReceipt（uq event×user×type 数据库去重），
    再 create_notification 加入当前事务（不 commit）。同一事件对同一人同一
    类型只发一条：定向扇出与受众扇出叠加时按回执去重（查询经 autoflush
    可见本事务未提交行）。
    """
    from blueprints.notification import create_notification
    from models import WorkNotificationReceipt

    sent = 0
    for uid, notify_type in targets:
        existed = WorkNotificationReceipt.query.filter_by(
            event_id=event.id, user_id=uid, notify_type=notify_type).first()
        if existed:
            continue
        receipt = WorkNotificationReceipt(event_id=event.id, user_id=uid,
                                          notify_type=notify_type)
        db.session.add(receipt)
        db.session.flush()
        notification = create_notification(
            uid, '内部工作台', NOTIFY_TEXTS.get(notify_type, '内部工作台有新动态'),
            category='work', source_type='work_item', source_id=item.id)
        receipt.notification_id = notification.id
        sent += 1
    return sent

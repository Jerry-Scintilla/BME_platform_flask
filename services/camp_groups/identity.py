"""账号归并对新小组关系的保护；不隐式转移任职或合并同组重复人员。"""
from exts import db
from models import (GroupMembership, CampGroup, GroupMembershipEvent,
                    CampPersonParticipationModel, UnitPersonParticipationModel)
from services.auth_context import AuthRejected


def prepare_person_rebind(user, person_id, actor_id):
    rows = GroupMembership.query.filter_by(user_id=user.id).all()
    if not rows or user.person_id == person_id:
        return
    group_ids = sorted({row.group_id for row in rows})
    groups = CampGroup.query.filter(CampGroup.id.in_(group_ids)).order_by(
        CampGroup.id).populate_existing().with_for_update().all()
    if GroupMembership.query.filter(GroupMembership.group_id.in_(group_ids),
                                    GroupMembership.person_id == person_id,
                                    GroupMembership.user_id != user.id).first():
        raise AuthRejected('关联双方在同一小组已有参与，需要先裁定成员映射',
                           status=409, machine='GROUP_PERSON_MAPPING_CONFLICT')
    # 既有人员锚点没有 ON UPDATE CASCADE；不擅自重写历史锚点绕过原身份约束。
    if (CampPersonParticipationModel.query.filter_by(user_id=user.id).first() or
            UnitPersonParticipationModel.query.filter_by(user_id=user.id).first()):
        raise AuthRejected('该账号存在旧营期参与锚点，需要先完成其人员映射裁定',
                           status=409, machine='GROUP_LEGACY_ANCHOR_REVIEW_REQUIRED')
    for group in groups:
        group.version += 1
        db.session.add(GroupMembershipEvent(group_id=group.id, actor_user_id=actor_id,
            action='person_rebind', context_kind='identity', context_id=person_id,
            authorization_source='identity:approved_link', reason='经身份关联流程调整人员归属，参与账号和职责不变',
            before={'person_id': user.person_id, 'user_id': user.id},
            after={'person_id': person_id, 'user_id': user.id}, group_version=group.version))
    # user.person_id 的后续更新由复合外键 ON UPDATE CASCADE 同事务传递给成员。
    db.session.flush()

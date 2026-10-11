"""教学营与项目营共用的 CampGroup 小组核心；由 models.py 统一注册，不含营期业务外键。"""
from datetime import datetime

from exts import db
from sqlalchemy.dialects.mysql import DATETIME

# 任命立即生效；MySQL DATETIME(0) 会舍入到下一秒，造成刚授予的职责短暂不可见。
GROUP_DATETIME = db.DateTime().with_variant(DATETIME(fsp=6), 'mysql')


class CampGroup(db.Model):
    __tablename__ = 'camp_group'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    status = db.Column(db.String(20), nullable=False, server_default='active')
    owner_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    version = db.Column(db.Integer, nullable=False, server_default='1')
    created_at = db.Column(GROUP_DATETIME, nullable=False, default=datetime.now)
    updated_at = db.Column(GROUP_DATETIME, nullable=False, default=datetime.now,
                           onupdate=datetime.now)
    __table_args__ = (
        db.CheckConstraint("status IN ('active', 'archived')", name='ck_camp_group_status'),
        db.CheckConstraint('version >= 1', name='ck_camp_group_version'),
    )
    __mapper_args__ = {'version_id_col': version, 'version_id_generator': False}


class GroupMembership(db.Model):
    """同组同账号/人员唯一；再加入复用成员 ID，历次参与和职责变动保留在事件中。"""
    __tablename__ = 'group_membership'
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey('camp_group.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    # 空值仅供既有 shadow/off 身份策略下的历史未归并账号，不能伪造 Person。
    person_id = db.Column(db.Integer, db.ForeignKey('person.id'))
    status = db.Column(db.String(20), nullable=False, server_default='active')
    started_at = db.Column(GROUP_DATETIME, nullable=False, default=datetime.now)
    ended_at = db.Column(GROUP_DATETIME)
    __table_args__ = (
        db.UniqueConstraint('group_id', 'user_id', name='uq_group_member_user'),
        db.UniqueConstraint('group_id', 'person_id', name='uq_group_member_person'),
        db.UniqueConstraint('id', 'group_id', name='uq_group_member_id_group'),
        db.ForeignKeyConstraint(['user_id', 'person_id'], ['user.id', 'user.person_id'],
                                name='fk_group_member_user_person', onupdate='CASCADE'),
        db.CheckConstraint("status IN ('active', 'ended')", name='ck_group_member_status'),
        db.CheckConstraint("(status = 'active' AND ended_at IS NULL) OR "
                           "(status = 'ended' AND ended_at IS NOT NULL)",
                           name='ck_group_member_ended'),
    )


class GroupRoleAssignment(db.Model):
    """固定职责，按组或课程限定；结束后释放有效槽，保留每次任命的原始时间。"""
    __tablename__ = 'group_role_assignment'
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, nullable=False)
    membership_id = db.Column(db.Integer, nullable=False)
    role = db.Column(db.String(30), nullable=False)
    scope_type = db.Column(db.String(20), nullable=False, server_default='group')
    scope_id = db.Column(db.Integer, nullable=False, server_default='0')
    # 1=尚未显式结束，NULL=历史；读取仍须判断 expires_at，不能只看槽位。
    active_slot = db.Column(db.Integer, server_default='1')
    granted_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    started_at = db.Column(GROUP_DATETIME, nullable=False, default=datetime.now)
    expires_at = db.Column(GROUP_DATETIME)
    ended_at = db.Column(GROUP_DATETIME)
    __table_args__ = (
        db.ForeignKeyConstraint(['membership_id', 'group_id'],
                                ['group_membership.id', 'group_membership.group_id'],
                                name='fk_group_role_member'),
        db.UniqueConstraint('membership_id', 'role', 'scope_type', 'scope_id',
                            'active_slot', name='uq_group_role_active'),
        db.CheckConstraint("role IN ('learner', 'mentor', 'project_leader', 'project_member')",
                           name='ck_group_role_name'),
        db.CheckConstraint("(scope_type = 'group' AND scope_id = 0) OR "
                           "(scope_type = 'course' AND scope_id > 0)",
                           name='ck_group_role_scope'),
        db.CheckConstraint("(active_slot IS NOT NULL AND active_slot = 1 AND ended_at IS NULL) OR "
                           "(active_slot IS NULL AND ended_at IS NOT NULL)",
                           name='ck_group_role_ended'),
        db.CheckConstraint('expires_at IS NULL OR expires_at > started_at',
                           name='ck_group_role_expiry'),
    )


class GroupMembershipEvent(db.Model):
    """只追加的组织事件；成员/任职快照只存必要 ID 与状态。"""
    __tablename__ = 'group_membership_event'
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey('camp_group.id'), nullable=False)
    membership_id = db.Column(db.Integer, db.ForeignKey('group_membership.id'))
    actor_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    action = db.Column(db.String(30), nullable=False)
    context_kind = db.Column(db.String(30), nullable=False)
    context_id = db.Column(db.Integer, nullable=False)
    authorization_source = db.Column(db.String(100), nullable=False)
    reason = db.Column(db.String(500), nullable=False)
    before = db.Column(db.JSON)
    after = db.Column(db.JSON)
    group_version = db.Column(db.Integer, nullable=False)
    occurred_at = db.Column(GROUP_DATETIME, nullable=False, default=datetime.now)
    __table_args__ = (db.Index('idx_group_event_cursor', 'group_id', 'id'),)


class GroupOperation(db.Model):
    """同操作者同幂等键唯一，结果与组织变更同事务提交。"""
    __tablename__ = 'group_operation'
    id = db.Column(db.String(64), primary_key=True)  # SHA-256(actor ID + key)
    actor_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    group_id = db.Column(db.Integer, db.ForeignKey('camp_group.id'), nullable=False)
    request_digest = db.Column(db.String(64), nullable=False)
    result = db.Column(db.JSON, nullable=False)
    created_at = db.Column(GROUP_DATETIME, nullable=False, default=datetime.now)


GROUP_CORE_TABLES = (
    CampGroup.__table__, GroupMembership.__table__,
    GroupRoleAssignment.__table__, GroupMembershipEvent.__table__, GroupOperation.__table__,
)


class LegacyUnitMembershipMap(db.Model):
    """旧单元成员到核心成员的固定映射；旧参与锚点与旧事件继续保留其原始引用。"""
    __tablename__ = 'legacy_unit_membership_map'
    legacy_member_id = db.Column(db.Integer, db.ForeignKey('camp_unit_member.id'), primary_key=True)
    membership_id = db.Column(db.Integer, db.ForeignKey('group_membership.id'), nullable=False, unique=True)
    unit_id = db.Column(db.Integer, db.ForeignKey('camp_unit.id'), nullable=False, index=True)
    migrated_at = db.Column(GROUP_DATETIME, nullable=False, default=datetime.now)

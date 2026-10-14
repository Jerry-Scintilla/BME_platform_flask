"""训练营积分规则版本、发放队列与操作留痕；可用余额仅由积分中心提供。

camp_id 为 NULL 表示平台级规则/发放（每日打卡、发帖、文章过审），不挂营期。
"""
from datetime import datetime
from exts import db


class PointsRule(db.Model):
    __tablename__ = 'points_rule'
    id = db.Column(db.Integer, primary_key=True)
    camp_id = db.Column(db.Integer, db.ForeignKey('camp_session.id'), nullable=True)
    kind = db.Column(db.String(24), nullable=False)
    version = db.Column(db.Integer, nullable=False, default=0)
    __table_args__ = (db.UniqueConstraint('camp_id', 'kind', name='uq_points_rule_camp_kind'),)


class PointsRuleVersion(db.Model):
    __tablename__ = 'points_rule_version'
    id = db.Column(db.Integer, primary_key=True)
    rule_id = db.Column(db.Integer, db.ForeignKey('points_rule.id'), nullable=False, index=True)
    version = db.Column(db.Integer, nullable=False)
    amount = db.Column(db.Integer, nullable=False)
    period_limit = db.Column(db.Integer, nullable=False, default=1)
    enabled = db.Column(db.Boolean, nullable=False, default=False)
    effective_at = db.Column(db.DateTime, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)
    created_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    # 平台每日打卡的时长奖励参数（仅 daily_checkin 读；NULL = 默认每满 2 小时 +1、不封顶）
    bonus_unit_hours = db.Column(db.Integer, nullable=True)
    bonus_amount = db.Column(db.Integer, nullable=True)
    bonus_cap = db.Column(db.Integer, nullable=True)
    __table_args__ = (db.UniqueConstraint('rule_id', 'version', name='uq_points_rule_version'),)


class PointsReward(db.Model):
    __tablename__ = 'points_reward'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    camp_id = db.Column(db.Integer, db.ForeignKey('camp_session.id'), nullable=True, index=True)
    rule_id = db.Column(db.Integer, db.ForeignKey('points_rule.id'), nullable=False)
    rule_version = db.Column(db.Integer, nullable=False)
    kind = db.Column(db.String(24), nullable=False)
    title = db.Column(db.String(100), nullable=False)
    amount = db.Column(db.Integer, nullable=False)
    period = db.Column(db.String(32), nullable=False)
    event_key = db.Column(db.String(160), nullable=False, unique=True)
    idempotency_key = db.Column(db.String(128), nullable=False, unique=True)
    status = db.Column(db.String(24), nullable=False, default='review', index=True)
    reason = db.Column(db.String(512), nullable=False)
    source_id = db.Column(db.String(100))
    source_path = db.Column(db.String(240))
    parent_id = db.Column(db.Integer, db.ForeignKey('points_reward.id'), unique=True)
    proposed_by = db.Column(db.Integer, db.ForeignKey('user.id'))
    reviewed_by = db.Column(db.Integer, db.ForeignKey('user.id'))
    reviewed_at = db.Column(db.DateTime)
    review_note = db.Column(db.String(512))
    center_user_id = db.Column(db.Integer)
    center_email = db.Column(db.String(128))
    transaction_uuid = db.Column(db.String(128), unique=True)
    balance_after = db.Column(db.Numeric(18, 2))
    attempts = db.Column(db.Integer, nullable=False, default=0)
    last_error = db.Column(db.String(200))
    lease_until = db.Column(db.DateTime)
    next_attempt_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now, index=True)
    completed_at = db.Column(db.DateTime)


class PointsEvent(db.Model):
    __tablename__ = 'points_event'
    id = db.Column(db.Integer, primary_key=True)
    reward_id = db.Column(db.Integer, db.ForeignKey('points_reward.id'), nullable=False, index=True)
    actor_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    action = db.Column(db.String(40), nullable=False)
    note = db.Column(db.String(512))
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)

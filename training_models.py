"""营期教学扩展；课程责任、招生位置不进入 CampGroup 核心。"""
from datetime import datetime
from exts import db


class CampMaintenanceFence(db.Model):
    """独立维护锁行；共享请求锁不锁住营期业务行，也不随业务 commit 释放。"""
    __tablename__ = 'camp_maintenance_fence'
    camp_session_id = db.Column(db.Integer, db.ForeignKey('camp_session.id'), primary_key=True)


class TrainingGroupProfile(db.Model):
    __tablename__ = 'training_group_profile'
    unit_id = db.Column(db.Integer, db.ForeignKey('camp_unit.id'), primary_key=True)
    camp_session_id = db.Column(db.Integer, db.ForeignKey('camp_session.id'), nullable=False)
    direction = db.Column(db.String(100))
    course_ids = db.Column(db.JSON, nullable=False, default=list)
    capacity = db.Column(db.Integer)
    # 每张旧招生名片唯一解析到一个组；名单编组不占用该映射。
    recruitment_mentor_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    __table_args__ = (
        db.UniqueConstraint('camp_session_id', 'recruitment_mentor_id', name='uq_training_recruitment'),
        db.CheckConstraint('capacity IS NULL OR capacity BETWEEN 1 AND 30', name='ck_training_capacity'),
    )


class CampTeachingEvent(db.Model):
    __tablename__ = 'camp_teaching_event'
    id = db.Column(db.Integer, primary_key=True)
    camp_session_id = db.Column(db.Integer, db.ForeignKey('camp_session.id'), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    actor_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    action = db.Column(db.String(40), nullable=False)
    course_id = db.Column(db.Integer, db.ForeignKey('course.id'))
    chapter_id = db.Column(db.Integer, db.ForeignKey('chapter.id'))
    before = db.Column(db.JSON)
    after = db.Column(db.JSON)
    reason = db.Column(db.String(500), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)


class TrainingMigrationRun(db.Model):
    """每营一个未结束批次；保存预览、原绑定和恢复校验，不覆盖教学历史。"""
    __tablename__ = 'training_migration_run'
    id = db.Column(db.Integer, primary_key=True)
    camp_session_id = db.Column(db.Integer, db.ForeignKey('camp_session.id'), nullable=False)
    active_slot = db.Column(db.Integer, default=1)
    actor_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    state = db.Column(db.String(20), nullable=False)  # frozen / mapped / switched / restored / cancelled
    preview_digest = db.Column(db.String(64))
    baseline = db.Column(db.JSON)
    mapping = db.Column(db.JSON)
    mapped_digest = db.Column(db.String(64))
    journal = db.Column(db.JSON, nullable=False, default=list)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)
    __table_args__ = (
        db.UniqueConstraint('camp_session_id', 'active_slot', name='uq_training_migration_active'),
        db.CheckConstraint("(state IN ('frozen', 'mapped', 'switched') AND active_slot IS NOT NULL AND active_slot = 1) OR "
                           "(state IN ('restored', 'cancelled') AND active_slot IS NULL)",
                           name='ck_training_migration_state'),
    )

"""独立项目模型；人员事实复用组织核心，营期关联不决定项目生存。"""
from datetime import datetime
from exts import db


class Project(db.Model):
    __tablename__ = 'project_core_project'
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(40), nullable=False, unique=True)
    group_id = db.Column(db.Integer, db.ForeignKey('camp_group.id'), unique=True)
    title = db.Column(db.String(100), nullable=False)
    background = db.Column(db.Text)
    goal = db.Column(db.Text)
    status = db.Column(db.String(20), nullable=False, default='active')
    zero_date = db.Column(db.Date)
    zero_date_confirmed_at = db.Column(db.DateTime)
    timezone = db.Column(db.String(64), nullable=False, default='Asia/Shanghai')
    version = db.Column(db.Integer, nullable=False, default=1)
    migration_ready = db.Column(db.Boolean, nullable=False, default=True, server_default='1')
    created_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)
    __table_args__ = (db.CheckConstraint("status IN ('active','paused','completed','terminated','archived')", name='ck_project_core_status'),)
    __mapper_args__ = {'version_id_col': version, 'version_id_generator': False}


class ProjectChange(db.Model):
    __tablename__ = 'project_core_change'
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey('project_core_project.id'), nullable=False, index=True)
    actor_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    action = db.Column(db.String(50), nullable=False)
    reason = db.Column(db.String(500), nullable=False)
    before = db.Column(db.JSON)
    after = db.Column(db.JSON)
    occurred_at = db.Column(db.DateTime, nullable=False, default=datetime.now)


class ProjectCommand(db.Model):
    __tablename__ = 'project_core_command'
    id = db.Column(db.String(64), primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey('project_core_project.id'), nullable=False, index=True)
    actor_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    digest = db.Column(db.String(64), nullable=False)
    result = db.Column(db.JSON, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)


PROJECT_CORE_TABLES = (Project.__table__, ProjectChange.__table__, ProjectCommand.__table__)


class ProjectScheduleRow(db.Model):
    __abstract__ = True
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey('project_core_project.id'), nullable=False, index=True)
    title = db.Column(db.String(200), nullable=False)
    content = db.Column(db.Text)
    schedule_mode = db.Column(db.String(12), nullable=False, default='absolute')
    due_on = db.Column(db.Date)
    offset_days = db.Column(db.Integer)
    frozen_due_on = db.Column(db.Date)
    actual_on = db.Column(db.Date)
    status = db.Column(db.String(12), nullable=False, default='planned')
    created_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    accepted_by = db.Column(db.Integer, db.ForeignKey('user.id'))
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)


class ProjectMilestone(ProjectScheduleRow):
    __tablename__ = 'project_core_milestone'
    __table_args__ = (
        db.CheckConstraint("schedule_mode IN ('absolute','relative')",name='ck_pc_milestone_schedule'),
        db.CheckConstraint("(schedule_mode='absolute' AND offset_days IS NULL) OR (schedule_mode='relative' AND due_on IS NULL AND offset_days IS NOT NULL)",name='ck_pc_milestone_basis'),
        db.CheckConstraint("status IN ('planned','done','cancelled')",name='ck_pc_milestone_status'),
    )


class ProjectCalendarEvent(ProjectScheduleRow):
    __tablename__ = 'project_core_calendar_event'
    kind = db.Column(db.String(20), nullable=False, default='event')
    __table_args__ = (
        db.CheckConstraint("schedule_mode IN ('absolute','relative')",name='ck_pc_event_schedule'),
        db.CheckConstraint("(schedule_mode='absolute' AND offset_days IS NULL) OR (schedule_mode='relative' AND due_on IS NULL AND offset_days IS NOT NULL)",name='ck_pc_event_basis'),
        db.CheckConstraint("status IN ('planned','done','cancelled')",name='ck_pc_event_status'),
    )


PROJECT_CORE_TABLES += (ProjectMilestone.__table__, ProjectCalendarEvent.__table__)


class ProjectCampBinding(db.Model):
    __tablename__ = 'project_core_camp_binding'
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey('project_core_project.id'), nullable=False, index=True)
    camp_session_id = db.Column(db.Integer, db.ForeignKey('camp_session.id'), nullable=False, index=True)
    legacy_unit_id = db.Column(db.Integer, db.ForeignKey('camp_unit.id'), unique=True)
    leader_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    status = db.Column(db.String(16), nullable=False, default='requested')
    requested_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    requested_users = db.Column(db.JSON, nullable=False, default=list)
    approved_by = db.Column(db.Integer, db.ForeignKey('user.id'))
    started_at = db.Column(db.DateTime)
    ended_at = db.Column(db.DateTime)
    version = db.Column(db.Integer, nullable=False, default=1)
    __table_args__ = (db.UniqueConstraint('project_id','camp_session_id',name='uq_pc_project_camp'),db.CheckConstraint("status IN ('requested','active','ended','rejected')",name='ck_pc_binding_status'))


class ProjectCampParticipant(db.Model):
    __tablename__ = 'project_core_camp_participant'
    id = db.Column(db.Integer, primary_key=True)
    binding_id = db.Column(db.Integer, db.ForeignKey('project_core_camp_binding.id'), nullable=False, index=True)
    project_membership_id = db.Column(db.Integer, db.ForeignKey('group_membership.id'))
    camp_member_id = db.Column(db.Integer, db.ForeignKey('camp_member.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    status = db.Column(db.String(12), nullable=False, default='active')
    started_at = db.Column(db.DateTime, nullable=False, default=datetime.now)
    ended_at = db.Column(db.DateTime)
    __table_args__ = (db.UniqueConstraint('binding_id','user_id',name='uq_pc_binding_user'),db.CheckConstraint("status IN ('active','ended')",name='ck_pc_participant_status'))


class ProjectLegacyMap(db.Model):
    __tablename__ = 'project_core_legacy_map'
    unit_id = db.Column(db.Integer, db.ForeignKey('camp_unit.id'), primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey('project_core_project.id'), nullable=False, index=True)
    binding_id = db.Column(db.Integer, db.ForeignKey('project_core_camp_binding.id'), nullable=False, unique=True)
    write_model = db.Column(db.String(12), nullable=False, default='shadow')
    source_digest = db.Column(db.String(64), nullable=False)
    migrated_at = db.Column(db.DateTime, nullable=False, default=datetime.now)
    project = db.relationship('Project',foreign_keys=[project_id],lazy='joined')
    binding = db.relationship('ProjectCampBinding',foreign_keys=[binding_id],lazy='joined')
    __table_args__ = (db.CheckConstraint("write_model IN ('legacy','shadow','project')",name='ck_pc_write_model'),)


class ProjectAssessmentLink(db.Model):
    __tablename__ = 'project_core_assessment_link'
    camp_milestone_id = db.Column(db.Integer, db.ForeignKey('camp_milestone.id'), primary_key=True)
    binding_id = db.Column(db.Integer, db.ForeignKey('project_core_camp_binding.id'), nullable=False, index=True)
    project_milestone_id = db.Column(db.Integer, db.ForeignKey('project_core_milestone.id'))


PROJECT_CORE_TABLES += (ProjectCampBinding.__table__,ProjectCampParticipant.__table__,ProjectLegacyMap.__table__,ProjectAssessmentLink.__table__)


class ProjectRecord(db.Model):
    __tablename__ = 'project_core_record'
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey('project_core_project.id'), nullable=False, index=True)
    binding_id = db.Column(db.Integer, db.ForeignKey('project_core_camp_binding.id'))
    kind = db.Column(db.String(16), nullable=False)
    title = db.Column(db.String(200), nullable=False)
    content = db.Column(db.Text)
    occurred_on = db.Column(db.Date, nullable=False)
    status = db.Column(db.String(12), nullable=False, default='published')
    created_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    verified_by = db.Column(db.Integer, db.ForeignKey('user.id'))
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)
    __table_args__=(db.CheckConstraint("kind IN ('meeting','record','outcome')",name='ck_pc_record_kind'),db.CheckConstraint("status IN ('published','pending','verified','rejected')",name='ck_pc_record_status'))


class ProjectRecordFile(db.Model):
    __tablename__ = 'project_core_record_file'
    id = db.Column(db.Integer, primary_key=True)
    record_id = db.Column(db.Integer, db.ForeignKey('project_core_record.id'), nullable=False, index=True)
    object_key = db.Column(db.String(500), nullable=False, unique=True)
    filename = db.Column(db.String(200), nullable=False)
    content_type = db.Column(db.String(100))
    size = db.Column(db.Integer, nullable=False)
    sha256 = db.Column(db.String(64), nullable=False)


class ProjectBindingSnapshot(db.Model):
    __tablename__ = 'project_core_binding_snapshot'
    binding_id = db.Column(db.Integer, db.ForeignKey('project_core_camp_binding.id'), primary_key=True)
    snapshot = db.Column(db.JSON, nullable=False)
    frozen_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    frozen_at = db.Column(db.DateTime, nullable=False, default=datetime.now)


class ProjectReminder(db.Model):
    __tablename__ = 'project_core_reminder'
    key = db.Column(db.String(64), primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey('project_core_project.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    sent_at = db.Column(db.DateTime, nullable=False, default=datetime.now)


PROJECT_CORE_TABLES += (ProjectRecord.__table__,ProjectRecordFile.__table__,ProjectBindingSnapshot.__table__,ProjectReminder.__table__)

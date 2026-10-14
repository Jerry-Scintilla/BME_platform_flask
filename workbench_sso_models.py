"""Dedicated workbench one-use authorization codes; no reusable credentials."""
from exts import db


class WorkbenchSSOCode(db.Model):
    __tablename__ = 'workbench_sso_code'
    digest = db.Column(db.String(64), primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    session_id = db.Column(db.String(36), nullable=False)
    person_id = db.Column(db.Integer, nullable=False)
    security_version = db.Column(db.Integer, nullable=False)
    challenge = db.Column(db.String(43), nullable=False)
    redirect_uri = db.Column(db.String(512), nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False, index=True)
    consumed_at = db.Column(db.DateTime)


class WorkbenchAdmission(db.Model):
    """Central application admission, independent of teaching and group slots."""
    __tablename__ = 'workbench_admission'
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), primary_key=True)
    person_id = db.Column(db.Integer, nullable=False, index=True)
    status = db.Column(db.String(20), nullable=False, default='active')
    granted_by = db.Column(db.Integer)
    reason = db.Column(db.String(200), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False)
    revoked_at = db.Column(db.DateTime)

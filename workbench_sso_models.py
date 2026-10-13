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

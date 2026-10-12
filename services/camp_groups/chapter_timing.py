"""认证是否完成仍以认证行为准；时效按组会布置分别留痕，不随改期重算。"""
from datetime import datetime

from exts import db
from models import CampChapterMaterial, CampMeeting, CampMeetingChapterPlan


def iso(value):
    return value.isoformat() if value else None


def snapshot(meeting, certified_at, mentor_id, submitted_at):
    due = meeting.chapter_due_at
    return dict(
        meeting_id=meeting.id, due_at=iso(due), certified_at=iso(certified_at),
        mentor_user_id=mentor_id, first_submitted_at=iso(submitted_at),
        status=('certified' if not due else
                'certified_late' if certified_at > due else 'certified_on_time'),
        submission_status=('no_material' if not submitted_at else
                           'submitted' if not due else
                           'submitted_late' if submitted_at > due else 'submitted_on_time'))


def capture(cert, plans=None):
    """只补尚无快照的布置；改分/改期不得覆盖认证时的结果。"""
    from .training_access import meeting_students
    if not cert.certified_at:
        return
    if plans is None:
        plans = (CampMeetingChapterPlan.query.join(CampMeeting)
                 .filter(CampMeeting.camp_session_id == cert.camp_session_id,
                         CampMeetingChapterPlan.chapter_id == cert.chapter_id).all())
    submitted = db.session.query(db.func.min(CampChapterMaterial.created_at)).filter_by(
        camp_session_id=cert.camp_session_id, student_user_id=cert.student_user_id,
        chapter_id=cert.chapter_id).filter(CampChapterMaterial.created_at <= cert.certified_at).scalar()
    timings = dict(cert.meeting_timings or {})
    for plan in plans:
        if str(plan.id) in timings:
            continue
        meeting = db.session.get(CampMeeting, plan.meeting_id)
        if cert.student_user_id not in meeting_students(
                meeting, assigned_at=plan.created_at, course_id=plan.course_id):
            continue
        timings[str(plan.id)] = snapshot(meeting, cert.certified_at, cert.mentor_user_id, submitted)
    cert.meeting_timings = timings


def payload(plan, meeting, cert, submitted_at=None, *, required=True, now=None):
    if not required:
        return {'status': 'not_required'}
    if cert:
        saved = (cert.meeting_timings or {}).get(str(plan.id))
        if saved:
            return saved
        return dict(status='historical_certified', certified_at=iso(cert.certified_at),
                    mentor_user_id=cert.mentor_user_id, due_at=None,
                    first_submitted_at=None, submission_status='unknown')
    overdue = bool(meeting.chapter_due_at and (now or datetime.now()) > meeting.chapter_due_at)
    return dict(status=('overdue_pending_certification' if submitted_at else 'overdue_pending_submission')
                if overdue else ('pending_certification' if submitted_at else 'pending_submission'),
                first_submitted_at=iso(submitted_at), due_at=iso(meeting.chapter_due_at))


def cert_record(cert):
    if not cert:
        return None
    return dict(score=cert.score, mentor_user_id=cert.mentor_user_id,
                certified_at=iso(cert.certified_at), meeting_timings=cert.meeting_timings)

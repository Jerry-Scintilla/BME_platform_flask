"""项目节点完成口径：多人由负责人评价，单人由管理员验收最新材料。"""
from sqlalchemy.orm import aliased

from exts import db
from models import CampMilestone, CampNodeEvaluation, CampSession, CampSubmissionVersion, CampUnit,ProjectLegacyMap,Project,ProjectCampBinding
from services.camp_groups.reads import UnitMembershipRead


def progress(milestone, unit, members=None, evaluations=None, submissions=None):
    members = (UnitMembershipRead.query.filter_by(unit_id=unit.id, status='active').all()
               if members is None else members)
    targets = {r.user_id for r in members if r.user_id != unit.owner_user_id}
    solo = not targets and any(r.user_id == unit.owner_user_id for r in members)
    evaluations = (CampNodeEvaluation.query.filter_by(milestone_id=milestone.id).all()
                   if evaluations is None else evaluations)
    evaluations = [e for e in evaluations if e.milestone_id == milestone.id and e.member_user_id in targets]
    submissions = (CampSubmissionVersion.query.filter_by(milestone_id=milestone.id).all()
                   if submissions is None else submissions)
    latest = {}
    for row in submissions:
        if row.milestone_id == milestone.id and (row.submitted_by not in latest
                                                or row.version > latest[row.submitted_by].version):
            latest[row.submitted_by] = row
    owner_submission = latest.get(unit.owner_user_id)
    complete = (bool(owner_submission and owner_submission.status == 'approved') if solo else
                bool(targets) and targets <= {e.member_user_id for e in evaluations})
    relevant = {unit.owner_user_id} if solo else targets
    states = {s.status for uid, s in latest.items() if uid in relevant}
    status = ('approved' if complete else 'returned' if 'returned' in states else
              'submitted' if states & {'submitted', 'approved'} else 'open')
    return {
        'node_complete': complete, 'status': status,
        'completion_mode': 'leader_review' if solo else 'member_evaluation',
        'member_count': len(targets), 'evaluated_count': len({e.member_user_id for e in evaluations}),
        'evaluations': evaluations, 'latest_submissions': latest,
    }


def pending_reviews():
    """管理员只审核负责人材料；旧版本、归档营及暂停项目不计入待办。"""
    newer = aliased(CampSubmissionVersion)
    return (CampSubmissionVersion.query.join(CampMilestone)
            .join(CampUnit, CampUnit.id == CampMilestone.unit_id)
            .join(CampSession, CampSession.id == CampUnit.camp_session_id)
            .filter(CampSubmissionVersion.status == 'submitted',
                    CampSubmissionVersion.submitted_by == CampUnit.owner_user_id,
                    CampUnit.unit_type == 'project', CampUnit.status == 'active',
                    CampSession.status.notin_(('archived', 'deleted')),
                    ~db.session.query(ProjectLegacyMap.unit_id).join(Project,Project.id==ProjectLegacyMap.project_id).join(ProjectCampBinding,ProjectCampBinding.id==ProjectLegacyMap.binding_id).filter(
                        ProjectLegacyMap.unit_id==CampUnit.id,ProjectLegacyMap.write_model=='project',
                        db.or_(Project.status!='active',ProjectCampBinding.status!='active')).exists(),
                    ~db.session.query(newer.id).filter(
                        newer.milestone_id == CampSubmissionVersion.milestone_id,
                        newer.submitted_by == CampSubmissionVersion.submitted_by,
                        newer.version > CampSubmissionVersion.version).exists()))

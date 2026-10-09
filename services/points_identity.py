"""商城身份来源：登录教育邮箱，或当前人员正式核验记录中的教育邮箱。

只读已有身份表，不改登录邮箱、人员归属或积分绑定。核验邮箱必须追溯正式登记
的申请证明；不能凭前端传参、通用 verified 状态或自填学号推导。
"""
from dataclasses import dataclass
import re

from exts import db
from models import IdentityApplicationModel, PersonIdentityModel, PersonModel, UserModel

ALLOWED_EMAIL_DOMAINS = ("mail2.sysu.edu.cn", "mail.sysu.edu.cn")
_LOCAL_PART_RE = re.compile(r"^[A-Za-z0-9._+-]+$")
_APPLICATION_REF_RE = re.compile(r"application#([1-9][0-9]*)")


def normalize_campus_email(raw):
    if not isinstance(raw, str):
        return None
    local, sep, domain = raw.strip().rpartition("@")
    local, domain = local.strip(), domain.strip().lower()
    if not sep or not _LOCAL_PART_RE.fullmatch(local) or domain not in ALLOWED_EMAIL_DOMAINS:
        return None
    return f"{local}@{domain}"


@dataclass(frozen=True)
class PointsIdentity:
    email: str | None = None
    source: str | None = None
    error_code: str | None = None
    message: str = "符合本平台 SSO 资格，进入商城时仍需校验关联关系"
    status: int = 200

    @property
    def eligible(self):
        return self.email is not None


def resolve_points_identity(user):
    """每次读库复核；教育邮箱登录优先，保留原有外部绑定的邮箱来源。"""
    if ((getattr(user, 'status', None) or 'active') != 'active' or
            (getattr(user, 'lifecycle', None) or 'active') != 'active' or
            getattr(user, 'account_kind', None) == 'service'):
        return PointsIdentity(error_code='POINTS_ACCOUNT_UNAVAILABLE', status=403,
                              message='当前账号不可使用积分商城，请联系管理员')

    person_id = getattr(user, 'person_id', None)
    person = db.session.get(PersonModel, person_id) if person_id else None
    if (person_id and person is None) or (person and (
            person.record_status != 'active' or
            person.verification_status in ('disputed', 'revoked'))):
        return PointsIdentity(error_code='POINTS_IDENTITY_UNAVAILABLE', status=403,
                              message='当前身份核验状态异常，请到身份中心查看或联系管理员')

    email = normalize_campus_email(user.email)
    if email:
        return PointsIdentity(email=email, source='login_email')

    candidates = set()
    if person and person.verification_status == 'verified':
        identities = PersonIdentityModel.query.filter_by(
            person_id=person.id, issuer='sysu', kind='netid',
            proof_status='verified', assurance_method='school_email').all()
        for identity in identities:
            ref = _APPLICATION_REF_RE.fullmatch(identity.proof_ref or '')
            if not ref:
                continue
            application = db.session.get(IdentityApplicationModel, int(ref[1]))
            if (application is None or application.status != 'approved' or
                    application.school_id != 'sysu' or application.method != 'school_email' or
                    not application.challenge_verified_at or not application.reviewed_at or
                    application.reviewed_by is None or
                    application.claimed_identifier != identity.canonical_key):
                continue
            # 账号归并会迁移登记与 user.person_id，原申请的 person_id 留作历史。
            # 以正式登记及证明账号的当前人员归属为准，避免读他人的核验申请。
            applicant = db.session.get(UserModel, application.applicant_user_id)
            if applicant is None or applicant.person_id != person.id:
                continue
            verified_email = normalize_campus_email(application.contact_email)
            if verified_email:
                candidates.add(verified_email)

    if len(candidates) == 1:
        return PointsIdentity(email=candidates.pop(), source='verified_school_email')
    if len(candidates) > 1:
        return PointsIdentity(error_code='POINTS_IDENTITY_AMBIGUOUS', status=409,
                              message='存在多份不同的教育邮箱核验记录，请联系管理员核对后进入商城')
    return PointsIdentity(error_code='EMAIL_DOMAIN', status=400,
                          message='请先到「个人中心 → 身份中心」完成中大教育邮箱验证及审核，再进入积分商城')

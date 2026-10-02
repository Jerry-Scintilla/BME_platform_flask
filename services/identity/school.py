"""学校核验配置（D3a）：域规则、NetID-邮箱映射、审核人解析（规格 5.1）。

域名必须精确匹配（拒绝字符串包含式判断）；共享/公务/校友域明确排除并给
「转人工」话术。配置实时读取不缓存（与 D1 安全版本同口径，防缓存滞后）。

审核人是数据驱动配置（identity_school_config.reviewer_user_ids）：
- 审批动作要求 actor 在该配置内（super_admin 不旁路——核验负责人制）；
- 审核人必须是管理员账号（2026-10-02 运营规则）：添加时校验、解析与
  审批门槛同步过滤——账号被降级即自动失去审核资格，无需改配置；
- reviewers_ready（≥2 名）只标运营就绪、不拦功能（规格 §18 的就绪前置，
  生产上线前由管理端补齐真人；dev 用 seed 号占位可全流程联调）。
"""
from services.auth_context import AuthRejected
from services.identity.errors import IdentityError
from exts import db
from models import IdentitySchoolConfigModel

# 域名匹配可接受的字符（防域内注入奇怪的元字符；仅小写字母数字与点连字符）
_DOMAIN_CHARS = set('abcdefghijklmnopqrstuvwxyz0123456789.-')


def get_school_config(school_id):
    cfg = db.session.get(IdentitySchoolConfigModel, school_id)
    if cfg is None:
        raise AuthRejected('该学校暂未开放核验，请选择其他学校或联系负责人',
                           status=404, machine='SCHOOL_NOT_CONFIGURED')
    return cfg


def validate_contact_email(cfg, email, claimed_identifier):
    """校验待验证邮箱：个人域精确匹配、共享域排除、NetID-邮箱映射规则。

    返回归一化后的邮箱（registry.canonicalize 同口径：仅域名小写）。
    抛 AuthRejected（400/404 语义），message 不区分「已存在」类细节。
    """
    from services.identity.registry import canonicalize
    normalized = canonicalize('email', email)  # ValueError → 400
    local, _, domain = normalized.rpartition('@')
    if set(domain) - _DOMAIN_CHARS or '..' in domain or not domain:
        raise AuthRejected('邮箱域名不合法', status=400, machine='BAD_EMAIL_DOMAIN')

    excluded = set(cfg.excluded_email_domains or [])
    if domain in excluded:
        raise AuthRejected('公务/共享邮箱不能用于个人核验，请使用个人学生邮箱；'
                           '确无个人邮箱请联系负责人人工核验',
                           status=400, machine='EMAIL_DOMAIN_EXCLUDED')
    personal = set(cfg.personal_email_domains or [])
    if domain not in personal:
        # 精确匹配，不用包含判断（mail2.sysu.edu.cn ≠ sysu.edu.cn）
        raise AuthRejected('请使用本校个人学生邮箱（如 name@mail2.sysu.edu.cn）；'
                           '其他邮箱暂不支持自动核验',
                           status=400, machine='EMAIL_DOMAIN_NOT_ALLOWED')
    if cfg.email_local_matches_identifier and local != claimed_identifier.strip():
        raise AuthRejected('邮箱名与申报的学号/NetID 不一致（应为 NetID@个人域）',
                           status=400, machine='IDENTIFIER_EMAIL_MISMATCH')
    return normalized


def resolve_reviewers(cfg):
    """核验负责人 → 有效 user 列表（过滤已不存在/封禁/非管理员账号）。"""
    from models import UserModel
    out = []
    for uid in cfg.reviewer_user_ids or []:
        u = db.session.get(UserModel, uid)
        if u and (u.status or 'active') == 'active' and u.is_admin():
            out.append(u)
    return out


def require_reviewer(cfg, actor_user):
    """审批动作的审核人门槛：必须在配置名单内且为管理员（super_admin 不旁路）。"""
    if not cfg.is_reviewer(actor_user.id) or not actor_user.is_admin():
        raise AuthRejected('仅该学校配置的核验负责人（管理员）可审核',
                           status=403, machine='NOT_REVIEWER')
    return True


def update_school_config(cfg, *, name=None, personal_email_domains=None,
                         excluded_email_domains=None, email_local_matches_identifier=None,
                         reviewer_user_ids=None):
    """管理端改配置（不 commit）：config_version+1，变更不追溯改写旧记录。

    reviewer_user_ids 全量替换（前端传完整名单）；校验 id 存在且非 service 账号。
    """
    from models import UserModel
    if name is not None:
        cfg.name = name
    if personal_email_domains is not None:
        domains = [d.strip().lower() for d in personal_email_domains if d and d.strip()]
        if not domains:
            raise IdentityError('个人邮箱域清单不能为空')
        for d in domains:
            if d != d.lower() or set(d) - _DOMAIN_CHARS or '..' in d:
                raise IdentityError(f'域名格式不合法：{d}')
        cfg.personal_email_domains = domains
    if excluded_email_domains is not None:
        cfg.excluded_email_domains = [d.strip().lower()
                                      for d in excluded_email_domains if d and d.strip()]
    if email_local_matches_identifier is not None:
        cfg.email_local_matches_identifier = bool(email_local_matches_identifier)
    if reviewer_user_ids is not None:
        ids = sorted({int(i) for i in reviewer_user_ids})
        if not ids:
            raise IdentityError('审核人名单不能为空')
        for uid in ids:
            u = db.session.get(UserModel, uid)
            if u is None or u.account_kind == 'service':
                raise IdentityError(f'审核人 id={uid} 不存在或为服务号')
            if not u.is_admin():
                raise IdentityError(f'审核人必须是管理员账号（id={uid} 不是管理员）')
        cfg.reviewer_user_ids = ids
    cfg.config_version = (cfg.config_version or 1) + 1
    return cfg

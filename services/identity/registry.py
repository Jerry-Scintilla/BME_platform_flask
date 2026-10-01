"""已核验身份登记服务（D2 只建服务不接入口，D3 核验流程调用）。

UNIQUE(issuer, kind, canonical_key) 是学校 key 唯一登记的数据库裁决点：
首插以约束裁决（不依赖「先查不存在」）；冲突不覆盖、不改已提交者，转
IdentityKeyConflict 可恢复冲突（规格 8.2.4）——只回滚登记保存点，外层事务
由调用方决定去留。历史撤销也不自动释放给别人（释放属 D3 人工流程）。

本表只存核验通过的正式登记；待提交的声明值走申请表（D3 identity_application），
自填不占位。写入必须与核验事务同提交（services/identity/events.py 记账本）。
"""
from sqlalchemy.exc import IntegrityError

from exts import db
from models import PersonIdentityModel
from services.identity.errors import IdentityKeyConflict
from services.identity.txn import conflict_savepoint

ISSUER_SYSU = 'sysu'

KINDS = ('netid', 'email', 'roster_ref')


def canonicalize(kind, value):
    """身份 key 归一化——只按机构文档确定规则，禁止猜测性归一化（规格 3.3）。

    - 统一：去首尾空白；空值拒绝；必须是字符串（学校标识禁止整数转换）。
    - netid / roster_ref：原样保留（大小写敏感存储；SYSU NetID 无官方大小写
      折叠规则，roster_ref 是名册稳定引用码）。
    - email：仅域名部分小写（DNS 语义下域名大小写不敏感），本地部分原样——
      禁止通用删除点号/加号后缀等猜测（不同提供方规则不同）。

    D3 接入学校配置版本后，各校规则进配置表（规格 5.1），本函数保持最小实现。
    """
    if not isinstance(value, str):
        raise ValueError('身份 key 必须是字符串（学校标识禁止整数转换，规格 3.3）')
    v = value.strip()
    if not v:
        raise ValueError('身份 key 不能为空')
    if kind == 'email':
        local, _, domain = v.rpartition('@')
        if not local or not domain:
            raise ValueError('email 形式的身份 key 缺少本地部或域名')
        return f'{local}@{domain.lower()}'
    if kind in ('netid', 'roster_ref'):
        return v
    raise ValueError(f'未知的身份 kind：{kind}（可选：{"/".join(KINDS)}）')


def register_identity_key(person, *, issuer, kind, key, assurance_method,
                          proof_ref=None):
    """为人员登记一条已核验身份 key（同事务，不 commit；冲突只回滚保存点）。

    只有核验通过才走到这里——本函数不校验核验过程本身（D3 的 challenge/
    名册核对在上游完成，assurance_method/proof_ref 留核验依据引用）。
    同人重复登记同 key：幂等返回既有行。
    """
    if not isinstance(issuer, str) or not issuer.strip():
        raise ValueError('issuer 不能为空（如 sysu / external:<school>）')
    if kind not in KINDS:
        raise ValueError(f'未知的身份 kind：{kind}')
    issuer_clean = issuer.strip()
    canonical = canonicalize(kind, key)
    row = PersonIdentityModel(
        issuer=issuer_clean,
        kind=kind,
        canonical_key=canonical,
        person_id=person.id,
        proof_status='verified',
        assurance_method=assurance_method,
        proof_ref=proof_ref,
    )
    try:
        with conflict_savepoint():  # 登记保存点：冲突不伤外层事务
            db.session.add(row)
            db.session.flush()
        return row
    except IntegrityError:
        existing = PersonIdentityModel.query.filter_by(
            issuer=issuer_clean, kind=kind, canonical_key=canonical).first()
        if existing is not None and existing.person_id == person.id:
            return existing
        raise IdentityKeyConflict(
            issuer_clean, kind, canonical,
            existing.person_id if existing else None)

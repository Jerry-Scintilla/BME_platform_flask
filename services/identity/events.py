"""身份只追加账本与幂等操作（D2，规格 8.2/15 章/S09/S14）。

账本与业务写同事务、只追加：写入失败即整体回滚，不吞错当成功；代码里没有
UPDATE/DELETE 路径。before/after 只记允许字段（白名单）且值仅限标量——禁止
令牌/OTP/JWT/OIDC code/完整响应/姓名邮箱等敏感或超范围内容入库。

幂等包装 run_idempotent：UNIQUE(actor_user_id, operation_type, idempotency_key)
裁决并发——同 key 同 request_digest 返回既有结果；同 key 不同内容
OperationConflict；同 key 同摘要但仍在 running 转 OperationPending（可恢复，
稍后按 operation_id 查询，规格 8.2.6）。
"""
import hashlib
import json
import uuid

from sqlalchemy.exc import IntegrityError

from exts import db
from models import IdentityEventModel, IdentityOperationModel
from services.identity.errors import OperationConflict, OperationPending
from services.identity.txn import conflict_savepoint

# 账本快照允许字段（规格 15 章「允许字段列表」）：状态/归属/依据类标量。
# 姓名邮箱等 PII 不入账本快照；确需引用走 evidence_refs/proof_ref。
EVENT_FIELD_ALLOWLIST = frozenset({
    'person_id', 'user_id', 'primary_user_id', 'merged_to_person_id',
    'verification_status', 'record_status', 'version',
    'account_kind', 'lifecycle', 'status',
    'issuer', 'kind', 'proof_status', 'assurance_method', 'proof_ref',
})

# target_ids/evidence_refs 允许的键（同为白名单语义，防止把敏感字典整体塞入）
TARGET_ID_KEYS = frozenset({'user_id', 'person_id', 'primary_user_id', 'case_id'})
EVIDENCE_KEYS = frozenset({'challenge_id', 'roster_batch', 'review_id', 'operation_id', 'note'})


def sanitize_snapshot(snapshot):
    """过滤快照到白名单标量字段；未知/非标量值丢弃（宁缺勿泄，规格 15 章）。"""
    if not snapshot:
        return None
    clean = {}
    for k, v in snapshot.items():
        if k in EVENT_FIELD_ALLOWLIST and (v is None or isinstance(v, (str, int, bool, float))):
            clean[k] = v
    return clean or None


def _sanitize_map(raw, allowed_keys):
    if not raw:
        return None
    clean = {}
    for k, v in raw.items():
        if k in allowed_keys and (v is None or isinstance(v, (str, int, bool, float))):
            clean[k] = v
    return clean or None


def record_event(action, *, target_ids=None, before=None, after=None,
                 actor_user_id=None, actor_person_id=None, operation_id=None,
                 case_id=None, evidence_refs=None, reason=None):
    """追加一条身份事件（不 commit；随调用方业务写同事务落库）。

    before/after/target_ids/evidence_refs 均经白名单过滤；过滤后为空的不落列。
    """
    event = IdentityEventModel(
        operation_id=operation_id,
        case_id=case_id,
        actor_user_id=actor_user_id,
        actor_person_id=actor_person_id,
        target_ids=_sanitize_map(target_ids, TARGET_ID_KEYS),
        action=action,
        before=sanitize_snapshot(before),
        after=sanitize_snapshot(after),
        evidence_refs=_sanitize_map(evidence_refs, EVIDENCE_KEYS),
        reason=(reason[:255] if isinstance(reason, str) else None),
    )
    db.session.add(event)
    db.session.flush()  # 撞库/约束问题当场暴露，驱动外层回滚（S09）
    return event


def request_digest(payload):
    """请求内容摘要（幂等冲突判定用）：规范化 JSON 的 SHA-256。"""
    return hashlib.sha256(
        json.dumps(payload or {}, sort_keys=True, ensure_ascii=False,
                   separators=(',', ':')).encode('utf-8')).hexdigest()


def new_operation_id():
    return uuid.uuid4().hex


def get_operation(actor_user_id, operation_type, idempotency_key):
    return IdentityOperationModel.query.filter_by(
        actor_user_id=actor_user_id or 0,
        operation_type=operation_type,
        idempotency_key=idempotency_key,
    ).first()


def run_idempotent(actor_user_id, operation_type, idempotency_key, payload, fn):
    """幂等执行包装（同事务，不 commit）。

    fn(operation) 在登记 running 之后调用，做真正的业务写（含 record_event）；
    返回值 str() 后存 result_ref。并发同 key：唯一约束只准一个 INSERT 成功，
    后来者在保存点回滚后读到已提交行——同摘要且 completed 返回既有 operation，
    running 转 OperationPending，不同摘要 OperationConflict。
    """
    digest = request_digest(payload)
    op = IdentityOperationModel(
        operation_id=new_operation_id(),
        actor_user_id=actor_user_id or 0,
        operation_type=operation_type,
        idempotency_key=idempotency_key,
        request_digest=digest,
        state='running',
    )
    try:
        with conflict_savepoint():  # 抢键保存点：撞并发不伤外层事务
            db.session.add(op)
            db.session.flush()
    except IntegrityError:
        existing = get_operation(actor_user_id, operation_type, idempotency_key)
        if existing is None:  # 并发未提交可见性窗口：按可恢复处理，调用方重试
            raise OperationPending(
                '操作正在并发执行中，请稍后按 operation_id 查询结果')
        if existing.request_digest != digest:
            raise OperationConflict(
                '同幂等键请求内容不同（规格 8.2.6）：换 idempotency_key 重试')
        if existing.state != 'completed':
            raise OperationPending(f'操作仍在 {existing.state}，稍后查询结果')
        return existing, False

    result = fn(op)
    op.state = 'completed'
    op.result_ref = str(result) if result is not None else None
    db.session.flush()
    return op, True

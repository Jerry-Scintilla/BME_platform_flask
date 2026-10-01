"""身份域领域错误（D2）。

全部为「可恢复冲突」语义（规格 8.2.4/8.2.6）：不覆盖已提交者、不吞错当成功；
调用方决定对外形态（用户端不得回显 key 归属人等私有信息，规格 15 章）。
"""


class IdentityError(Exception):
    """身份域错误基类。"""

    machine = 'IDENTITY_ERROR'

    def __init__(self, message, *, machine=None):
        super().__init__(message)
        if machine:
            self.machine = machine


class IdentityKeyConflict(IdentityError):
    """身份 key 已登记（UNIQUE(issuer,kind,canonical_key) 裁决）。

    existing_person_id 仅限内部/管理视角使用；对普通用户只报「已被登记」，
    不泄露归属人（规格 8.2.4：不把已提交者改掉，失败转可恢复冲突）。
    """

    machine = 'IDENTITY_KEY_CONFLICT'

    def __init__(self, issuer, kind, canonical_key, existing_person_id):
        self.issuer = issuer
        self.kind = kind
        self.canonical_key = canonical_key
        self.existing_person_id = existing_person_id
        super().__init__(
            f"身份 key 已登记：({issuer}, {kind}) 冲突，归属 person#{existing_person_id}")


class OperationConflict(IdentityError):
    """同 idempotency_key 不同 request_digest（规格 8.2.6：同 key 不同内容=冲突）。"""

    machine = 'IDENTITY_OPERATION_CONFLICT'


class OperationPending(IdentityError):
    """同 key 同摘要但操作仍在 running（并发进行中或异常残留）——可恢复，稍后查询。"""

    machine = 'IDENTITY_OPERATION_PENDING'

"""治理适配契约：上下文仅用于定位，不能携带客户端宣称的权限。"""
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class GovernanceContext:
    kind: str  # training / project
    scope_id: int  # 由适配器解析的业务对象 ID，核心不解释为营期或单元 ID


@dataclass(frozen=True)
class GovernanceDecision:
    authorization_source: str  # 服务端查出的授权依据，用于审计
    participation_scope: str  # 既有身份宽限的 operation_scope
    participation_scope_id: int


class GovernancePolicy(ABC):
    """适配器必须读取真实绑定与权限；写操作在同一事务锁定治理对象。

    authorize 每次调用（包括幂等重放和读取）重新核验 actor、上下文与组绑定。
    创建时 group=None，适配器检查创建入口；应用层须在同事务建立业务绑定。
    容量、招生窗口、营期只读、课程范围和任职资格均由业务策略判断。
    核心不提供默认放行策略，也不根据 owner 或某个 role 自动授予管理权。
    """

    @abstractmethod
    def authorize(self, *, context, actor, group, action, payload):
        """返回 GovernanceDecision；拒绝返回 None 或抛出 GroupError。"""

    @abstractmethod
    def participant_allowed(self, *, context, group, user):
        """有效业务参与资格（营期适配必须检查有效入营），严格返回 bool。"""

    @abstractmethod
    def role_allowed(self, *, context, group, user, role, scope_type, scope_id):
        """职责、业务对象及课程范围是否合法；不得仅信任请求中的 ID。"""

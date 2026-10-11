"""教学与项目的小组内部服务。生产治理适配由营期应用层显式装配。"""
from .access import GovernanceContext, GovernanceDecision, GovernancePolicy
from .errors import GroupError
from .service import GroupService

__all__ = ['GovernanceContext', 'GovernanceDecision', 'GovernancePolicy',
           'GroupError', 'GroupService']

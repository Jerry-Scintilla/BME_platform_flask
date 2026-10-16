"""独立项目领域；营期仅通过显式关联消费。"""
from flask import current_app
from services.groups import GroupError


class ProjectError(GroupError):
    pass


def enabled():
    return bool(current_app.config.get('PROJECT_CORE_ENABLED', False))


def require_enabled():
    if not enabled():
        raise ProjectError('独立项目功能尚未开放', machine='PROJECT_CORE_DISABLED', status=503)

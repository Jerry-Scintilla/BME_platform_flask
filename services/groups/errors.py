class GroupError(Exception):
    """可映射为 HTTP 响应的领域错误；服务不依赖蓝图、不自行提交事务。"""

    def __init__(self, message, *, machine='GROUP_CONFLICT', status=409):
        super().__init__(message)
        self.machine = machine
        self.status = status

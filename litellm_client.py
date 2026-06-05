"""
LiteLLM Proxy Admin API 客户端封装

通过 master key 调用外部已部署的 LiteLLM Proxy 的管理接口，
用于创建/删除 team、provision internal user、生成/删除 virtual key、
查询用量等。所有方法在网络/接口异常时抛出 LiteLLMError，
由蓝图层捕获后转换为可读的 JSON 响应，避免裸抛 500。

参考部署样式见 AMEII_LLM（config.yaml / docker-compose.yml）。
实际端点以所部署的 LiteLLM 版本为准，首次联调需校验。
"""
import requests
from flask import current_app


class LiteLLMError(Exception):
    """LiteLLM 调用异常"""

    def __init__(self, message, status_code=None, detail=None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.detail = detail


# 默认请求超时（秒）
_TIMEOUT = 30


def _base_url():
    url = current_app.config.get("LITELLM_BASE_URL")
    if not url:
        raise LiteLLMError("未配置 LITELLM_BASE_URL")
    return url.rstrip("/")


def _headers():
    master_key = current_app.config.get("LITELLM_MASTER_KEY")
    if not master_key:
        raise LiteLLMError("未配置 LITELLM_MASTER_KEY")
    return {
        "Authorization": f"Bearer {master_key}",
        "Content-Type": "application/json",
    }


def _request(method, path, json=None, params=None):
    """统一发起请求并处理异常"""
    url = f"{_base_url()}{path}"
    try:
        resp = requests.request(
            method,
            url,
            headers=_headers(),
            json=json,
            params=params,
            timeout=_TIMEOUT,
        )
    except requests.exceptions.RequestException as e:
        raise LiteLLMError(f"无法连接 LiteLLM 服务: {e}")

    if resp.status_code >= 400:
        detail = None
        try:
            detail = resp.json()
        except ValueError:
            detail = resp.text
        raise LiteLLMError(
            f"LiteLLM 接口返回错误 ({resp.status_code})",
            status_code=resp.status_code,
            detail=detail,
        )

    try:
        return resp.json()
    except ValueError:
        return {}


# ==================== Team（项目） ====================

def new_team(team_alias, models=None, metadata=None):
    """创建 team（对应平台「项目」），不设预算。返回含 team_id 的字典。"""
    payload = {"team_alias": team_alias}
    if models:
        payload["models"] = models
    if metadata:
        payload["metadata"] = metadata
    return _request("POST", "/team/new", json=payload)


def delete_team(team_id):
    return _request("POST", "/team/delete", json={"team_ids": [team_id]})


def team_info(team_id):
    return _request("GET", "/team/info", params={"team_id": team_id})


# ==================== Internal User（平台用户） ====================

def provision_user(user_id, user_email=None, max_budget=None, budget_duration=None, models=None):
    """
    确保 LiteLLM 中存在该 internal user（已存在则更新预算）。
    user_id 直接复用平台 UserModel.id 的字符串形式。
    """
    payload = {"user_id": str(user_id)}
    if user_email:
        payload["user_email"] = user_email
    if max_budget is not None:
        payload["max_budget"] = max_budget
    if budget_duration:
        payload["budget_duration"] = budget_duration
    if models is not None:
        payload["models"] = models
    try:
        return _request("POST", "/user/new", json=payload)
    except LiteLLMError as e:
        # 用户已存在：转为更新
        detail_str = str(e.detail).lower() if e.detail else ""
        if e.status_code in (400, 409) and ("already exists" in detail_str or "duplicate" in detail_str):
            return update_user_budget(user_id, max_budget=max_budget, budget_duration=budget_duration)
        raise


def update_user_budget(user_id, max_budget=None, budget_duration=None):
    payload = {"user_id": str(user_id)}
    if max_budget is not None:
        payload["max_budget"] = max_budget
    if budget_duration:
        payload["budget_duration"] = budget_duration
    return _request("POST", "/user/update", json=payload)


def user_info(user_id):
    return _request("GET", "/user/info", params={"user_id": str(user_id)})


# ==================== Virtual Key ====================

def generate_key(key_alias=None, team_id=None, user_id=None, max_budget=None,
                 budget_duration=None, models=None, metadata=None):
    """
    生成 virtual key。
    - 项目 key：传 team_id，不传预算（无限额）。
    - 用户 key：传 user_id，预算继承自 user（不在 key 上单独设）。
    返回含 'key' 字段（sk-...）的字典。
    """
    payload = {}
    if key_alias:
        payload["key_alias"] = key_alias
    if team_id:
        payload["team_id"] = team_id
    if user_id is not None:
        payload["user_id"] = str(user_id)
    if max_budget is not None:
        payload["max_budget"] = max_budget
    if budget_duration:
        payload["budget_duration"] = budget_duration
    if models is not None:
        payload["models"] = models
    if metadata:
        payload["metadata"] = metadata
    return _request("POST", "/key/generate", json=payload)


def delete_key(key):
    return _request("POST", "/key/delete", json={"keys": [key]})


def key_info(key):
    return _request("GET", "/key/info", params={"key": key})


# ==================== 用量查询 ====================

def spend_logs(api_key=None, user_id=None, start_date=None, end_date=None):
    """查询消费明细日志。"""
    params = {}
    if api_key:
        params["api_key"] = api_key
    if user_id is not None:
        params["user_id"] = str(user_id)
    if start_date:
        params["start_date"] = start_date
    if end_date:
        params["end_date"] = end_date
    return _request("GET", "/spend/logs", params=params)


def global_spend_report(start_date=None, end_date=None):
    """全局消费报表（管理员看板用）。"""
    params = {}
    if start_date:
        params["start_date"] = start_date
    if end_date:
        params["end_date"] = end_date
    return _request("GET", "/global/spend/report", params=params)


def list_models():
    """列出 LiteLLM 当前可用模型。"""
    return _request("GET", "/models")

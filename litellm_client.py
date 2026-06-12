"""
LiteLLM Proxy Admin API 客户端封装

通过 master key 调用外部已部署的 LiteLLM Proxy 的管理接口，
用于创建/删除 team、provision internal user、生成/删除 virtual key、
查询用量等。所有方法在网络/接口异常时抛出 LiteLLMError，
由蓝图层捕获后转换为可读的 JSON 响应，避免裸抛 500。

参考部署样式见 AMEII_LLM（config.yaml / docker-compose.yml）。
实际端点以所部署的 LiteLLM 版本为准，首次联调需校验。

【已知 Bug 规避】LiteLLM ≤ v1.84.x 的 reset_budget 后台任务在重置 DB 中
spend=0 后未同步清除 Redis 缓存，导致预算周期到期后用户仍被超限值拦截。
规避方式：在 budget_reset_at 到期后主动调用 reset_user_spend()，通过
POST /user/update spend=0 触发 LiteLLM 内部缓存失效路径。
上游 Issue：https://github.com/BerriAI/litellm/issues/27735
"""
import requests
from datetime import datetime, timezone
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


def reset_user_spend(user_id):
    """
    将用户的 spend 强制归零，同时触发 LiteLLM 内部 Redis 缓存失效。

    规避 LiteLLM ≤ v1.84.x 中 reset_budget 后台任务仅更新 DB、未清除
    Redis spend 缓存的 Bug（Issue #27735）。应在 budget_reset_at 到期后调用，
    以使预算周期刷新立即对请求生效，而无需等待 Redis TTL 自然过期。
    """
    return _request("POST", "/user/update", json={"user_id": str(user_id), "spend": 0})


def reset_user_spend_if_needed(user_id):
    """
    查询用户信息，若 budget_reset_at 已过期且 spend > 0，则自动调用 reset_user_spend。
    返回 (已重置: bool, 用户信息: dict)。
    """
    info = user_info(user_id)
    user_data = info.get("user_info") or info
    if isinstance(user_data, list):
        user_data = user_data[0] if user_data else {}

    if not isinstance(user_data, dict):
        return False, info

    spend = user_data.get("spend", 0) or 0
    reset_at_str = user_data.get("budget_reset_at")

    if not reset_at_str or spend <= 0:
        return False, user_data

    try:
        reset_at = datetime.fromisoformat(reset_at_str.replace("Z", "+00:00"))
        if datetime.now(timezone.utc) >= reset_at:
            reset_user_spend(user_id)
            return True, user_data
    except (ValueError, AttributeError):
        pass

    return False, user_data


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


# ==================== 活动趋势查询 ====================

def user_daily_activity(user_id, start_date=None, end_date=None):
    """查询某用户按日的活动数据（spend / token / 请求数趋势）。"""
    params = {"user_id": str(user_id)}
    if start_date:
        params["start_date"] = start_date
    if end_date:
        params["end_date"] = end_date
    return _request("GET", "/user/daily/activity", params=params)


def team_daily_activity(team_id, start_date=None, end_date=None):
    """查询某 team（项目）按日的活动数据。"""
    params = {"team_ids": team_id}
    if start_date:
        params["start_date"] = start_date
    if end_date:
        params["end_date"] = end_date
    return _request("GET", "/team/daily/activity", params=params)


def model_spend_report(start_date=None, end_date=None):
    """全局按模型分组的用量报告。"""
    params = {"group_by": "model"}
    if start_date:
        params["start_date"] = start_date
    if end_date:
        params["end_date"] = end_date
    return _request("GET", "/global/spend/report", params=params)

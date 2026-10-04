"""积分中心（PointsCenter）外部接入客户端。

按《外部平台接入接口文档》v1.2.0 实现 POINTS-SIGN-V1 协议：每次请求携带
Ed25519 签名的 5 个请求头（X-Platform-ID / X-Key-ID / X-Timestamp / X-Nonce /
X-Signature），时间窗 ±60 秒、nonce 不复用。私钥仅存于服务端配置，不入 git。

本模块只负责"签名 + 请求 + 响应格式校验"，业务语义（谁可以拿 ticket、
错误如何映射给前端）在 blueprints/points_sso.py。异常统一抛 PointsCenterError，
由蓝图层转换为 JSON 响应，避免裸 500。
"""
import base64
import binascii
import hashlib
import json
import time
import uuid
from urllib.parse import urlsplit

import requests
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from flask import current_app

# 私钥按 Flask app 隔离缓存（app.extensions 专用键）：多 app / 测试互不串私钥
EXTENSION_KEY = "points_center"

# 空请求体的 SHA-256 固定值（协议约定，GET 或无 body 时使用）
EMPTY_BODY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

# 协议约定的 ticket 有效期（秒）；上游返回值不符视为版本/配置错位，须停下排查
EXPECTED_TICKET_EXPIRES_IN = 90


class PointsCenterError(Exception):
    """积分中心调用异常。

    category: CONFIG(初始化配置) / REQUEST(本地序列化) / NETWORK(连接失败) /
              TIMEOUT(超时) / HTTP(上游返回非 200) / RESPONSE(响应格式) /
              CONTRACT(响应字段与协议不符)
    status_code: 上游 HTTP 状态码（仅 HTTP 类别）
    detail: 截断脱敏后的诊断摘要，仅供日志排查，不直接透传给浏览器
    retry_after: 上游 429 携带的可信 Retry-After 秒数（可选）
    """

    def __init__(self, message, category="RESPONSE", status_code=None, detail=None, retry_after=None):
        super().__init__(message)
        self.message = message
        self.category = category
        self.status_code = status_code
        self.detail = detail
        self.retry_after = retry_after


def _fail_config(item):
    return PointsCenterError(f"积分中心配置无效：{item}", category="CONFIG")


def _check_url(value, item, fixed_path=None):
    """校验服务端配置的 URL：仅 http/https、有主机、无凭据/query/fragment；
    fixed_path 给定时路径必须精确匹配。"""
    if not value or not str(value).strip() or "XXXX" in str(value):
        raise _fail_config(item)
    try:
        parts = urlsplit(str(value).strip())
    except ValueError:
        raise _fail_config(item)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise _fail_config(item)
    if parts.username or parts.password or parts.query or parts.fragment:
        raise _fail_config(item)
    if fixed_path is not None and parts.path != fixed_path:
        raise _fail_config(f"{item}（路径必须为 {fixed_path}）")
    return str(value).strip()


def init_points_center(app):
    """应用初始化校验。开关关闭时直接返回（不解析私钥、不连上游，零行为变化）；
    开启时校验全部配置，任何一项不合法都拒绝启动（仿 JWT_SECRET 强校验，但
    只在启用时生效）。校验通过仅代表本地格式正确，不代表上游可达。"""
    if not app.config.get("POINTS_CENTER_ENABLED"):
        app.extensions.pop(EXTENSION_KEY, None)
        return

    platform_id = (app.config.get("POINTS_PLATFORM_ID") or "").strip()
    key_id = (app.config.get("POINTS_KEY_ID") or "").strip()
    private_key_b64 = (app.config.get("POINTS_PRIVATE_KEY_B64") or "").strip()
    if not platform_id or "XXXX" in platform_id:
        raise RuntimeError("POINTS_PLATFORM_ID 缺失或为模板占位值，拒绝启动")
    if not key_id or "XXXX" in key_id:
        raise RuntimeError("POINTS_KEY_ID 缺失或为模板占位值，拒绝启动")
    # 标识字段进入签名串，含换行会破坏规范化格式
    if "\n" in platform_id or "\n" in key_id:
        raise RuntimeError("POINTS_PLATFORM_ID / POINTS_KEY_ID 不得包含换行符")

    try:
        raw_key = base64.b64decode(private_key_b64, validate=True)
    except (binascii.Error, ValueError):
        raise RuntimeError("POINTS_PRIVATE_KEY_B64 不是合法 Base64，拒绝启动")
    if len(raw_key) != 32:
        raise RuntimeError("POINTS_PRIVATE_KEY_B64 解码后不是 32 字节 raw 私钥，拒绝启动")
    try:
        private_key = Ed25519PrivateKey.from_private_bytes(raw_key)
    except Exception:
        raise RuntimeError("POINTS_PRIVATE_KEY_B64 无法构造 Ed25519 私钥，拒绝启动")

    # base_url 不附加路径前缀（请求路径由协议常量给出）；callback 路径固定
    try:
        base_url = _check_url(app.config.get("POINTS_CENTER_BASE_URL"), "POINTS_CENTER_BASE_URL")
        _check_url(app.config.get("POINTS_STORE_CALLBACK_URL"), "POINTS_STORE_CALLBACK_URL",
                   fixed_path="/sso/callback")
    except PointsCenterError as e:
        # 初始化配置错误统一拒绝启动，运行期上游错误仍由蓝图映射。
        raise RuntimeError(e.message) from None
    if urlsplit(base_url).path not in ("", "/"):
        raise RuntimeError("POINTS_CENTER_BASE_URL 本版不允许携带路径前缀")

    try:
        connect_timeout = float(app.config.get("POINTS_CENTER_CONNECT_TIMEOUT", 3))
        read_timeout = float(app.config.get("POINTS_CENTER_READ_TIMEOUT", 10))
    except (TypeError, ValueError):
        raise RuntimeError("POINTS_CENTER_*_TIMEOUT 必须为数值")
    if connect_timeout <= 0 or read_timeout <= 0:
        raise RuntimeError("POINTS_CENTER_*_TIMEOUT 必须为正数")

    app.extensions[EXTENSION_KEY] = {"private_key": private_key}


def _sign_and_build_headers(method, path, body_bytes):
    """构造规范化签名串并生成协议要求的 5 个签名头。
    换行符为真实 \\n，末尾恰好保留一个；METHOD 全大写；path 为不含域名与
    query 的 API 绝对路径。"""
    cfg = current_app.config
    state = current_app.extensions.get(EXTENSION_KEY)
    if not state:
        raise PointsCenterError("积分中心未初始化（开关未开启或初始化未执行）", category="CONFIG")

    timestamp = str(int(time.time()))
    nonce = uuid.uuid4().hex  # 每次调用独立生成，ensure 与 ticket 不共用、重试不复用
    body_sha256 = hashlib.sha256(body_bytes).hexdigest()
    canonical = (
        f"POINTS-SIGN-V1\n"
        f"{cfg['POINTS_PLATFORM_ID'].strip()}\n"
        f"{cfg['POINTS_KEY_ID'].strip()}\n"
        f"{timestamp}\n"
        f"{nonce}\n"
        f"{method.upper()}\n"
        f"{path}\n"
        f"{body_sha256}\n"
    )
    signature = base64.b64encode(state["private_key"].sign(canonical.encode("utf-8"))).decode("ascii")
    return {
        "Content-Type": "application/json",
        "X-Platform-ID": cfg["POINTS_PLATFORM_ID"].strip(),
        "X-Key-ID": cfg["POINTS_KEY_ID"].strip(),
        "X-Timestamp": timestamp,
        "X-Nonce": nonce,
        "X-Signature": signature,
    }


def _sanitize_detail(resp):
    """只保留响应结构摘要；上游错误正文可能回显票据，不能靠截断脱敏。"""
    try:
        body = resp.json()
        # 不记录任意字段名或值，避免正文、ticket 或长效凭据进入日志。
        return {"upstream_format": "json", "field_count": len(body) if isinstance(body, dict) else None}
    except ValueError:
        return {"upstream_format": "non_json"}


def _request(method, path, payload=None):
    """签名并发起请求。JSON 只序列化一次：同一份 body_bytes 用于哈希、签名
    与发送，禁止签名后再用 json= 二次序列化（字节不一致会导致验签失败）。"""
    state = current_app.extensions.get(EXTENSION_KEY)
    if not state:
        raise PointsCenterError("积分中心未初始化（开关未开启或初始化未执行）", category="CONFIG")
    cfg = current_app.config

    if payload is None:
        body_bytes = b""
    else:
        try:
            body_bytes = json.dumps(
                payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False
            ).encode("utf-8")
        except (TypeError, ValueError) as e:
            raise PointsCenterError(f"请求体序列化失败: {e}", category="REQUEST")

    headers = _sign_and_build_headers(method, path, body_bytes)
    url = f"{cfg['POINTS_CENTER_BASE_URL'].strip().rstrip('/')}{path}"
    try:
        resp = requests.request(
            method.upper(),
            url,
            headers=headers,
            data=body_bytes,
            timeout=(
                float(cfg.get("POINTS_CENTER_CONNECT_TIMEOUT", 3)),
                float(cfg.get("POINTS_CENTER_READ_TIMEOUT", 10)),
            ),
            allow_redirects=False,  # 上游 3xx 一律按异常处理，签名头不跟随跳转
        )
    except requests.exceptions.Timeout:
        raise PointsCenterError(f"积分中心响应超时 ({path})", category="TIMEOUT")
    except requests.exceptions.RequestException as e:
        raise PointsCenterError(f"无法连接积分中心: {e}", category="NETWORK")

    if resp.status_code != 200:
        retry_after = None
        if resp.status_code == 429:
            try:
                retry_after = max(1, int(float(resp.headers.get("Retry-After", ""))))
            except (TypeError, ValueError):
                retry_after = None
        raise PointsCenterError(
            f"积分中心接口返回错误 ({resp.status_code}, {path})",
            category="HTTP",
            status_code=resp.status_code,
            detail=_sanitize_detail(resp),
            retry_after=retry_after,
        )

    try:
        data = resp.json()
    except ValueError:
        raise PointsCenterError(
            f"积分中心返回非 JSON 响应 ({path})",
            category="RESPONSE",
            detail=_sanitize_detail(resp),
        )
    if not isinstance(data, dict):
        raise PointsCenterError(
            f"积分中心返回格式异常 ({path})",
            category="RESPONSE",
            detail=_sanitize_detail(resp),
        )
    return data


def _norm_email(value):
    return str(value or "").strip().lower()


def ensure_user(email, platform_user_id):
    """开户与绑定（幂等）。固定 allow_rebind=false：冲突必须暴露为 409，
    由上层引导用户联系管理员，绝不静默改绑。"""
    data = _request(
        "POST",
        "/platform-api/v1/users/ensure",
        {"email": email, "platform_user_id": platform_user_id, "allow_rebind": False},
    )
    if data.get("platform_user_id") != platform_user_id:
        raise PointsCenterError(
            "ensure 返回的 platform_user_id 与请求不一致",
            category="CONTRACT",
            detail={"resp_platform_user_id": str(data.get("platform_user_id"))[:64]},
        )
    if _norm_email(data.get("email")) != _norm_email(email):
        raise PointsCenterError(
            "ensure 返回的 email 与请求不一致",
            category="CONTRACT",
            detail={"resp_email": str(data.get("email"))[:128]},
        )
    if data.get("status") != "active":
        raise PointsCenterError(
            f"ensure 返回账号状态异常: {data.get('status')}",
            category="CONTRACT",
        )
    return data


def create_sso_ticket(platform_user_id):
    """申请 90 秒单次有效的 SSO ticket。返回 (ticket, expires_in)。"""
    data = _request(
        "POST",
        "/platform-api/v1/sso/tickets",
        {"platform_user_id": platform_user_id},
    )
    ticket = data.get("ticket")
    if not isinstance(ticket, str) or not ticket.strip():
        raise PointsCenterError(
            "ticket 返回缺失或为空",
            category="RESPONSE",
            detail={"resp_keys": sorted(data.keys())[:10]},
        )
    expires_in = data.get("expires_in")
    if isinstance(expires_in, bool) or not isinstance(expires_in, int) or expires_in <= 0:
        raise PointsCenterError(
            f"expires_in 非法: {expires_in!r}",
            category="CONTRACT",
        )
    if expires_in != EXPECTED_TICKET_EXPIRES_IN:
        # 不自行写死 90 掩盖差异：与上游版本/配置错位时立即停下排查
        raise PointsCenterError(
            f"expires_in={expires_in} 与协议约定 {EXPECTED_TICKET_EXPIRES_IN} 不符，需排查版本/配置",
            category="CONTRACT",
        )
    return ticket, expires_in

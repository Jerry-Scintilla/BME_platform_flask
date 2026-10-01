"""邮件验证码挑战（D1）：用途限定、安全随机、摘要存储、限次与冷却。

规格 8.1/第 6 章要求（D1 全量落地）：
- 注册与找回分离 key（captcha:register:{email} / captcha:findpwd:{email}），
  跨用途不可互换（S08：一项操作一次性消费）；
- 生成改 secrets（原 random.sample(digits*4, 6) 数字池有偏且非密码学安全）；
- 只存 HMAC-SHA256 摘要（专用密钥），恒时比较；
- 5 次错误锁死该 challenge；重发覆盖旧码；60s 冷却 + 每小时 5 次/每日 10 次；
- Redis 不可达 → 503 暂停（不静默放行，规格 6.2）。
"""
import json
import secrets
import string
from datetime import datetime

from services.auth_context import AuthRejected, challenge_digest, constant_time_eq

from exts import redis_client

CAPTCHA_TTL = 300            # 5 分钟
MAX_ATTEMPTS = 5             # 错 5 次锁死该 challenge（重新申请才解锁）
RESEND_COOLDOWN = 60         # 同目标重发间隔（秒）
HOURLY_LIMIT = 5
DAILY_LIMIT = 10

PURPOSES = ("register", "findpwd")


def _require_redis():
    """挑战类流程强依赖 Redis：不可达即 503 暂停（规格 6.2 fail-closed）。"""
    try:
        redis_client.ping()
    except Exception as e:
        raise AuthRejected("验证服务暂不可用，请稍后重试",
                           status=503, machine="SERVICE_UNAVAILABLE") from e


def _rl_keys(email, purpose):
    now = datetime.now()
    return (f"captcha:rl:{purpose}:{email}:{now:%Y%m%d%H}",
            f"captcha:rl:{purpose}:{email}:{now:%Y%m%d}")


def issue_captcha(email, purpose):
    """生成并登记验证码（返回明文仅供发送邮件，绝不回 API 响应）。

    超限/冷却抛 429（带 Retry-After 语义的 machine），不区分「已发送过」细节。
    """
    if purpose not in PURPOSES:
        raise AuthRejected("验证码用途不合法", status=400, machine="CHALLENGE_BAD_PURPOSE")
    _require_redis()
    email = (email or "").strip().lower()

    cd_key = f"captcha:cd:{purpose}:{email}"
    if redis_client.exists(cd_key):
        ttl = redis_client.ttl(cd_key)
        raise AuthRejected(f"发送过于频繁，请 {max(ttl, 1)} 秒后再试",
                           status=429, machine="CHALLENGE_COOLDOWN")
    hour_key, day_key = _rl_keys(email, purpose)
    if (int(redis_client.get(hour_key) or 0) >= HOURLY_LIMIT
            or int(redis_client.get(day_key) or 0) >= DAILY_LIMIT):
        raise AuthRejected("今日验证码发送次数已达上限，请明日再试",
                           status=429, machine="CHALLENGE_LIMIT")

    code = "".join(secrets.choice(string.digits) for _ in range(6))
    payload = {"d": challenge_digest(f"{purpose}:{email}:{code}"), "n": 0}
    redis_client.setex(f"captcha:{purpose}:{email}", CAPTCHA_TTL, json.dumps(payload))
    redis_client.setex(cd_key, RESEND_COOLDOWN, "1")
    for key, window in ((hour_key, 3700), (day_key, 90000)):
        redis_client.incr(key)
        if redis_client.ttl(key) < 0:
            redis_client.expire(key, window)
    return code


def verify_captcha(email, purpose, code):
    """校验并一次性消费；错误计数，5 次锁死。Redis 不可达 → 503。"""
    if purpose not in PURPOSES:
        raise AuthRejected("验证码用途不合法", status=400, machine="CHALLENGE_BAD_PURPOSE")
    _require_redis()
    email = (email or "").strip().lower()
    key = f"captcha:{purpose}:{email}"
    raw = redis_client.get(key)
    if not raw:
        return False
    try:
        payload = json.loads(raw)
    except Exception:
        redis_client.delete(key)
        return False
    expected = payload.get("d", "")
    attempts = int(payload.get("n", 0))
    given = challenge_digest(f"{purpose}:{email}:{(code or '').strip()}")
    if expected and constant_time_eq(given, expected):
        redis_client.delete(key)  # 成功即消费（一次性，S08）
        return True
    attempts += 1
    if attempts >= MAX_ATTEMPTS:
        redis_client.delete(key)  # 锁死：重新申请
    else:
        payload["n"] = attempts
        redis_client.setex(key, CAPTCHA_TTL, json.dumps(payload))
    return False

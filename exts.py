import os
from datetime import datetime, timezone

from dotenv import load_dotenv
load_dotenv()

# flask-sqlalchemy
from flask_sqlalchemy import SQLAlchemy
# flask_mail
from flask_mail import Mail
# flask_limiter
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_redis import FlaskRedis

db = SQLAlchemy()

mail = Mail()

limiter = Limiter(
    key_func=get_remote_address,  # 使用客户端 IP 作为限流键
    storage_uri=os.getenv("REDIS_URL", "redis://localhost:6379/0"),  # 从环境变量读取 Redis 地址（含密码）
    storage_options={"socket_connect_timeout": 30},  # Redis 连接选项
    strategy="fixed-window",  # 限流策略
)

redis_client = FlaskRedis()


# ── JWT 吊销（2026-09-16 安全加固）──
# 登出/刷新轮换时把令牌 jti 写入 Redis blocklist，TTL = 令牌剩余寿命（到期自动清）。
# 放在 exts 而非 app：blueprints 直接 import 会与 app.py 循环。
def revoke_token(jti: str, expires_at: datetime) -> None:
    """把令牌 jti 加入 blocklist；expires_at 为令牌 exp（UTC）。"""
    ttl = max(1, int((expires_at - datetime.now(timezone.utc)).total_seconds()))
    try:
        redis_client.setex(f"jwt:blocklist:{jti}", ttl, 1)
    except Exception:
        # Redis 故障时降级为「本次未吊销」：令牌至多活到自然过期（access 2h），
        # 与限流器同依赖 Redis，故障面一致
        print("[auth] JWT 吊销写入失败（Redis 异常），令牌将存活至自然过期")

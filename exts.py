import os

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

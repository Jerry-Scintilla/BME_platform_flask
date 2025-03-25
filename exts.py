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
    storage_uri="redis://:sdkhujvcbs@localhost:6379/0",  # 使用 Redis 作为存储后端
    storage_options={"socket_connect_timeout": 30},  # Redis 连接选项
    strategy="fixed-window",  # 限流策略
)

redis_client = FlaskRedis()

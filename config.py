# 数据库配置信息
import os
from datetime import timedelta

from dotenv import load_dotenv

load_dotenv()

HOSTNAME = '127.0.0.1'
PORT = '3306'
DATABASE = 'sysu_bme'
USERNAME = os.getenv("DB_USERNAME")
PASSWORD = os.getenv("DB_PASSWORD")
DB_URI = 'mysql+pymysql://{}:{}@{}:{}/{}?charset=utf8mb4'.format(USERNAME, PASSWORD, HOSTNAME, PORT, DATABASE)

SQLALCHEMY_DATABASE_URI = DB_URI

# redis数据库
REDIS_URL = os.getenv("REDIS_URL")

# JWT密匙
JWT_SECRET_KEY = os.getenv("JWT_SECRET")
JWT_ACCESS_TOKEN_EXPIRES = timedelta(days=7)

# 邮箱授权码
# MBWa73BLhWMgkEmJ

# 邮箱配置
MAIL_SERVER = os.getenv("EMAIL_SERVER")
MAIL_USE_SSL = True
MAIL_PORT = 465
MAIL_USERNAME = os.getenv("MAIL_USERNAME")
MAIL_PASSWORD = os.getenv("MAIL_PASSWORD")
MAIL_DEFAULT_SENDER = os.getenv("MAIL_USERNAME")
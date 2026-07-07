# 数据库配置信息
import os
from datetime import timedelta

from dotenv import load_dotenv

load_dotenv()

HOSTNAME = os.getenv("DB_HOST", "127.0.0.1")
PORT = os.getenv("DB_PORT", "3306")
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

# LiteLLM 大模型代理配置
# LiteLLM Proxy 的基础地址，例如 http://127.0.0.1:4000
LITELLM_BASE_URL = os.getenv("LITELLM_BASE_URL", "http://127.0.0.1:4000")
# LiteLLM Proxy 的 master key，用于调用其 Admin API
LITELLM_MASTER_KEY = os.getenv("LITELLM_MASTER_KEY")
# 平台用户默认配额（美元）及重置周期，作为 LiteLLM internal user 的 max_budget 兜底默认值
LITELLM_DEFAULT_MAX_BUDGET = float(os.getenv("LITELLM_DEFAULT_MAX_BUDGET", "5"))
LITELLM_DEFAULT_BUDGET_DURATION = os.getenv("LITELLM_DEFAULT_BUDGET_DURATION", "30d")

# 每日出勤报告（00:00 自动汇总昨日出勤并发邮件）
# 收件人通过 RBAC 权限 attendance_report.recipient 管理（见 /permission/assign）
ATTENDANCE_REPORT_ENABLED = os.getenv("ATTENDANCE_REPORT_ENABLED", "true").lower() == "true"
ATTENDANCE_REPORT_TIMEZONE = os.getenv("ATTENDANCE_REPORT_TIMEZONE", "Asia/Shanghai")
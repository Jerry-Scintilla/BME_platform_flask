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
# 收件人通过 RBAC 权限 attendance_report_recipient 管理（见 /permission/assign）
ATTENDANCE_REPORT_ENABLED = os.getenv("ATTENDANCE_REPORT_ENABLED", "true").lower() == "true"
ATTENDANCE_REPORT_TIMEZONE = os.getenv("ATTENDANCE_REPORT_TIMEZONE", "Asia/Shanghai")

# 对象存储（MinIO / 任意 S3 兼容服务）：课程资源等文件的本体存储，
# 后端只做上传/下载代理，不在本地磁盘持久化文件
MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "127.0.0.1:9000")
MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "minioadmin")
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "minioadmin")
MINIO_BUCKET = os.getenv("MINIO_BUCKET", "bme-course-resources")
MINIO_SECURE = os.getenv("MINIO_SECURE", "false").lower() == "true"

# AI 每日话题（每天 1 篇精挑话题 + 讨论问题，系统账号"BME 资讯君"发布到社区广场 feed）
AI_DAILY_TOPIC_ENABLED = os.getenv("AI_DAILY_TOPIC_ENABLED", "false").lower() == "true"
AI_TOPIC_AUTHOR_EMAIL = os.getenv("AI_TOPIC_AUTHOR_EMAIL", "ai-topic@bme.sysu.edu.cn")
AI_TOPIC_MODEL = os.getenv("AI_TOPIC_MODEL", "deepseek-chat")
# 调用 LLM 的 key：默认回退 master key；生产用 litellm_client.generate_key() 发的 virtual key
AI_TOPIC_LITELLM_KEY = os.getenv("AI_TOPIC_LITELLM_KEY") or LITELLM_MASTER_KEY
AI_TOPIC_CRON_HOUR = int(os.getenv("AI_TOPIC_CRON_HOUR", "8"))
AI_TOPIC_CRON_MINUTE = int(os.getenv("AI_TOPIC_CRON_MINUTE", "30"))
# 发布模式：draft(测试期,不进 feed,admin 审) / published(直接进 feed)
AI_TOPIC_PUBLISH_MODE = os.getenv("AI_TOPIC_PUBLISH_MODE", "draft")
# 信源 RSS（AI 应用 + 教育科技，逗号分隔）；以下为起步候选，上线前务必核实可用性并按需增删
AI_TOPIC_FEED_URLS = [
    u.strip() for u in os.getenv(
        "AI_TOPIC_FEED_URLS",
        "https://www.solidot.org/index.rss,"
        "http://www.ruanyifeng.com/blog/atom.xml,"
        "https://36kr.com/feed"
    ).split(",") if u.strip()
]
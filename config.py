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
# 2026-09-16 安全加固：access 短时效（2h，泄露窗口小）+ refresh 14d 静默续期；
# 退出/轮换即时吊销走 Redis blocklist（app.py），旧前端不感知 refresh 也只是 2h 后重新登录
JWT_ACCESS_TOKEN_EXPIRES = timedelta(hours=2)
JWT_REFRESH_TOKEN_EXPIRES = timedelta(days=14)

# JWT_SECRET 启动强校验：弱/缺失密钥 = 任何人可伪造 token，直接拒绝启动。
# 生成：python3 -c "import secrets; print(secrets.token_urlsafe(48))"
if not JWT_SECRET_KEY or len(JWT_SECRET_KEY) < 32:
    raise RuntimeError(
        "JWT_SECRET 缺失或长度不足 32 字符，拒绝启动。"
        '请在 .env 写入随机密钥：python3 -c "import secrets; print(secrets.token_urlsafe(48))"'
    )

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

# 本地数据根目录：头像/名片照片/文章/作业/错误图等本地文件的统一根（存量默认 ./data 不变）；
# 部署时指到数据盘挂载点即离开系统盘。local 存储后端的附件仓库在其下 storage/ 子目录
DATA_ROOT = os.getenv("DATA_ROOT", "./data")

# 对象存储后端选择：minio（默认，向后兼容）| local（本地磁盘，无需 MinIO 服务）
STORAGE_BACKEND = os.getenv("STORAGE_BACKEND", "minio")

# 官方富文本推文（HTML 正文）总开关：关闭后 HTML 创建/导入接口一律拒绝（回滚用，
# 不影响已发布 HTML 文章的阅读渲染）。独立于前端 VITE 开关，权限不靠前端控制。
ARTICLE_HTML_ENABLED = os.getenv("ARTICLE_HTML_ENABLED", "true").lower() == "true"

# 表单非文件字段上限：Werkzeug 3.1 默认 500KB，会拦掉官方富文本导入的 html 字段
# （原始上限 3MB，见 services/article_html.py）。抬到 4MB 留余量（09-20 修复 413）。
MAX_FORM_MEMORY_SIZE = 4 * 1024 * 1024

# 对象存储（MinIO / 任意 S3 兼容服务）：课程资源等文件的本体存储，
# 后端只做上传/下载代理，不在本地磁盘持久化文件（STORAGE_BACKEND=local 时本组配置不生效）
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
# 数据库配置信息
import os
from datetime import datetime, timedelta

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

# 开发测试账号面板（GET /auth/dev_accounts，登录页快捷登录数据源）：
# debug 模式自动可用；非 debug 环境需显式 DEV_TEST_ACCOUNTS=on（.env），生产不设即关
DEV_TEST_ACCOUNTS = os.getenv("DEV_TEST_ACCOUNTS", "").lower() in ("1", "on", "true")

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

# 内部工作台调度器（feature/work-collab M3）：到期提醒 1 分钟扫描 + 转交过期 5 分钟扫描
WORK_SCHEDULER_ENABLED = os.getenv("WORK_SCHEDULER_ENABLED", "true").lower() == "true"

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
# ── D1 身份安全地基（migrate_62，成员身份确认与重复账号安全迁移）──
# refresh 迁 HttpOnly cookie 总开关：默认关=兼容模式（响应体仍返回 refresh_token，
# 前端 facade 自动探测 /auth/session/config 落兼容分支）。开启后 body 不再返回 refresh。
AUTH_REFRESH_COOKIE_ENABLED = os.getenv("AUTH_REFRESH_COOKIE_ENABLED", "false").lower() in ("1", "on", "true")
# cookie Secure 属性：生产 https 置 true；dev http://127.0.0.1 下 Chrome 可用但 Safari 拒收，默认关
AUTH_COOKIE_SECURE = os.getenv("AUTH_COOKIE_SECURE", "false").lower() in ("1", "on", "true")
# 旧协议 token（无 sid/版本声明）兼容截止（ISO 日期时间，Asia/Shanghai）：
# 未显式设置时按「本次启动 + 14 天」取值并打告警——重启会顺延窗口，上线 SOP 要求显式固定。
_raw_deadline = os.getenv("AUTH_LEGACY_TOKEN_DEADLINE")
if _raw_deadline:
    AUTH_LEGACY_TOKEN_DEADLINE = datetime.fromisoformat(_raw_deadline)
else:
    AUTH_LEGACY_TOKEN_DEADLINE = datetime.now() + timedelta(days=14)
    print("[warn] AUTH_LEGACY_TOKEN_DEADLINE 未显式设置，旧 token 兼容截止取本次启动+14 天"
          f"（{AUTH_LEGACY_TOKEN_DEADLINE:%Y-%m-%d %H:%M}）——生产必须在 .env 固定该值")
# 高风险操作的近期认证窗口（秒）：MFA 停用/恢复码重发等，D3 高风险端点复用
AUTH_RECENT_AUTH_SECONDS = int(os.getenv("AUTH_RECENT_AUTH_SECONDS", "300"))
# 挑战摘要密钥（验证码/恢复码/refresh 的 HMAC）：未设时由 JWT_SECRET 派生（服务端持有，不外发）
CHALLENGE_HMAC_SECRET = os.getenv("CHALLENGE_HMAC_SECRET")
# 媒体短签独立密钥：未设回退 JWT_SECRET_KEY 并告警（runbook 要求独立设置）
MEDIA_SIGN_SECRET = os.getenv("MEDIA_SIGN_SECRET") or JWT_SECRET_KEY
if not os.getenv("MEDIA_SIGN_SECRET"):
    print("[warn] MEDIA_SIGN_SECRET 未设置，媒体短签回退 JWT_SECRET_KEY——生产建议独立密钥")
# MFA 因子加密密钥（Fernet key，python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"）
MFA_ENC_SECRET = os.getenv("MFA_ENC_SECRET")
# 管理端 MFA 强制开关：默认关（D1 只交钩子）；置 true 时 /admin/* 会话须含 totp 因子
MFA_ENFORCE_FOR_ADMIN = os.getenv("MFA_ENFORCE_FOR_ADMIN", "false").lower() in ("1", "on", "true")
if MFA_ENFORCE_FOR_ADMIN and not MFA_ENC_SECRET:
    raise RuntimeError("MFA_ENFORCE_FOR_ADMIN=true 但 MFA_ENC_SECRET 未配置，拒绝启动")
# ── D3 身份核验与认领（migrate_64，规格 13.1：开关必须拆开）──
# UI 总入口：停新入口，已提交申请仍可查询（默认关，随 D4 前端一起开）
IDENTITY_UI_ENABLED = os.getenv("IDENTITY_UI_ENABLED", "false").lower() in ("1", "on", "true")
# 核验通道：停新申请/新挑战，不撤销已核验结果
IDENTITY_VERIFICATION_ENABLED = os.getenv("IDENTITY_VERIFICATION_ENABLED", "false").lower() in ("1", "on", "true")

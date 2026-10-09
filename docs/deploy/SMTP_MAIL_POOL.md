# SMTP 邮箱池：开发与验收说明

本次基于后端 `303b753`（已有 SSO 修复），仅新增 SMTP 邮箱池和配置、测试。
发布目标是 `jiayuan_develop`。本次不修改前端、数据库结构、SSO 规则或内网部署。
邮箱池默认关闭，需验收后配置账号并启用。

## 行为

- 现有 `exts.mail.send` 统一接入：注册/找回密码验证码、身份核验、认领、恢复、名单邀请、通知及报告。
- 支持 1～20 个账号，可以先接 2 个，再增加到 5 个。每次发信选择下一个可用邮箱。
- Redis 原子分配，所有进程共享轮换位置；单个邮箱同时只允许一个发信任务持有租约。
- 发件地址和 SMTP 信封地址均为实际登录的邮箱，显示名默认「BME 训练营」。回复地址、正文和附件保留。
- 每个邮箱有发送间隔、每分钟和每日收件人额度、故障冷却。失败尝试也计入额度，额度按收件人数而非邮件条数计算。
- 连接、TLS 或认证失败发生在发送内容之前，可尝试其他邮箱；默认一封邮件最多尝试 3 个账号。
- 收件人拒收、内容拒收、部分收件人成功以及投递结果不确定时，不自动换邮箱重发。服务端可能已经收信，重复发送会让用户收到多份验证码。
- SMTP 接受之后的连接关闭或 Redis 租约清理失败，不把成功改成失败。
- Redis 不可用或所有邮箱繁忙/限额/冷却时明确报错，不绕过限速，也不回退到旧邮箱。
- 测试模式继续支持 `MAIL_SUPPRESS_SEND` 和 `record_messages`，不连接 SMTP/Redis。

## 准备账号

1. 在 https://mail.163.com/ 按网易要求逐个注册专用邮箱并完成手机验证。
2. 登录每个账号，在「设置 → POP3/SMTP/IMAP」开启 SMTP，生成客户端授权码。
3. 保存邮箱、授权码、管理人和找回方式。授权码不是网页登录密码。
4. 真实授权码仅放部署环境/受保护的 `.env`，不写入源码、GitHub 或运行日志。

手机号可注册/绑定数量以网易当前页面提示为准；本功能不注册邮箱，也不绕过服务商限制。

## 配置

`.env_example` 提供 5 个账号模板。只启用 2 个时，JSON 数组只写实际的 2 个账号。
不要保留未配置的占位账号。例：

```dotenv
MAIL_POOL_ENABLED=true
MAIL_POOL_ACCOUNTS='[{"username":"实际邮箱1@163.com","password_env":"SMTP_AUTH_1"},{"username":"实际邮箱2@163.com","password_env":"SMTP_AUTH_2"}]'
SMTP_AUTH_1=实际邮箱1的SMTP授权码
SMTP_AUTH_2=实际邮箱2的SMTP授权码
MAIL_POOL_SERVER=smtp.163.com
MAIL_POOL_PORT=465
MAIL_POOL_SENDER_NAME="BME 训练营"
MAIL_POOL_NAMESPACE=bme:mail-pool:v1
MAIL_POOL_TIMEOUT=5
MAIL_POOL_MAX_ATTEMPTS=3
MAIL_POOL_MIN_INTERVAL=10
MAIL_POOL_PER_MINUTE=5
MAIL_POOL_PER_DAY=200
MAIL_POOL_FAILURE_COOLDOWN=300
```

需要有效的 `REDIS_URL`。所有使用同一批邮箱的后端进程应共用 Redis 数据库、namespace、账号顺序和额度配置。
不同测试环境使用独立 Redis/namespace 和测试邮箱，避免干扰真实发送额度。
不要通过切换 namespace 或清空 Redis 来重置额度；生产 Redis 应启用适当持久化，避免重启丢失计数。

启用时检查账号配置，空授权码、重复邮箱、错误 JSON、非法数字等拒绝启动。
仅支持带证书校验的 SMTP 隐式 TLS（163 对应 465），不使用明文 SMTP。
默认 SMTP 每次网络操作超时 5 秒，Redis 操作超时 2 秒；这不是整封邮件的总超时。

限额为平台自设保守值，不是网易官方承诺额度。分钟/日计数分别在首次计数后的 60/86400 秒到期，
不是自然日重置，也不是精确滑动窗口。租约自动过期，正常发送结束按持有者标识释放。

## 容量与失败处理

这次是同步发信入口的邮箱池，没有增加持久邮件队列。单邮箱默认至少间隔 10 秒，5 个邮箱一轮可立即处理 5 次发信；
超出可用容量会快速失败。验证码接口沿用现有 503 响应和挑战清理逻辑，用户需稍后重试。
现有批量通知线程对失败收件人记录失败并继续，**不会自动排队补发**；大量群发应另接持久队列，
避免与验证码争抢额度。上线验收应包含实际通知规模，不能把「5 个邮箱」视为无限发送能力。

SMTP 接受仅表示交给邮件服务器，不保证到达收件箱。垃圾邮件、服务商限额和新账号限制需通过真实收信验收。
本次开发与测试不发送真实邮件，不更改内网配置。

日志仅使用不含地址的账号哈希和固定故障代码，不记录 SMTP 原始响应、密码或授权码。
主要错误：`MAIL_POOL_BUSY_OR_LIMITED`、`MAIL_POOL_COORDINATION_UNAVAILABLE`、
`MAIL_POOL_CONNECTION_FAILED`、`MAIL_POOL_RECIPIENT_REJECTED`、`MAIL_POOL_MESSAGE_REJECTED`、
`MAIL_POOL_DELIVERY_UNCERTAIN`、`MAIL_POOL_PARTIAL_DELIVERY`。
其中后两项不能盲目重试整封邮件。

## 隔离测试与上线验收

在已安装后端依赖的 Python 环境中：

```bash
python -m pip install -r scripts/requirements-mail-test.txt
python scripts/test_mail_pool.py
python scripts/test_auth_foundation.py
python scripts/test_identity_verification.py
python scripts/test_points_sso.py
```

邮箱池测试使用 fake SMTP 与 fakeredis 的 Lua 引擎，覆盖轮换、跨实例并发、额度、冷却、TLS 参数、
消息内容、失败切换、模糊投递、部分投递、Redis 故障、旧配置兼容，以及验证码真实路由失败后清理挑战。
测试专用依赖不加入生产 requirements。

2026-10-09 本地验证：邮箱池 21 项、账号认证 62 项、身份核验 19 项、积分商城 SSO 54 项，
共 156 项测试通过。账号认证套件仍输出已有 SQLite 审计写入警告，测试结果为通过。
尚未使用真实 163 账号验证投递，也未部署内网。

验收部署时先备份当前运行配置，使用专用测试账号和收件地址确认注册、找回密码、身份核验及通知收信。
查看原始邮件 From，确认账号轮换和附件可读。真实测试通过后再安排内网上线。
回滚配置可设 `MAIL_POOL_ENABLED=false` 并重启后端，恢复原 `EMAIL_SERVER/MAIL_USERNAME/MAIL_PASSWORD` 单邮箱设置；
只有保留的旧邮箱凭据仍然有效时才可这样回滚。

# 社区标签与阅读周整合

此改动与前端 `jiayuanpush` 配套。支持普通标签、阅读专题与书单、同书提示及主动继续发布、最长 10000 字符的正文/评论、两层显示的回复、作者停止讨论后保留历史评论、精选和今日热帖。500 字仅为阅读活动建议，不阻止发布。标签由管理员统一维护，用户发帖时选用。

## 数据库迁移

在已备份的 MySQL 8 环境先确认讨论内容列支持 `utf8mb4`，以保存 Unicode 表情。连接字符集已配置为 `utf8mb4`，但旧数据库列字符集需按实际环境核对。本地验收使用隔离 SQLite，未连接线上 MySQL。

停止 API 写入后依次执行（不带 `--apply` 可先查看 SQL）：

```sh
python scripts/migrate/migrate_82_community_reading.py --apply
python scripts/migrate/migrate_83_community_discovery.py --apply
```

82 创建标签/书籍/关联表，扩展帖子和评论字段、索引、正文长度，并为历史回复回填根评论；83 创建访问统计表并回填精选时间。脚本支持重复执行。完成后再启动新版 API 与前端；不要在旧表结构上直接启动新版服务。

精选由管理员设置，按精选时间取前三条。热度按滚动 24 小时的「独立读者 + 3 × 独立评论者 + 2 × 独立点赞者」排序，同一账号每帖仅保留最近一次浏览时间，排除作者自访。缓存 15 分钟，返回时再核对内容可见性；无须额外定时任务。访问统计表按帖子与账号唯一，不为每次刷新增加记录。

## 本地验收

```sh
python -m pip install -r requirements-reading-local.txt
python scripts/test_reading_week.py
python scripts/run_reading_local.py --port 5001
```

运行器只连接本地 SQLite / fakeredis，种子账号 `stu1@seed.dev` 至 `stu4@seed.dev` 与 `admin@seed.dev`，密码 `12345678`。不要把此运行器或测试账号用于生产启动。前端真实接口套件另起 5003 端口并使用独立数据库，详见前端 README。

## 只读汇总

无需新增教师评分或导出页面。以下脚本按标签导出姓名、字数、原文及书名、帖子链接等来源信息；普通已删除/隐藏内容排除，作者停止讨论后的可见历史评论仍纳入。

```sh
python scripts/export_reading_week.py --tag 1 --output reading.csv --base-url https://YOUR_HOST/AMEII
```

默认读取配置数据库并设置只读事务；应配只读数据库账号。导出 `.xlsx` 需安装 `openpyxl`（本地验收依赖已包含）。本地验收加 `--sqlite data/reading-local/community.sqlite`。输出文件属于学生数据，不入 Git，保存到仓库外的指定目录。

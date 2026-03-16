# 讨论区功能设计计划（基于现有 Flask 项目）

## 1. 结论（先回答你的核心问题）

- 可以**部分复用**现有 `article` 相关基础，但**不建议直接复用 `ArticleModel` 作为讨论帖主表**。
- 推荐方案：保留 `article` 作为“内容发布/资讯”模块，新增独立 `discussion` 模块；通过 `scope_type + scope_id` 机制把讨论串挂载到 `article/course/group/task` 等对象上。
- 这样能最大化复用现有认证、权限、审计、用户与小组结构，同时避免后续讨论功能扩展受限。

## 2. 现状评估（当前仓库）

基于当前代码，`article` 模块定位更偏“文章发布”而非“社区讨论”：

- `ArticleModel`：`title/introduction/publish_time/url/author_id`，正文通过 `./data/article/*.html` 文件存储。
- `ArticleComment`：仅用于浏览/点赞统计（`view_time/like_time`），不是文本评论。
- `/article/*` 接口多数受 `article_management` 权限保护，普通用户发帖路径不清晰。

这意味着：

- 如果直接改 `ArticleModel`，会把“文章发布”和“用户讨论”耦合在一起。
- 评论楼中楼、引用回复、禁言/锁帖、按课程小组可见等需求难以自然落地。

## 3. 复用策略

### 3.1 可直接复用

- 认证体系：`flask_jwt_extended`（`get_jwt_identity()`）。
- 用户模型：`UserModel`。
- 权限中间件：`check_permission()`。
- 审计日志：`@audit_log`。
- 业务上下文：`CourseGroup`、`Task`、`CourseModel`（用于讨论归属与可见范围）。
- 新模块风格：参考 `task`、`course_group` 的 REST 路由和返回结构。

### 3.2 不建议直接复用

- `ArticleModel` 主表：字段与存储方式不适合讨论帖。
- `ArticleComment`：语义是统计记录，不是评论内容。
- `article_management` 权限：过于管理端导向，不适合全体用户讨论。

## 4. 目标数据模型（建议）

最小可行版本（MVP）建议 3 张主表 + 1 张可选表：

### 4.1 `discussion_thread`（主题帖）

- `id` PK
- `scope_type`：`global|article|course|group|task`
- `scope_id`：关联对象 ID（可为空，仅 `global`）
- `title` varchar(200)
- `content` LONGTEXT（建议存 Markdown/HTML 之一）
- `author_id` FK -> `user.id`
- `status`：`normal|hidden|locked|deleted`
- `is_pinned` bool
- `is_anonymous` bool（可选）
- `reply_count` int（冗余计数）
- `like_count` int（冗余计数）
- `view_count` int（冗余计数）
- `last_reply_at` datetime
- `created_at/updated_at` datetime

索引建议：

- `(scope_type, scope_id, status, last_reply_at)`
- `(author_id, created_at)`

### 4.2 `discussion_reply`（回帖/评论）

- `id` PK
- `thread_id` FK -> `discussion_thread.id`
- `parent_reply_id` FK -> `discussion_reply.id`（支持楼中楼）
- `author_id` FK -> `user.id`
- `content` LONGTEXT
- `status`：`normal|hidden|deleted`
- `like_count` int
- `created_at/updated_at` datetime

索引建议：

- `(thread_id, created_at)`
- `(parent_reply_id, created_at)`

### 4.3 `discussion_reaction`（点赞等互动）

- `id` PK
- `target_type`：`thread|reply`
- `target_id`：目标 ID
- `user_id` FK -> `user.id`
- `reaction_type`：`like`（后续可扩展）
- `created_at` datetime

约束建议：

- `UNIQUE(user_id, target_type, target_id, reaction_type)`（防重复点赞）

### 4.4 可选：`discussion_read_state`（未读/已读）

- `user_id + thread_id` 唯一
- `last_read_reply_id`
- `last_read_at`

用于后续“未读消息数”“我参与的讨论”。

## 5. API 设计草案（REST 风格）

统一前缀：`/discussions`

### 5.1 主题帖

- `POST /discussions/threads`：创建主题
- `GET /discussions/threads`：列表（支持 `scope_type/scope_id/sort/page/per_page`）
- `GET /discussions/threads/{thread_id}`：详情
- `PUT /discussions/threads/{thread_id}`：编辑（作者或管理员）
- `DELETE /discussions/threads/{thread_id}`：删除（软删除）
- `POST /discussions/threads/{thread_id}/lock`：锁帖/解锁（管理员）
- `POST /discussions/threads/{thread_id}/pin`：置顶/取消置顶（管理员）

### 5.2 回帖

- `POST /discussions/threads/{thread_id}/replies`：回复主题
- `GET /discussions/threads/{thread_id}/replies`：回复列表
- `PUT /discussions/replies/{reply_id}`：编辑回复
- `DELETE /discussions/replies/{reply_id}`：删除回复（软删除）

### 5.3 点赞

- `POST /discussions/reactions`：点赞/取消点赞
- `GET /discussions/threads/{thread_id}/reactions/me`：当前用户互动状态（可选）

## 6. 权限与可见性设计

建议新增权限点（沿用现有 `permission` 体系）：

- `discussion_post`：发帖与回帖
- `discussion_moderate`：置顶/锁帖/隐藏
- `discussion_admin`：跨范围管理

可见性规则建议：

- `global`：登录用户可见。
- `article`：可见性跟随文章可见性。
- `course/group/task`：仅相关课程成员或小组成员可见。
- 管理员（`user_mode=admin`）和 `discussion_admin` 可全局查看与处理。

## 7. 分阶段实施计划

## Phase 0：需求冻结（0.5 天）

- 确认首期范围：是否要匿名、附件、@提及、敏感词。
- 明确讨论挂载范围：先做 `group + task`，还是全域都做。

## Phase 1：数据层与迁移（1 天）

- 在 `models.py` 增加 `DiscussionThread/DiscussionReply/DiscussionReaction`。
- 增加 Alembic 迁移脚本（含索引和唯一约束）。
- 初始化新权限（迁移或管理脚本）。

## Phase 2：核心接口（2 天）

- 新建 `blueprints/discussion.py` 并注册到 `app.py`。
- 实现主题帖 CRUD、回复 CRUD、点赞接口。
- 接入 `jwt_required`、`check_permission`、`audit_log`。

## Phase 3：业务规则完善（1 天）

- 软删除与计数回填逻辑。
- scope 访问控制（按 group/task 成员校验）。
- 锁帖后禁止普通用户回复。

## Phase 4：文档与联调（1 天）

- 补充 `apidocs/discussion/*.yaml`。
- 与前端联调分页、排序、状态字段。

## Phase 5：测试与发布（1 天）

- 单测/集成测试：权限、边界、并发点赞幂等。
- 灰度上线，观察慢查询与错误率。

## 8. 与 article 的关系（推荐最终形态）

- `article` 保持“内容发布”。
- `discussion` 负责“互动讨论”。
- 通过 `scope_type='article' + scope_id=<Article_Id>` 让文章拥有讨论区，而不是把讨论强塞进文章表。

这样后续可以无缝扩展到课程、小组、任务的讨论，而不破坏原有文章逻辑。

## 9. 首迭代（MVP）建议范围

- 必做：主题帖、回复、点赞、删除、分页、按时间排序。
- 必做：`group` 范围权限校验（成员可读写，非成员不可见）。
- 可暂缓：附件、匿名发帖、敏感词、@提及、消息通知、未读状态。

## 10. 风险与规避

- 风险：把讨论和 article 强耦合，后期 scope 扩展成本高。  
  规避：从第一版就使用独立 `discussion_*` 表。

- 风险：权限规则复杂导致越权。  
  规避：在 service 层统一封装 `can_view/can_post/can_moderate` 判断。

- 风险：计数不一致（reply_count/like_count）。  
  规避：写路径实时更新 + 定时校准脚本双保险。


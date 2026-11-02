# 反馈工单现场与处理契约（2026-11-01）

用户表单仍走 `POST /feedback-tickets`，multipart 向后兼容 `image`，新增 `snapshot` 与 `page_context`。
两类图片各一张、各最多 5MiB，PNG/JPEG/WebP/GIF；全部附件校验后才写入，存储失败回滚工单并尽力清理已写对象。
页面信息 JSON 上限 8KiB，只保存允许的定位字段与导航 query，拒绝非 HTTP(S) 地址，不保存 URL hash/认证参数。

本轮不增加数据库列、无需新增结构迁移。复用 `feedback_ticket_event` 的 created metadata 保存
`page_context` 与 `snapshot_attachment_id`；文件仍在 `feedback_ticket_attachment` 与原 storage。
用户/管理详情增加 `page_context`，附件元数据增加 `kind`（snapshot/upload），历史记录兼容空上下文和 upload。
附件访问继续短签/JWT 双通道及提交人/管理员归属校验；用户详情不回内部备注。

管理列表新增 `queue=inbox|active|waiting|done` 与 `assignee=mine|unassigned`，原 status/category/priority/assignee_id/q/page/page_size 保持兼容。
新增受原 system_management 保护的 `/admin/feedback-tickets/stats` 和 `/staff`。
管理详情返回 `allowed_transitions`（new 不暴露受理/撤回的通用迁移入口）与 `can_triage`。

管理写入可带 `expected_status`；后端取得工单行锁后判断，过期返回 409。旧客户端不传时仍兼容。
受理/保存先完整校验字段和有效管理员，避免失败请求留下部分修改；只在首次受理或实际改派时通知。
waiting_user 转换要求 reason，公开说明与状态在同一事务落库；用户补充后回 in_progress。
解决方式限定已定义枚举；前端要求填写结果说明，后端继续兼容旧客户端的可选 summary。
重开清除当前解决字段和完成时间，旧结果仍保留在事件中。事件同时更新工单 updated_at。

先部署兼容后端，再发布配套前端；没有生产操作授权时不执行部署或业务数据修改。

隔离检查：`PYTHONPATH=.:scripts .venv/bin/python -m unittest test_feedback_workflow`。
该检查只创建 SQLite 内存表、模拟文件存储/通知，不导入生产 app；不替代 MySQL 行锁与真实 storage/代理联调。

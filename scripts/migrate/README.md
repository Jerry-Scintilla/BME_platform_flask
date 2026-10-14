# 数据库迁移脚本（生产上线用）

本目录承载 **2025-06-12 基线 → 座位 / 营期 / RBAC 版本** 的全部 schema 与权限迁移。
全部**幂等**，可重复运行。脚本自带 sys.path 修正，**必须在项目根目录执行**。

## 执行顺序（生产原地升级老库）

```bash
cd /opt/BME_platform_flask          # 项目根
python scripts/migrate/migrate_01_user_role.py          # user 加 role 列 + admin→super_admin 回填
python scripts/migrate/migrate_02_camp_schema.py        # 建 6 营期表 + 4 老表加 6 列
python scripts/migrate/migrate_03_feature_camp.py       # camp_session.is_featured + camp_join_request 表
python scripts/migrate/migrate_04_join_selected_days.py # camp_join_request.selected_days
python scripts/migrate/migrate_05_perms.py              # 17 下划线权限 + super_admin 授权
python init_seats.py                                    # 建 study_room/seat 表 + 106 房 40 座位
python scripts/migrate/migrate_06_check_record_indexes.py  # check_record (user_id, date) 复合索引
python scripts/migrate/migrate_09_mentor_selection.py     # camp_session 加选导生 6 列 + 3 张选导生表
```

## 迁移 79：个人任职历史（2026-10-14，本地开发）

`migrate_79_appointment_history.py` 追加人员/职位类别快照、来源说明、已知年份和登记时间，允许历史任职日期不详。已有记录标为 legacy；登记时间只从实际任命/卸任事件回填，没有事件时留空，保留旧任期日期和状态。

默认不连接数据库；只有加 `--apply` 才操作 config 指向的库。先备份并确认目标。应先执行来源迁移 79 和独立工作台 Alembic 0003，再发布两边的新代码。MySQL 验证使用 `scripts/test_appointment_history_mysql.py --run`，严格限制回环主机、创建唯一临时测试库并在结束后清理；不修改本地业务库。新 schema 支持未知日期，回退旧 NOT NULL schema 需要单独制定保留数据的恢复方案。
## 顺序依赖

### 迁移80：独立工作台应用准入

`migrate_80_workbench_admission.py` 默认只展示计划，--apply 才建立 workbench_admission。新表仅记录认证账号、人员标识及准入审计，不保存工作台小组关系，不修改教学角色、正式职务或原两槽归属。已有准入被撤销后普通导入不能恢复。先迁移，再启动新版SSO提供端；工作台同时需升级0004。发布及名单录入交接见独立工作台deploy/README.md。

- `migrate_05` 按 `role='super_admin'` 授权 → 必须在 `migrate_01`（加列 + 回填）之后。
- `migrate_03/04` 依赖 `migrate_02` 建出 camp_session / camp_join_request。
- 全部幂等，顺序错了也不报错，但推荐按序。

## 不要跑
- `seed.py` 会灌测试账号 / 演示课程 / 测试通知，生产**禁整体跑**。权限已由 `migrate_05` 处理。
- 本目录**不含**测试 / 清理脚本（`dev_*` 为本地自用冒烟脚本，gitignored 不入库，用完即删）。

## 小组核心阶段 1（2026-10-10）

新增 `migrate_72_group_core.py`，仅负责五张小组核心表。默认输出 MySQL DDL，不连接数据库；
`--apply` 才显式建表，不执行成员回填或业务切换。尚未加入批量上线脚本。
应用现有 `create_all()` 启动逻辑也可能补建这些新表，发布前需按 [服务契约](../../docs/group-core.md)
确认接入与身份归并依赖。空表可在停止相关代码后按 operation、event、role、membership、group
顺序移除；已有数据时先备份并制定恢复方案，不能直接删除。

## 回滚

阶段 2 新增 `migrate_73_camp_group_binding.py`：为 camp_unit 增加唯一 group_id 绑定，
建立 legacy_unit_membership_map，并为核心成员的 user/person 复合外键补充 ON UPDATE CASCADE。
默认离线打印 DDL，显式 `--apply` 才修改结构，不搬迁成员。既有 app.create_all 不能给
camp_unit 补列，故必须先完成结构迁移再部署本版本业务代码；本轮未执行结构迁移。
`scripts/preview_camp_groups.py` 只读预览项目单元或培训营，无写入选项。
成员映射后的恢复需连同 group_id、映射表、核心成员与新业务事件一起处理，不能直接删列回退。

- 加列均 nullable / 有默认值，回滚用 `ALTER TABLE ... DROP COLUMN ...`，不伤老数据。
- 新表回滚 `DROP TABLE ...`。
- **升级前务必 `mysqldump` 备份**，这是回滚底气。

## 各脚本做了什么
| 脚本 | 动作 |
|---|---|
| 01_user_role | `ALTER user ADD role VARCHAR(20) NOT NULL DEFAULT 'student'` + `UPDATE user SET role='super_admin' WHERE user_mode='admin'` |
| 02_camp_schema | create_all 建 6 营期表 + 给 user_course / medal_user / check_record / notification 加 6 列（INT NULL） |
| 03_feature_camp | `camp_session.is_featured` + 建 camp_join_request 表 |
| 04_join_selected_days | `camp_join_request.selected_days TEXT` |
| 05_perms | 17 下划线权限 + 删点号旧权限 + super_admin 全授权 |
| 06_check_record_indexes | `CREATE INDEX ix_check_record_user_date ON check_record (user_id, date)`（幂等） |
| 09_mentor_selection | camp_session 加 6 列（enabled + 4 时间点 + ms_tags）；create_all 建 camp_mentor_profile / preference / match 3 表；建 ./data/mentor_photos/ |
| 36_showcase_media | `ALTER showcase_project ADD images_json TEXT NULL`（XLAB 图集列；cover 列 22 建表已有） |
| 37_community_redesign | article_v2 加 cover_image_key/is_official；discussion_thread 加 images_json（社区重设计：推文封面标记 + 帖子图集） |
| 38_community_governance | discussion_thread 加 category/project_id/pinned_until（话题标签、关联 XLAB 项目、置顶过期） |
| 39_essence | discussion_thread/article_v2 加 is_essence（精华标记，热度 ×2） |

## 培训小组阶段 3（2026-10-11）

`migrate_74_training_groups.py` 依赖 72/73，默认离线输出 DDL；`--apply` 仅增加培训扩展、
教学事件表，以及营期切换标记、学习资格、考勤义务、招生位置和课程责任列。不会回填人员，
不会打开旧营切换标记。本轮未执行任何 apply。

部署前必须先完成全部结构变更；`create_all()` 不会补齐这些旧表新增列。
新创建的培训营采用统一小组结构，既有培训营仍沿用兼容路径，必须经阶段 4 显式映射后切换。
已有新组、职责或教学事件时，不能通过删表或清空切换标记回滚；恢复须基于完整备份和映射账本。

## 旧培训切换工具（阶段 4，2026-10-11）

`migrate_75_training_cutover.py` 依赖 72/73/74，默认离线 DDL；`--apply` 仅增加按营暂停列和
映射批次表及按营共享/独占栅栏锁表，不回填名单、不切换旧营。本轮未执行任何数据库 apply。

`scripts/training_group_cutover.py --camp <ID>` 默认只读预览；另有 freeze/map/verify/switch/
resume/restore/export。写操作需显式 `--apply --actor`，映射必须携带预览摘要和批次 ID。
原预览工具的 training-camp 入口已使用同一份完整预览，仍没有写参数。
运行前必须部署全部维护栅栏及调度跳过代码，排空旧版本进程和不受控外部写入；操作步骤、
课程裁定格式、恢复限制与检查范围见 [小组核心契约](../../docs/group-core.md#阶段-4旧培训名单映射切换与恢复2026-10-11)。
不能直接翻转旧营标记或删列作为映射恢复；已有新业务数据时工具拒绝直接退回旧结构。

## 上线准备补充（2026-10-11，全量回归授权后）

结构迁移链更新为 **72 → 73 → 74 → 75 → 76**。`migrate_76_camp_task_time_precision.py`
将 `camp_meeting_task.created_at` 与 `camp_meeting_chapter_plan.created_at` 扩为 DATETIME(6)，
与新职责生效时间保持一致，避免同秒任务被误认成加入前任务。72 的核心时间字段也采用微秒，
并支持修正早期开发库的低精度列。旧时间不重算、不伪造更精细的历史时间。

已在全新本机 MySQL 9.6 隔离库实际执行 72–76 两遍并通过 53 项测试；此前各阶段的
“未执行 apply”指当时状态，本轮也未对生产或共享业务库执行 apply。
完整备份、停旧进程、发布及恢复顺序见 [上线指南](../../docs/deploy/营期小组上线指南-202610.md)。
上面的通用 DROP COLUMN / DROP TABLE 说明不适用于已有组业务写入的营期小组升级。

- `migrate_78_points_rewards.py`：积分规则版本、奖励队列和审计事件。默认打印 DDL，`--apply` 才建表；不发积分。上线后另行配置 `POINTS_REWARDS_ENABLED` 与规则。

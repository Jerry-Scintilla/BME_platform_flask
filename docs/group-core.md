# 教学与项目共用小组核心服务契约

2026-10-11，阶段 5 兼容收口。通用核心位于 `group_models.py`、`services/groups/`，营期与项目适配位于
`services/camp_groups/`。新项目审核、成员管理与相关读取已接入核心；存量项目需显式映射后才能
写入。培训功能已接入；既有培训营仍保留兼容路径，实际名单映射与切换须经阶段 4 工具显式执行。本轮未连接业务数据库或执行生产迁移。

## 边界与依赖

核心组实体命名为 `CampGroup`，物理表为 `camp_group`，营期绑定关系为 `CampUnit.camp_group`。
名称限定教学营与项目营共用的业务范围；成员、职责与事件仍使用 `GroupMembership`、
`GroupRoleAssignment`、`GroupMembershipEvent` 等内部名称。社团和工作区不属于接入目标。

核心管理组、成员、限定范围的职责、负责人和变更事件，依赖账号与 Person 身份层。
没有营期、课程业务外键，不导入业务蓝图，不调用社团或工作区的成员与权限服务。
课程、招生、考勤、认证、项目交付、通知仍由消费方持有。核心提供的成员和职责不能
替代课程入读关系，也不能给本人认证提供授权。一个人同营同课仍只建立一份修读关系。

调用方向：营期应用服务 → 培训或项目治理策略 → GroupService → 核心表与身份层。
`models.py` 统一注册新模型；物理模型拆入独立文件，避免继续扩大单文件定义。
没有新增公共小组 HTTP 端点、全局组管理权限或前端入口。既有项目端点装配 ProjectGovernancePolicy，
按真实 CampUnit 绑定和 CampSession 状态授权；培训策略现已装配，既有营须完成映射后才可通过统一培训入口写入核心。

## 数据约束

| 表 | 约束及用途 |
| --- | --- |
| camp_group | 名称、active/archived、治理负责人、version；负责人账号必须是有效成员，由服务在创建和转交时保证 |
| group_membership | group/user 与 group/person 分别唯一；复合外键校验 user/person 配对；ended 留存，再加入复用成员 ID |
| group_role_assignment | 成员外键带 group，防跨组挂职责；同成员、职责、范围最多一个未结束槽；历史行不删除 |
| group_membership_event | 追加动作、操作者、服务端授权依据、上下文、原因及状态快照；与组织变更同事务 |
| group_operation | SHA-256(actor ID + 幂等键) 主键；请求摘要和结果同事务保存；不同内容复用同键报冲突 |

职责固定为培训 `learner`、`mentor` 和项目 `project_member`、`project_leader`。
培训可限定 `group/0` 或 `course/正整数 ID`，项目仅限 `group/0`。
课程 ID 是否属于真实教学配置，由策略核验；核心不把客户端提供的 ID 当成有效授权。
治理负责人单独存储，变更负责人不自动增加教学或项目职责。

职责有效期使用与既有模型一致的本地无时区 datetime。读取同时检查成员活动状态、
账号生命周期、人员关联、当前业务参与、职责时间与业务范围。`active_slot=1` 单独不代表
当前仍有权限。到期后重新授予会结束旧槽并创建新任命；更改未到期职责的期限须先结束原任命。
退出会结束全部未关闭职责，再加入不会恢复它们。结束负责人参与前必须先转交。
归档组整体只读，保留历史成员和职责记录。archive_group(end_memberships=True) 在项目终止时
原子结束全员（含治理负责人）的参与与职责；普通归档默认只冻结。本阶段不提供恢复归档或物理删除接口。

## 治理策略

由服务器装配 `GroupService({'training': training_policy, 'project': project_policy})`。
构造时只接受 `GovernancePolicy` 实现；未注册的上下文一律拒绝，不能由请求注册或选择任意策略。
`GovernanceContext(kind, scope_id)` 只传对象定位信息，没有 `is_admin` 等授权声明。

适配器必须实现三个方法：

| 方法 | 必须履行的职责 |
| --- | --- |
| authorize | 每次从可信数据解析治理对象、组绑定和操作权限；写操作锁定治理对象并校验业务窗口、只读状态；返回服务端授权依据及身份宽限范围 |
| participant_allowed | 目标人员当前具有有效业务参与资格；营期策略必须核验有效入营关系 |
| role_allowed | 职责符合业务类型和资格，作用范围属于实际课程或项目；读取有效职责时也会调用 |

创建时 `group=None`，策略检查业务创建入口。应用服务必须在同一外层事务中建立营期绑定，
否则回滚创建。阶段 2 的生产适配应在创建服务完成后验证绑定存在，再提交；不能暴露独立的
无绑定创建接口。幂等重放仍重新授权，创建重放通过已创建组的 `read_group` 权限判断。

读操作名为 `read_group`、`read_members`、`read_roles`、`read_events`，可分别授权；
事件包含原因与历史，不能仅凭普通成员身份开放。读历史与当前写权限必须分开。
`effective_roles` 只返回过滤后的职责事实，消费方仍须判断具体动作、对象及禁止自评等规则。
核心不会自动把负责人、导生或项目组长解释为平台管理员。

## 内部服务接口

全部写操作接受 `context`、`actor_user_id`、`idempotency_key`、`reason`。
除创建外，还必须提供 `group_id` 和 `expected_version`。返回 `group`、可选 `membership`、
`detail` 及 `event_id`；重放返回原结果，最新状态需另读。

| 接口 | 业务参数 |
| --- | --- |
| create_group | name、owner_user_id；创建组及负责人参与，不自动任命为导生或组长 |
| add_member | user_id；重复加入幂等，退出后再加入不复制成员行 |
| end_membership | user_id；结束参与及全部职责，不退营、不删除课程事实 |
| grant_role | user_id、role、scope_type、scope_id、可选 expires_at |
| end_role | user_id、role、scope_type、scope_id；可以撤销已不符合资格者的旧职责 |
| transfer_owner | user_id；新负责人必须为有效成员，且通过身份及任职资格检查 |
| rename_group | name |
| archive_group | 无额外业务参数 |

`get_group`、`list_members`、`effective_roles`、`list_events` 均须提供治理上下文和操作者。
成员与事件列表使用 `after_id` 游标，默认 100、最多 200 条；列表结果为必要字段投影，不返回账号资料。
`list_members(include_ended=True)` 可读取结束参与的历史行；当前有效授权应使用 `effective_roles`。

领域错误 `GroupError` 提供 `machine` 和 `status`，由应用层映射为既有 HTTP 错误格式。
主要冲突包括重复人员、旧版本、重复幂等键内容不一致、组只读及业务范围不匹配。

## 身份与事务

新参与沿用 `IDENTITY_ENFORCEMENT_MODE` 的 off/shadow/enforce 及既有参与宽限。
任命导师、项目组长与治理负责人调用现有 `appoint` 门槛；普通学习或项目参与不新增任职核验要求。
核验通道关闭时的降级继续由既有身份层控制。

已合并/停用/封禁账号不新增授权；同组同 Person 只能对应一个成员，即使 shadow 也不能新建
第二份人员关系。未建 Person 的历史账号在 shadow/off 下允许 person_id 为空并按账号去重；
通过 `add_member` 可补齐同一账号的空锚点。既有非空锚点不被静默改写。
阶段 2 已将核心关系纳入账号空壳扫描。经批准的人员归属变更可通过复合外键级联保持账号/Person
一致，并增加身份关联事件；同组已有另一账号参与或仍有旧营期/单元锚点时明确拦截，须先完成
人员映射裁定。不会自动合并两份成员、转移主参与账号或恢复职务。

写操作在保存点内执行，依次锁定按 ID 排序的账号、Person、小组，再交给治理策略锁定业务对象。
应用层接入必须统一锁序，不能一条路径先锁营期、另一条路径先锁组。账号锁串行化同操作者的
幂等请求；组行锁串行化组织修改；版本检查及唯一约束兜底。每个成功的新命令增加组版本并记录
事件（自然幂等的重复加入也记录该次命令），同幂等键重放不增加版本或事件。

服务仅 flush，不提交外层事务、不发送通知。失败回滚本次保存点；应用服务须回滚完整业务操作，
避免只提交报名或绑定的一半。SQLAlchemy 打开保存点前会刷新调用方已有待写对象，调用方应先
处理自身异常边界。MySQL 死锁等数据库错误由应用层按完整事务重试，不能局部重放部分写入。

## 建表与接入状态

`scripts/migrate/migrate_72_group_core.py` 默认只输出五表 MySQL DDL，不连接数据库；
显式 `--apply` 才按配置幂等创建缺失表。只建立核心表，不改 CampUnit，不迁移历史成员。
已有表不会自动补列或修复不同版本结构。当前开发未执行 `--apply`。

注意既有 `app.py` 启动时会执行 `db.create_all()`，加载这些模型后重启应用也可能补建缺失新表。
本阶段没有启动应用；生产发布仍应先走迁移审批与结构核对。新项目只有在结构准备完成后才能建立核心成员；
未映射旧单元仍通过集中读取适配返回历史记录。未将迁移 72 加入批量上线脚本；下一阶段再纳入完整切换安排。

阶段 2 增加迁移 73：CampUnit.group_id 唯一绑定、旧成员映射表以及人员复合外键的更新级联。
脚本默认离线输出 DDL，--apply 仅改结构，不搬迁成员。因为 create_all 不会给既有 camp_unit
补列，部署本版本前必须先执行结构迁移；本轮未执行。两份迁移尚未加入批量上线脚本。

## 阶段 2 营期与项目适配

CampUnit 保留原 ID、营期、状态、可见范围及原业务外键。绑定后 name/owner_user_id 通过
组织核心投影；旧物理列只作历史值，不再双写。一个核心组最多绑定一个 CampUnit，避免共用
可变成员名单。当前未开放项目改名，保留既有营期内项目名称唯一约束。

ProjectGovernancePolicy 核验真实单元、项目营、绑定、有效入营和参与资格。项目管理沿用
既有 super_admin 范围，没有扩权给 CampStaff；普通负责人只在 selecting 且单元 active 时
增减普通成员，不能任命新组长。营期 archived/deleted、项目 terminated 拒绝组织写入。

| 项目流程 | 阶段 2 行为 |
| --- | --- |
| 申报审核通过 | 自动入池及参与锚点、创建核心组、绑定 CampUnit、授予 project_leader，同事务保留原项目档案与模板实例化 |
| 名单勾选 | 账号/组/营期持锁后核对名单版本；数据库幂等结果按操作者、单元、键隔离；保留逐项结果回包 |
| 批量回填与移除 | 使用核心成员服务；错误按既有逐项/单项响应处理，禁止继续写旧成员表 |
| 负责人转交 | 新负责人入池、治理负责人及项目职责一起转交；正常旧负责人转成员，已失去参与资格者结束授权；允许负责多个项目 |
| 暂停/恢复/终止 | 单元状态保留；终止归档核心并结束全员职责，旧成员事实不删除 |
| 退营 | 先结束该营已绑定组的参与；仍负责项目者须先转交；营期老师不能借退营绕过项目管理员变更门禁 |
| 交付/组会/考勤/提醒/导出/档案 | 统一从 UnitMembershipRead 读取新核心或未映射旧记录，业务对象继续引用原 unit_id |

UnitMembershipRead 是 SQLAlchemy 只读联合查询，不是第二张实体名单表：有 group_id 时
只选核心，无 group_id 时只选旧表。历史人员的角色缺少新任命记录时从显式映射回读旧角色，
权限判断另外走 unit_role/effective_roles。模型事件阻止通过该投影插入、更新或删除。
CampMembershipEvent 保留业务事件和旧 roster.version 契约，GroupMembershipEvent 记录组织动作。

## 旧成员映射与预览

`preview_project(unit_id)` 返回来源人员、原角色、状态、原时间、实名锚点、旧新 ID 和摘要。
`map_project(unit_id, actor_user_id, expected_digest)` 是显式后台服务：要求有效管理员，
持锁后重新预览，冲突或摘要变化即拒绝；不自行提交事务。已映射项目重复调用不会覆盖之后的业务变更。

LegacyUnitMembershipMap 固定对应旧 camp_unit_member.id 与新 group_membership.id，
原单元、旧成员、旧事件及 UnitPersonParticipation 锚点保留；新关系采用当前账号关联的
Person 事实，缺失者不伪造 Person。迁移保留原 started_at/ended_at；旧表没有职责任命时间，
因此活动职责以本次承接时间记录，原角色与来源在迁移事件中保留，不伪造历史任命人。

重复 Person、无负责人参与、入营关系缺失、原时间缺失、锚点不一致会进入冲突报告。
未映射旧项目的组织关系只读；修改成员、负责人或状态时返回 PROJECT_GROUP_MAPPING_REQUIRED。
项目交付及其他业务仍沿用既有规则，不把“组织关系只读”误作所有项目端点均禁写。
不自动迁移，不保留另一套旧成员写入路径。

阶段 2 的简化培训预览已在阶段 5 删除。培训统一使用 `training_migration.preview(camp_id)`，
包含历史教学事实、人员锚点、课程裁定和完整摘要；CLI 入口保持不变。

只读命令（执行前确认配置连接的是预期环境）：

```bash
.venv/bin/python scripts/preview_camp_groups.py --project-unit 123
.venv/bin/python scripts/preview_camp_groups.py --training-camp 456
```

此预览命令没有 apply 参数。阶段 4 已另增显式映射与切换恢复工具，见本文末尾；
真实数据迁移尚未执行。跨营学习继承不属于此核心，尚未启用。

## 本阶段检查

阶段 1 的 6 项核心定向检查及阶段 2 的 6 项适配定向检查在 SQLite 内存库通过。
阶段 2 检查覆盖项目生命周期及单一成员来源、名单幂等与只读门禁、旧新映射与原时间保留、
迁移冲突及培训预览、退营负责人保护，以及人员外键级联与旧锚点冲突拦截。
另做修改文件的语法检查、MySQL 查询/DDL 离线编译和 diff 空白检查。

本轮没有启动 app、连接业务数据库、执行结构/生产数据迁移或运行前端构建/E2E、全量回归。
实际 MySQL 锁竞争、生产数据质量、真实附件及前端整链路尚未验证；复杂验证需用户另行授权。

## 阶段 3 培训接入 2026-10-11

培训应用服务在 `services/camp_groups/training.py`，职责与兼容范围读取在
`training_access.py`。`TrainingGovernancePolicy` 仅装配到培训服务，目标须匹配
本营 `mentor_team` 绑定。老师按现有本营成员管理权限管理组；普通导生的招生写权限
只在服务校验窗口、容量、单一招生位置与来源后，按本次操作者／组／学员限定范围授予。
通用治理负责人不自动取得课程认证权。

`TrainingGroupProfile` 持有方向、课程及容量；招生主导生映射每营唯一。
CampMember 的学习资格、考勤义务与组内职责分开，空值保留旧 student 语义。
`recruitment_unit_id` 是单轮招生位置，`team_mentor_id` 是它的兼容投影，
名单编组不另写一套团队归属。CampMentorMatch 保留招生来源，所有招生写入同事务完成。

`CampCourseAssignment.unit_id` 是同营同人同课唯一的当前责任组。默认拒绝第二组同课，
老师通过明确转交或添加学习职责时确认 `transfer_courses=true` 处理；保留原分配 ID、
营内快照、认证者、分数、时间和材料。结束组学习职责、移除教学配置只释放对应责任，
不自然退课。`CampTeachingEvent` 记录课程责任、招生、考勤义务和认证变更。

| HTTP 入口 | 权限及用途 |
| --- | --- |
| GET /camp/sessions/:sid/training-groups | 本营管理人员或参与者；参与者仅看所属组 |
| POST /camp/sessions/:sid/training-groups | 本营 member.manage；创建培训组和首位导生 |
| PUT /camp/sessions/:sid/training-groups/:unit | member.manage + expected_version；调整名称、课程配置、容量、暂停/恢复与治理负责人 |
| POST/DELETE /camp/sessions/:sid/training-groups/:unit/members | member.manage + expected_version；增减 learner/mentor，可指定 course_id；招生学员仍走原指派/释放入口 |
| POST /camp/sessions/:sid/course-assign/transfer | member.manage；user_id/course_id/unit_id/from_unit_id，显式转交现有责任 |
| 既有 team/progress、team-meetings、team/task-summary | 多组请求传 unit_id；不默认选择任意组，单组继续自动解析 |
| 既有 my-direction | 返回 groups 和单一修读清单，可按 unit_id 筛选；待分配课程仍可由本人访问 |

请假依照所属学习组的有效导生范围授权；一张申请用行锁保证一次决定，禁止自批。
老师继续原有兜底权限。营期考勤按人汇总，新增组关系不生成或复制承诺日。
组会持有明确的培训 unit_id，附件和提醒按该组读取；章节催办额外检查课程责任未转走。
课程学习的无效显式营期上下文不会回落到其他营或全局写入。跨营继承不在本阶段启用。

迁移 74 默认离线输出 DDL，显式 --apply 也只添加结构。部署前需完成 72、73、74；
本次均未执行。新营自动采用统一培训组路径；旧营的 training_groups_ready 默认 false，
继续兼容读取和招生写入，后续完成显式旧名单映射后才切换，不能手动翻标记跳过映射。
这是数据迁移状态，不是面向用户的第二种营期结构。

定向检查入口 `.venv/bin/python scripts/test_training_groups.py`，10 个 SQLite 内存场景，
包括真实 Flask 路由；认证服务和 JWT 路由执行，账号会话解析使用隔离 fixture。
未启动应用入口、调度器或 Redis，未连接业务数据库。真实 MySQL 并发、完整前端交互、
旧培训数据迁移、档案与恢复演练仍须在阶段 4 完成；这些定向检查不代表全量验收。

## 阶段 4：旧培训名单映射、切换与恢复（2026-10-11）

已完成后台工具、历史读取、任务提醒及维护栅栏收口，没有执行结构迁移或生产名单迁移。
真实 MySQL 并发与端到端运营验收仍未完成。迁移 75 添加 `CampSession.training_migration_paused`、
`TrainingMigrationRun` 及 `CampMaintenanceFence`；默认仅输出离线 DDL。
发布本版本前须完成 72 → 73 → 74 → 75 的结构准备，`create_all()` 不能补旧表列。

### 操作入口与状态

`services/camp_groups/training_migration.py` 提供不自行提交的服务；
`scripts/training_group_cutover.py` 是后台操作入口，不导入 app、不启动调度器或发送通知。
默认 action 为 preview，所有写操作必须同时提供 `--apply --actor <有效管理员ID>`。
`--run` 显式定位批次，旧批次不能释放新批次的暂停状态。每营最多一个未结束批次。
此入口供维护人员使用，没有给学员或老师增加迁移开关。

| action | 行为与条件 |
| --- | --- |
| preview | 只读：输出拟建组、成员职责、课程与组会绑定、冲突、告警及逐表数量/摘要；支持 `--decisions` |
| freeze | 暂停该营并建立批次；重复执行沿用原批次；已切换营再次暂停沿用其切换批次 |
| map | 要求暂停、有效批次、无冲突和 `--expected-digest`；同一预览与裁定重试不重复建组 |
| verify | 比较原教学事实、映射后完整状态摘要及当前身份资格，给出能否安全恢复 |
| switch | 仅 mapped + 暂停 + 校验通过时启用统一路径；切换后仍保持暂停 |
| resume | switched 后开放业务；frozen 时取消未执行的批次；mapped 不允许半途开放 |
| restore | 暂停后且没有任何可检测的映射后业务变化时恢复旧绑定；保留迁移组和事件档案，仍保持暂停 |
| export | 输出原快照、映射、裁定、操作日志、当前营期及核心组/职责/事件数据，供核对或后续恢复方案使用 |

状态流程为 `frozen → mapped → switched`；frozen 可以取消，mapped/switched 经校验可以
转为 restored。恢复后可建立新批次重新预览；旧批次和其原快照仍保留。
`resume` 与业务结构分开：它只结束维护暂停，不替代切换或恢复。

示例命令仅作为日后运维说明，本轮未执行。先完成结构准备、部署所有带栅栏的应用及调度
进程，并停止或排空仍运行旧版本的 worker、独立脚本和外部写入，再按批准的营期窗口操作。
不要把不同版本的 worker 混跑后依赖新标记保护旧代码。

```bash
# 只读预览；这里的 SID / ADMIN / RUN / DIGEST 都须替换为实际核对值。
.venv/bin/python scripts/training_group_cutover.py --camp SID --output preview.json
# 暂停后重新预览，使用这一份的 digest；export 会导出当前未映射的原始事实。
.venv/bin/python scripts/training_group_cutover.py --camp SID --action freeze --actor ADMIN --apply
.venv/bin/python scripts/training_group_cutover.py --camp SID --action preview --output frozen-preview.json
.venv/bin/python scripts/training_group_cutover.py --camp SID --action export --run RUN --output before.json
.venv/bin/python scripts/training_group_cutover.py --camp SID --action map --run RUN --actor ADMIN --expected-digest DIGEST --apply
.venv/bin/python scripts/training_group_cutover.py --camp SID --action verify --run RUN
.venv/bin/python scripts/training_group_cutover.py --camp SID --action export --run RUN --output mapped.json
.venv/bin/python scripts/training_group_cutover.py --camp SID --action switch --run RUN --actor ADMIN --apply
.venv/bin/python scripts/training_group_cutover.py --camp SID --action resume --run RUN --actor ADMIN --apply
```

`--output` 仅用于只读操作，以 0600 创建新文件并拒绝覆盖；写命令不接受输出文件选项，
避免业务已经提交后才发现文件不能创建。导出包含人员和教学记录，应存放于受控目录。
JSON 快照用于核对与映射恢复，不能替代数据库与附件存储的完整备份。

### 映射与冲突处理

- 每位现役旧导生建立唯一招生组，现役学员按原 `team_mentor_id` 承接；不复制 CampMember，
  不改原 role、学习资格、考勤义务、加入/退出时间、志愿排序或配对来源。
- 当前成员与人员锚点、同人多账号、账号生命周期、原配对账本冲突会阻止切换；沿用现有
  shadow/enforce 和核验宽限。shadow 中缺 Person、非主号等情况出告警，不伪造身份。
- 原名片方向、课程和容量承接到配置；多义方向或失效招生目标出冲突。旧超额名单保留并
  告警，不删除学员来“修正”容量。恢复前先修正源数据的情形，取消 frozen 批次后按原流程处理。
- 课程只补当前责任组，不重新入课、不覆盖分配 ID/来源/时间、课时、认证分数（包括 0 分）、
  认证者、材料或附件。不明确的归属必须裁定；已结束分配保持历史，不自动复活。
- 组会只补 unit_id，保留 mentor_id、会议及任务/提交 ID、原更新时间和附件 object_key。
  已离营导生的旧组会绑定归档组，该组不授予现役权限，也不占用新的招生位置。
- 原始成员时间来自快照；新核心关系和任命从本次承接开始，迁移事件记录来源，不把操作人
  伪造成历史任命人。已经离营的成员事实保留在原表及快照，不推测其历史换组过程。

课程裁定文件示例（map 和 preview 必须传相同文件）：

```json
{
  "assignment_targets": {
    "123": {"mentor_id": null, "reason": "旧导生已释放，保留待分配修读"},
    "124": {"mentor_id": 45, "reason": "核对确认由当前归属导生继续负责该历史课程"}
  }
}
```

目标必须是该学员当前旧学习组的导生，不能借迁移工具任意转组。明确选择当前导生时，
可以将原已分配但方向配置缺失的课程纳入该组；不会给其他学员自动增课。
`null` 保留修读但暂不授予任何组课程责任。所有裁定必须有理由并进入批次档案。
未知账号、人员冲突等不通过此文件绕过，须在原业务/身份治理流程中解决。

### 维护栅栏与恢复限制

`maintenance.py` 在 `camp_maintenance_fence` 上按营持有行锁：HTTP 使用共享锁，切换写命令
使用独占锁；普通请求之间不因栅栏互斥。锁行不存在时仅按真实营期补建技术锁行，然后提交该
初始化事务，再进入持锁阶段；锁表不参与教学快照比较。独立 NullPool 连接贯穿请求及其中的
业务提交，不占用业务连接池，也不锁住业务 CampSession 行。取得栅栏后重新读取维护状态并
结束业务侧先前的干净读事务，避免沿用切换前快照；若发现未提交写入则拒绝该请求。
写命令同时持有该营独占锁和调度器命名锁，取不到锁即返回繁忙，不越过正在处理的请求或扫描。
preview/verify/export 只持调度锁，读取快照而不创建营期锁行，不写业务数据；预览可能随随后
业务操作过期，map 仍必须核对完整摘要。四类营期调度扫描使用同一调度锁，并跳过暂停营期，
不占用其 Redis 提醒游标，也不清空或重放既有游标。外部直接 SQL 和旧版进程不受此栅栏保护。

维护中的营期 HTTP 请求返回 423，包括可能含自动收官等副作用的 GET；不是正常可读状态。
资源详情会解析所属营；不能明确定位营期的个人聚合、学习入口保守覆盖所有培训营，
因此维护窗口内部分聚合入口也可能暂不可用。多个 scope 使用同一专用连接持锁。
SQLite 只用进程内锁供定向检查，不能代表生产跨进程或 MySQL 锁竞争已验收。
HTTP 遇到切换独占锁最多等待 10 秒，超时返回 503；切换命令不等待正在持有的共享行锁。
SQL 定向检查确认共享/独占语句生成，但真实 MySQL 锁竞争、异常释放和吞吐仍须在上线验收中
检查，不能把 SQLite 单进程检查当成生产并发验证。

安全恢复流程：再次 freeze → verify / export → restore → 核对 → resume。
恢复仅撤回本批次添加的招生、课程、组会绑定，归档迁移组并结束迁移职责；不物理删除组或
审计事件，不撤销成绩、考勤或通知。没有业务变化时支持幂等恢复。
若有新编组、课程/成绩/附件/任务等变化，返回 `MIGRATION_FORWARD_ONLY`，保持当前数据及
维护暂停，先 export 保存现场；必须修复前进或制定另行批准的数据恢复方案。
工具没有 `--force`，也不会仅关闭 training_groups_ready 来丢弃新业务。

本轮完成 10 项内存定向场景（含请求维护门禁、调度跳过、失效预览、裁定、原子失败、
幂等映射、切换前/后恢复限制、旧批次保护及归档历史），并复查第三部分 10 项直接相关场景。
这些检查不代表完整上线验收。下列运营收口另有 10 项定向检查；真实 MySQL 并发及端到端验收仍待后续。


### 阶段 4 运营收口（2026-10-11 续）

- `training_history` 只扩展本营工作人员／管理员的历史读取，不参与教学授权。统一小组学习看板与考勤
  看板/导出接受 `include_history=1`；归档营自动包含。学习历史含结束分配、退营人员，考勤
  纳入已存在承诺日的用户并按人去重。周统计可从历史营期人员读取，不依赖存在承诺日。旧营未切换前，学习看板继续原兼容读取，
  不显示新的历史筛选；切换后即可通过统一读模型查看保留的旧学习记录。
- 小组管理返回 `membership_status`、`camp_status`、职责 `active` 和起止时间；普通成员仍只看
  当前角色。组会保留有实际提交的离组人员；历史导生只作为管理筛选选项，不恢复带教权。
- 本营老师可读取章节材料和附件，不因此取得删除权限。附件短签实际下载时重新核对本营
  管理资格或当前课程带教关系；组会 ZIP 的章节材料同样受此限制。老师组会视角仍是统计汇总。
- 组会任务/章节增加 `expected_count`、`required_for_me`；带教矩阵增加 `expected_user_ids`，
  章节另有 `visible_user_ids`。任务义务要求当前有效学习职责在布置时已开始；实际历史提交保留。
  课内义务还需当前同营课程责任指向本组。任务汇总每人返回自己的 `task_total`，不能再以
  “所有任务 × 当前人数”估算。详情、列表、任务汇总及两类截止提醒遵守相同规则。
- 旧映射 `legacy_mapping` 事件记录中的原学员以旧营加入时间承接任务；不重造缺失的旧换组
  时间。后续新职责不会套用此例外。编辑布置使用章节差量保留，保留首次布置时间。
- 课程完成态只依据本营认证，不继承其他营的全局完成态。此变更不实现跨营学习进度继承。

定向运行 `PYTHONWARNINGS=ignore .venv/bin/python scripts/test_training_operations.py`：10 项，
SQLite 内存、模拟通知/存储，无 app 启动、Redis 游标写入或真实存储读取。覆盖历史报表、
0 分认证、考勤 CSV、材料短签失效、新入组/退出提醒、历史任务提交、首次布置时间、旧营
映射承接及维护 SQL/HTTP 接入；同时检查受影响组件编译。未做完整 build/E2E 或全量回归。


## 阶段 5：兼容代码收口（2026-10-11）

已切换营的 `team_mentor_id` 为招生绑定读投影；旧物理列映射为 `legacy_team_mentor_id`，只留
历史或供未切换营写入，不做新旧双写。Python 属性、SQL 筛选及 alias/join 使用同一读取规则。
旧营仍走原招生契约，已有 API 响应字段保持兼容。选导生及日常改派由 `recruitment.assign`
统一分流，避免蓝图提前改账本再调用服务。模型 setter/flush 拦截已切换营的旧列 ORM 误写。

项目投影只包含 project 单元；业务蓝图显式使用 `UnitMembershipRead`，映射后旧实体 ORM
增删改拒绝。教学候选范围与账号读取集中，身份授权仍实时判定。阶段 2 简化培训预览删除，
切换前统一使用阶段 4 的完整预览。没有新迁移，不改物理列或真实数据。

前两代小组接口尚有管理端消费者，故不删除或关闭。旧 API 明确携带营期上下文时返回
`LEGACY_GROUP_CONTEXT_UNSUPPORTED`；其他历史调用保留原语义。详见
[旧小组兼容与退役清单](legacy-group-retirement.md)，含离线调用盘点和生产存量待办。

8 项新增定向场景及受影响阶段 2–4 检查合计 44 项通过。仅 SQLite 内存/Python 编译，
不等于生产上线验收；物理退役、真实并发和旧客户端退出仍需后续确认。

## 上线准备与数据库回归（2026-10-11）

用户已授权视觉收尾与全量回归。此前的“仅定向检查、真实 MySQL 待验证”是阶段交付记录，
本轮补跑全部 18 个常规后端测试脚本（377 项通过），并新增
`scripts/test_camp_groups_mysql.py`：仅限本机指定前缀的空测试库，实际迁移 72–76 两遍，
在 MySQL 9.6.0 执行 50 项组核心/适配/教学/切换/运营场景及 3 项并发场景，全部通过。
并发覆盖最后一个名额、同营同课唯一责任组、普通请求共享及维护独占互斥；不代表吞吐压测。

真实数据库回归发现并修复：核心时间字段使用 DATETIME(6)，任务布置时间通过迁移 76
保持同等精度；项目映射预览刷新数据库持久化时间，避免秒级舍入产生虚假的摘要失效；
MySQL 共享非阻塞锁显式使用 `FOR SHARE NOWAIT`。已有历史时间和任务义务不重算。

前端两端培训组界面使用 DewUI，现有大一未切换营保留导生提示和原流程。旧 API/物理列
仍按退役清单保留，跨营进度继承仍未启用。此次没有触碰生产数据库，也没有执行生产名单切换。
部署与回退流程见 [营期小组上线指南](deploy/营期小组上线指南-202610.md)。

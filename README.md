# BME卓越工程师在线教育平台后端项目

## 项目简介

这个项目是一个服务于训练营学生和老师，实现资源共享，进度管理，讨论交流，个人生涯的在线教学平台 同时也是软件二组网页开发组学习网页开发的试点项目。该项目是在线教学平台的后端项目。

## 特性

* 后端主要使用Flask框架，数据库为MySQL，使用gunicorn作为WAGI服务器，使用redis作为缓存
* 后端使用了蓝图特性（blueprint）将api分类管理，更易读懂项目
* 后端使用了Swagger制作api文档，方便前端开发
* 前端使用了Vue+Vite框架和Javascript语言
* 形成训练营自己的知识库和交流平台

## 权限系统

本项目实现了基于模块的细粒度权限控制系统，替代了原有的简单角色权限模型。

### 权限模块

系统包含以下权限模块：
- `course_management`: 课程管理权限
- `user_management`: 用户管理权限
- `article_management`: 文章管理权限
- `medal_management`: 勋章管理权限
- `system_management`: 系统管理权限

### 权限API

提供以下权限管理接口：
- `/permissions/init`: 初始化权限系统
- `/permissions/list`: 列出所有权限
- `/permissions/assign`: 为用户分配权限
- `/permissions/revoke`: 撤销用户权限
- `/permissions/user/<user_id>`: 获取用户权限列表

## 开发测试账号

本地开发库（dev MySQL）固定测试账号，密码统一 `12345678`（登录走 md5 协议，
脚本改密码须 `set_password(md5(明文))`）：

| 账号 | 角色 | 用途 |
| --- | --- | --- |
| `admin@seed.dev` | super_admin | 管理端超管 |
| `teacher@seed.dev` | super_admin | 管理端老师视角 |
| `stu1@seed.dev` ~ `stu4@seed.dev` | user | 用户端学员（stu4 兼营期预览） |

注意：`python seed.py` 中的正式超管（admin@bme.sysu.edu.cn 等）自 09-17 安全加固后
密码为随机生成、seed 时打印一次，与上述开发测试账号无关。前端两端在 dev server 下
有独立「测试账号面板」页（/dev/accounts，登录页入口进入）：账号按角色分组卡片化，
点击一键登录；数据来自 `GET /auth/dev_accounts`（debug 模式自动可用，非 debug 需
`.env` 设 `DEV_TEST_ACCOUNTS=on`，生产一律 404）。

## 教学与项目共用小组核心

项目营交付、单人项目验收、附件限制与结营事务的当前约定见 [项目营交付与归档](docs/project-delivery.md)。

已实现通用核心及阶段 2 项目适配，服务契约见 [group-core.md](docs/group-core.md)。
新项目审核绑定核心；旧项目组织关系保留只读，成员变更前须完成映射。迁移 72/73 默认仅输出 DDL，
显式 `--apply` 才修改结构，均不迁移成员；部署这些代码前必须完成结构准备。

## 安装

1. 安装MySQL8.0.39
2. 安装redis
3. 安装python3.9

   ```
   sudo apt install python3.9
   ```
4. 克隆项目到本地

   ```
   git clone https://github.com/Jerry-Scintilla/BME_platform_flask.git
   ```
5. 进入到项目目录,创建虚拟环境

   ```
   cd /BME_platform_flask
   mkdir flask_nenv
   virtualenv -p /usr/bin/python3.9 flask_nenv
   ```
6. 激活虚拟环境，安装依赖文件

   ```
   source flask_nenv/bin/activate
   pip install -r requirements.txt
   ```
7. 前往config.py配置mysql以及邮箱账户
8. 前往gunicorn.conf配置WSGI服务器以及启动文件，配置完成启动后端

   ```
   gunicorn -c gunicorn.conf app:app
   ```


# 开源协议 / Open Source License

本项目代码仅供查看，**禁止修改、商用、二次分发**。
适用许可证：**CC BY-NC-ND 4.0**
详情查看 [LICENSE](LICENSE) 文件或访问：[CC BY-NC-ND 4.0](https://creativecommons.org/licenses/by-nc-nd/4.0/)

This project's code is for viewing only. **Modification, commercial use, and redistribution are prohibited.**
License: **CC BY-NC-ND 4.0**
For details, view the [LICENSE](LICENSE) file or visit: [CC BY-NC-ND 4.0](https://creativecommons.org/licenses/by-nc-nd/4.0/).

培训组阶段 3 已实现，接口与迁移边界见 [小组核心契约](docs/group-core.md#阶段-3-培训接入-2026-10-11)。发布前需完成结构迁移 72/73/74/75；本轮未执行数据库迁移。

旧营阶段 4 已补齐名单预览、显式映射、维护暂停、切换及受保护恢复工具。入口为 `scripts/training_group_cutover.py`（默认只读）；详见 [迁移操作与限制](docs/group-core.md#阶段-4旧培训名单映射切换与恢复2026-10-11)。真实生产切换和完整上线验收尚未执行。

阶段 4 运营收口已补充历史名单看板/考勤导出、历史材料只读权限、组会任务提醒口径和共享维护栅栏；定向检查见 `scripts/test_training_operations.py`。生产迁移及真实并发验收仍未执行。

阶段 5 已完成兼容代码收口：新营招生归属改为绑定读投影，旧营保留兼容写入；项目旧实体只读保护及旧 API 营期上下文边界已加入。见 [旧小组退役清单](docs/legacy-group-retirement.md)。没有新增结构迁移或关闭历史蓝图。

### 章节认证时效留痕

认证截止仅用于提醒和时效判定，过期仍可认证。组会按每次章节布置展示待提交、待认证、
逾期未提交、逾期待认证、按时认证及逾期补认证；无截止显示已认证，无可靠快照的存量记录
显示历史已认证。当前逾期未完成数量与逾期补认证数量分别统计，完成后不再把截止时间标红。

`camp_chapter_certification.meeting_timings` 按章节布置 ID 保存认证时的截止、认证人、
认证时间及当时留存材料的首次提交时间。材料时间仅代表留存材料，不代表首次上传且已删除的
材料，也不代表导生验收通过时间。改分或修改截止不覆盖已有结果；撤销的完整快照保存在
`camp_teaching_event`，重新认证产生新结果。新增布置可基于当时已有认证生成该次布置的快照，
不会追溯重算其他旧布置。布置与截止变更记录前后值、操作人和操作时间，导生/老师详情可查看
最近 50 条记录。认证完成态仍仅由原有章节认证记录决定。

部署新后端前需执行 `python scripts/migrate/migrate_77_chapter_certification_timing.py --apply`。
省略 `--apply` 只输出 DDL；迁移可重复执行，不回填历史数据。迁移先于应用更新，旧应用忽略新增列。
本次只提交代码及迁移脚本，未执行业务数据库迁移或部署。

轻量验证：`PYTHONPATH=.:scripts python -m unittest test_chapter_certification_timing`。

### 积分联动

个人余额、训练营奖励规则、发放审核与记录见 [积分联动上线说明](docs/deploy/积分联动-上线与接口.md)。
部署时先执行迁移 78，再显式开启 `POINTS_REWARDS_ENABLED`；默认不创建奖励规则或发放积分。
商城全量消费流水接口尚未向训练营开放，目前通过商城入口查看。

### 项目营工作区

项目营侧栏工作区、老师只读看板、节点通知定位及本地专项检查见 [工作区与灰测交付](docs/project-workspace.md)。本轮不新增结构迁移，配套前端与后端交付分支均为 `feature/camp-group-core`；部署前核对既有迁移77、78及生产配置。

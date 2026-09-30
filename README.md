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
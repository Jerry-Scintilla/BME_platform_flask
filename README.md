# BME卓越工程师在线教育平台后端项目

## 项目简介

这个项目是一个服务于训练营学生和老师，实现资源共享，进度管理，讨论交流，个人生涯的在线教学平台 同时也是软件二组网页开发组学习网页开发的试点项目。该项目是在线教学平台的后端项目。

## 特性

* 后端主要使用Flask框架，数据库为MySQL，使用gunicorn作为WAGI服务器
* 后端使用了蓝图特性（blueprint）将api分类管理，更易读懂项目
* 后端使用了Swagger制作api文档，方便前端开发
* 前端使用了Vue+Vite框架和Javascript语言
* 形成训练营自己的知识库和交流平台

## 安装

1. 安装MySQL8.0.39
2. 安装python3.9

   ```
   sudo apt install python3.9
   ```
3. 克隆项目到本地

   ```
   git clone https://github.com/Jerry-Scintilla/BME_platform_flask.git
   ```
4. 进入到项目目录,创建虚拟环境

   ```
   cd /BME_platform_flask
   mkdir flask_nenv
   virtualenv -p /usr/bin/python3.9 flask_nenv
   ```
5. 激活虚拟环境，安装依赖文件

   ```
   source flask_nenv/bin/activate
   pip install -r requirements.txt
   ```
6. 前往config.py配置mysql以及邮箱账户
7. 前往gunicorn.conf配置WSGI服务器以及启动文件，配置完成启动后端

   ```
   gunicorn -c gunicorn.conf app:app
   ```

```

```

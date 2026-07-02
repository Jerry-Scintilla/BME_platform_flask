"""
Seed 脚本 — 初始化开发数据

用法:
    flask db upgrade   # 先建表（或用 db.create_all）
    python seed.py     # 再填充数据
"""
import hashlib
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from app import app
from exts import db
from models import (
    UserModel, CourseModel, Chapter, LessonModel,
    UserCourseModel, MedalModel, MedalUserModel,
    PermissionModel, HomeCover,
    NotificationModel,
    UserPermissionModel,
)


def md5(s: str) -> str:
    """前端用 md5 哈希后传给后端，后端明文存储，所以这里存 md5 结果"""
    return hashlib.md5(s.encode()).hexdigest()


def seed():
    with app.app_context():
        # 确保所有表存在
        db.create_all()
        print("✅ 数据表已就绪\n")

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # 1. 用户
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        users_data = [
            {
                "username": "管理员",
                "email": "admin@bme.sysu.edu.cn",
                "password": md5("admin123"),
                "user_mode": "admin",
                "sex": "男",
                "institute": "生物医学工程学院",
                "major": "生物医学工程",
                "introduction": "系统管理员",
            },
            {
                "username": "张三",
                "email": "zhangsan@bme.sysu.edu.cn",
                "password": md5("123456"),
                "user_mode": "user",
                "sex": "男",
                "institute": "生物医学工程学院",
                "major": "生物医学工程",
                "student_id": 2024001,
                "introduction": "测试学生账号",
            },
            {
                "username": "李老师",
                "email": "liteacher@bme.sysu.edu.cn",
                "password": md5("123456"),
                "user_mode": "admin",
                "sex": "女",
                "institute": "生物医学工程学院",
                "major": "生物医学工程",
                "introduction": "测试教师账号",
            },
            {
                "username": "王五",
                "email": "wangwu@bme.sysu.edu.cn",
                "password": md5("123456"),
                "user_mode": "user",
                "sex": "男",
                "institute": "生物医学工程学院",
                "major": "生物医学工程",
                "student_id": 2024002,
                "introduction": "测试学生账号2",
            },
        ]

        created_users = []
        for u in users_data:
            existing = UserModel.query.filter_by(email=u["email"]).first()
            if existing:
                print(f"  ⏭  用户 {u['username']} 已存在，跳过")
                created_users.append(existing)
                continue
            user = UserModel(
                username=u["username"],
                email=u["email"],
                password=u["password"],
                user_mode=u["user_mode"],
                sex=u.get("sex"),
                institute=u.get("institute"),
                major=u.get("major"),
                student_id=u.get("student_id"),
                introduction=u.get("introduction"),
                study_stage="未分流",
            )
            db.session.add(user)
            created_users.append(user)
            print(f"  ✅ 创建用户: {u['username']} ({u['email']})")

        db.session.flush()
        admin_user, student1, teacher, student2 = created_users

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # 2. 权限
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        permissions_data = [
            {"name": "course.manage", "description": "课程管理"},
            {"name": "user.manage", "description": "用户管理"},
            {"name": "article.manage", "description": "文章管理"},
            {"name": "group.manage", "description": "小组管理"},
            {"name": "medal.manage", "description": "勋章管理"},
            {"name": "task.manage", "description": "任务管理"},
            {"name": "discussion.manage", "description": "讨论区管理"},
            {"name": "checkin.manage", "description": "签到管理"},
            {"name": "attendance_report.recipient", "description": "接收每日出勤汇总邮件"},
        ]
        perm_count = 0
        for p in permissions_data:
            if PermissionModel.query.filter_by(name=p["name"]).first():
                continue
            db.session.add(PermissionModel(name=p["name"], description=p["description"]))
            perm_count += 1
        if perm_count:
            print(f"  ✅ 创建 {perm_count} 条权限")

        # 给管理员和教师分配所有权限
        all_perms = PermissionModel.query.all()
        for user in [admin_user, teacher]:
            if UserPermissionModel.query.filter_by(user_id=user.id).first():
                continue
            for p in all_perms:
                db.session.add(UserPermissionModel(user_id=user.id, permission_id=p.id))
            print(f"  ✅ 给 {user.username} 分配了 {len(all_perms)} 条权限")

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # 3. 课程
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        courses_data = [
            {
                "title": "生物医学工程导论",
                "introduction": "本课程介绍生物医学工程的基本概念、发展历史和前沿技术，涵盖生物材料、医学影像、生物传感器等核心领域。",
                "difficulty": 1,
                "class_hour": 32,
                "tags": "导论,生物医学",
                "creator_id": teacher.id,
            },
            {
                "title": "医学影像原理与技术",
                "introduction": "系统讲解X射线、CT、MRI、超声等医学影像技术的物理原理、设备构成和临床应用。",
                "difficulty": 2,
                "class_hour": 48,
                "tags": "医学影像,CT,MRI",
                "creator_id": teacher.id,
            },
            {
                "title": "生物材料学",
                "introduction": "学习生物材料的基本性质、表征方法及其在组织工程和药物递送中的应用。",
                "difficulty": 3,
                "class_hour": 36,
                "tags": "生物材料,组织工程",
                "creator_id": teacher.id,
            },
        ]

        created_courses = []
        for c in courses_data:
            existing = CourseModel.query.filter_by(title=c["title"]).first()
            if existing:
                print(f"  ⏭  课程「{c['title']}」已存在，跳过")
                created_courses.append(existing)
                continue
            course = CourseModel(
                title=c["title"],
                introduction=c["introduction"],
                difficulty=c["difficulty"],
                class_hour=c["class_hour"],
                tags=c["tags"],
                creator_id=c["creator_id"],
            )
            db.session.add(course)
            created_courses.append(course)
            print(f"  ✅ 创建课程: {c['title']}")

        db.session.flush()

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # 4. 章节 + 课时（只给第一门课）
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        if not Chapter.query.filter_by(course_id=created_courses[0].id).first():
            chapters_data = [
                {"name": "第一章：概述", "level": 1, "order": 1},
                {"name": "1.1 什么是生物医学工程", "level": 2, "order": 2},
                {"name": "1.2 发展历史", "level": 2, "order": 3},
                {"name": "第二章：生物材料", "level": 1, "order": 4},
                {"name": "2.1 生物材料分类", "level": 2, "order": 5},
                {"name": "2.2 生物相容性", "level": 2, "order": 6},
            ]
            created_chapters = []
            for ch in chapters_data:
                chapter = Chapter(
                    course_id=created_courses[0].id,
                    name=ch["name"],
                    level=ch["level"],
                    order=ch["order"],
                )
                db.session.add(chapter)
                created_chapters.append(chapter)
            db.session.flush()
            print(f"  ✅ 创建 {len(chapters_data)} 个章节")

            lessons_data = [
                {"chapter_idx": 1, "title": "BME简介", "type": "text",
                 "content": "生物医学工程（Biomedical Engineering）是一门结合工程学原理和医学知识的交叉学科..."},
                {"chapter_idx": 2, "title": "发展历史概述", "type": "text",
                 "content": "生物医学工程的发展可以追溯到20世纪初..."},
                {"chapter_idx": 4, "title": "材料分类", "type": "text",
                 "content": "生物材料主要分为金属、陶瓷、聚合物和复合材料四大类..."},
            ]
            for idx, l in enumerate(lessons_data):
                lesson = LessonModel(
                    chapter_id=created_chapters[l["chapter_idx"]].id,
                    course_id=created_courses[0].id,
                    title=l["title"],
                    type=l["type"],
                    content=l["content"],
                    order=idx + 1,
                )
                db.session.add(lesson)
            print(f"  ✅ 创建 {len(lessons_data)} 个课时")

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # 5. 学生选课
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        enroll_count = 0
        for student in [student1, student2]:
            for course in created_courses:
                if UserCourseModel.query.filter_by(
                    user_id=student.id, course_id=course.id
                ).first():
                    continue
                db.session.add(UserCourseModel(
                    user_id=student.id, course_id=course.id, status="active"
                ))
                enroll_count += 1
        if enroll_count:
            print(f"  ✅ 创建 {enroll_count} 条选课记录")

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # 6. 勋章
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        medals_data = [
            {"medal_name": "初学者", "description": "完成首次登录", "tags": "新手"},
            {"medal_name": "学霸", "description": "完成5门课程", "tags": "学习"},
            {"medal_name": "先驱者", "description": "首批注册用户", "tags": "荣誉"},
            {"medal_name": "签到达人", "description": "连续签到7天", "tags": "签到"},
            {"medal_name": "互助之星", "description": "在讨论区获得10个赞", "tags": "社区"},
        ]
        medal_count = 0
        for m in medals_data:
            if MedalModel.query.filter_by(medal_name=m["medal_name"]).first():
                continue
            db.session.add(MedalModel(
                medal_name=m["medal_name"], description=m["description"], tags=m["tags"]
            ))
            medal_count += 1
        if medal_count:
            print(f"  ✅ 创建 {medal_count} 枚勋章")

        db.session.flush()

        # 给学生1颁发一枚勋章
        medal1 = MedalModel.query.filter_by(medal_name="先驱者").first()
        if medal1 and not MedalUserModel.query.filter_by(
            user_id=student1.id, medal_id=medal1.id
        ).first():
            db.session.add(MedalUserModel(
                user_id=student1.id, medal_id=medal1.id,
                description="首批注册用户奖励"
            ))
            print(f"  ✅ 给 {student1.username} 颁发「先驱者」勋章")

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # 7. 首页封面
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        if not HomeCover.query.first():
            db.session.add(HomeCover(url="/data/covers/default.jpg", cover_id=1))
            db.session.add(HomeCover(url="/data/covers/default.jpg", cover_id=2))
            print(f"  ✅ 创建首页封面")

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # 8. 通知测试数据
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        if NotificationModel.query.first() is None:
            notifications_data = [
                {
                    "user_id": student1.id,
                    "title": "系统维护通知",
                    "content": "系统将于本周六凌晨 2:00–4:00 进行维护升级，期间可能无法正常访问，请提前保存工作。",
                    "category": "system",
                    "source_type": "admin",
                    "is_read": False,
                    "is_important": True,
                },
                {
                    "user_id": student1.id,
                    "title": "新功能上线：学习进度统计",
                    "content": "学习进度统计功能已上线，您可以在个人中心查看详细的学习数据分析报告。",
                    "category": "system",
                    "source_type": "admin",
                    "is_read": True,
                    "is_important": False,
                },
                {
                    "user_id": student2.id,
                    "title": "系统维护通知",
                    "content": "系统将于本周六凌晨 2:00–4:00 进行维护升级，期间可能无法正常访问，请提前保存工作。",
                    "category": "system",
                    "source_type": "admin",
                    "is_read": False,
                    "is_important": True,
                },
                {
                    "user_id": teacher.id,
                    "title": "系统维护通知",
                    "content": "系统将于本周六凌晨 2:00–4:00 进行维护升级，期间可能无法正常访问，请提前保存工作。",
                    "category": "system",
                    "source_type": "admin",
                    "is_read": False,
                    "is_important": True,
                },
            ]
            for nd in notifications_data:
                db.session.add(NotificationModel(**nd))
            print(f"  ✅ 创建 {len(notifications_data)} 条系统通知测试数据")

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # 提交
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        db.session.commit()
        print("\n🎉 Seed 数据填充完成！")
        print("━" * 50)
        print("📋 测试账号：")
        print("   管理员: admin@bme.sysu.edu.cn / admin123")
        print("   教师:   liteacher@bme.sysu.edu.cn / 123456")
        print("   学生1:  zhangsan@bme.sysu.edu.cn / 123456")
        print("   学生2:  wangwu@bme.sysu.edu.cn / 123456")


if __name__ == "__main__":
    seed()

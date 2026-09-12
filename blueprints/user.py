import os
from collections import defaultdict
from datetime import datetime, timedelta
from flask import Blueprint, request, redirect, jsonify
import base64

# 导入拓展
from exts import db, redis_client

# 导入数据库表
from models import UserModel, GroupModel, CourseModel, LearningProgressModel, CheckRecord,MedalUserModel

# 导入表单验证
from .forms import AvatarForm
from .forms import UserInfoForm
from . import get_user_permissions  # RBAC：user_index 返回当前用户角色/权限

# 导入token验证模块
from flask_jwt_extended import (create_access_token, get_jwt_identity, jwt_required, JWTManager)

# 导入api文档模块
from flasgger import swag_from

# 导入权限检查模块
from . import check_permission, audit_log
from .officers import public_officers   # 社团干事任职（身份档案，不挂权限）附进 user_index/profile 回包

bp = Blueprint("user", __name__, url_prefix="/user")

@bp.route("/medal_wear", methods=['POST'])
@jwt_required()
@audit_log(operation="用户佩戴奖牌")
@swag_from('../apidocs/user/medal_wear.yaml')
def medal_wear():
    """用户佩戴已拥有的奖牌

    前端需传入 JSON: { "Medal_Id": <int> }
    """
    try:
        user_email = get_jwt_identity()
        user = UserModel.query.filter_by(email=user_email).first()
        if not user:
            return jsonify({
                "code": 401,
                "message": "用户不存在"
            }), 401

        medal_id = request.json.get('Medal_Id')
        if medal_id is None:
            return jsonify({
                "code": 400,
                "message": "缺少 Medal_Id 参数"
            }), 400

        # 检查用户是否拥有该奖牌记录
        medal_user = MedalUserModel.query.filter_by(user_id=user.id, medal_id=medal_id).first()
        if not medal_user:
            return jsonify({
                "code": 404,
                "message": "该用户未拥有该奖牌"
            }), 404

        # 更新 user 的当前佩戴奖牌字段
        user.medal = medal_id
        db.session.commit()

        return jsonify({
            "code": 200,
            "message": "佩戴奖牌成功",
            "Medal_Id": medal_id
        })

    except Exception as e:
        db.session.rollback()
        return jsonify({
            "code": 500,
            "message": str(e)
        }), 500


# 用户信息请求
@bp.route("/user_index")
@jwt_required()
@swag_from('../apidocs/user/user_index.yaml')
def user_index():
    User_Email = get_jwt_identity()
    # print(User_Email)
    user = UserModel.query.filter_by(email=User_Email).first()

    User_Name = user.username

    User_Medal = user.medal

    User_Stage = user.study_stage


    join_time = user.join_time
    User_Time = join_time.strftime('%Y-%m-%d')

    User_Id = str(user.id).zfill(7)

    Student_Id = user.student_id

    Introduction = user.introduction

    User_Sex = user.sex

    Institute = user.institute

    Major = user.major

    Github_Id = user.github_id

    Skill_Tags = user.skill_tags

    data = {
        "code": 200,
        "message": "获取用户数据成功",
        "User_Email": User_Email,
        "User_Name": User_Name,
        "User_Medal": User_Medal,
        "User_Stage": User_Stage,
        "join_time": User_Time,
        "User_Id": User_Id,
        "Student_Id": Student_Id,
        "Introduction": Introduction,
        "User_Sex": User_Sex,
        "Institute": Institute,
        "Major": Major,
        "Github_Id": Github_Id,
        "Skill_Tags": Skill_Tags,
        "College": user.college,
        "role": user.role,
        "level": user.level,
        "role_rank": user.role_rank,
        "permissions": get_user_permissions(user.id),
        "officers": public_officers(user.id),
    }
    return jsonify(data)


# 查询任意用户的公开资料（按 id）—— 排行榜/社区点击进个人主页用
# 与 user_index 对齐字段，但剔除邮箱/学号等隐私项；附带学习统计
@bp.route("/profile/<int:user_id>")
@jwt_required()
@swag_from('../apidocs/user/user_profile.yaml')
def user_profile(user_id):
    """按 user_id 返回某用户的公开主页资料（不含邮箱/学号）+ 学习统计"""
    user = UserModel.query.get(int(user_id))
    if not user:
        return jsonify({"code": 404, "message": "用户不存在"}), 404

    join_time = user.join_time.strftime('%Y-%m-%d') if user.join_time else None

    # —— 学习统计：复用 records/my_stats 的 CheckRecord 聚合写法 ——
    now = datetime.now()

    # 累计出勤天数（按 date 去重）+ 累计学习时长
    all_records = CheckRecord.query.filter(
        CheckRecord.user_id == user.id,
        CheckRecord.duration != None
    ).all()
    attendance_dates = {r.date for r in all_records if r.date}
    total_days = len(attendance_dates)
    total_hours = round(sum((r.duration or 0) for r in all_records), 2)

    # 本月时长
    first_day = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last_day = (first_day + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    month_records = CheckRecord.query.filter(
        CheckRecord.user_id == user.id,
        CheckRecord.date >= first_day.date(),
        CheckRecord.date <= last_day.date(),
        CheckRecord.duration != None
    ).all()
    month_hours = round(sum((r.duration or 0) for r in month_records), 2)
    # 本月出勤天数（按 date 去重，与累计 total_days 区分）
    month_days = len({r.date for r in month_records if r.date})

    # 本月排名（全用户本月时长排序定位；无记录则为 None）
    all_month_records = CheckRecord.query.filter(
        CheckRecord.date >= first_day.date(),
        CheckRecord.date <= last_day.date(),
        CheckRecord.duration != None
    ).all()
    user_month_hours = defaultdict(float)
    for r in all_month_records:
        user_month_hours[r.user_id] += r.duration or 0
    sorted_users = sorted(user_month_hours.items(), key=lambda x: x[1], reverse=True)
    month_rank = None
    for rank, (uid, _hours) in enumerate(sorted_users, 1):
        if uid == user.id:
            month_rank = rank
            break

    data = {
        "code": 200,
        "message": "获取用户资料成功",
        "User_Name": user.username,
        "User_Medal": user.medal,
        "User_Stage": user.study_stage,
        "join_time": join_time,
        "User_Id": str(user.id).zfill(7),
        "Introduction": user.introduction,
        "User_Sex": user.sex,
        "Institute": user.institute,
        "Major": user.major,
        "Github_Id": user.github_id,
        "Skill_Tags": user.skill_tags,
        "College": user.college,
        "role": user.role,
        "role_rank": user.role_rank,
        "officers": public_officers(user.id),
        "data": {
            "total_days": total_days,
            "total_hours": total_hours,
            "month_days": month_days,
            "month_hours": month_hours,
            "month_rank": month_rank,
        },
    }
    return jsonify(data)


@bp.route("/user_list")
@jwt_required()
@check_permission('user_management')
@swag_from('../apidocs/user/user_list.yaml')
def user_list():
    a_list = UserModel.query.all()
    data = []
    for user in a_list:
        b_list = {"User_Email": user.email,
                  "User_Id": user.id,
                  "User_Name": user.username,
                  "User_Medal": user.medal,
                  "User_Stage": user.study_stage,
                  "role": user.role,
                  "admin_tag": user.admin_tag,
                  "level": user.level,
                  "status": user.status or 'active',
                  "join_time": user.join_time
                  }
        data.append(b_list)

    return jsonify(data)


@bp.route("/user_avatars/upgrade", methods=['POST'])
@jwt_required()
@audit_log(operation="用户上传头像")
@swag_from('../apidocs/user/user_avatars_upgrade.yaml')
def user_avatars_upgrade():
    User_Email = get_jwt_identity()
    user = UserModel.query.filter_by(email=User_Email).first()
    avatar_id = user.id
    form = AvatarForm(request.files)
    if form.validate():
        # 删除旧头像
        url = user.avatar_url
        if url:
            try:
                os.remove('./data/avatars/' + url)
            except:
                print("删除旧头像失败")
        # 保存新头像到data文件
        file = form.avatar.data
        filename = file.filename
        file.save('./data/avatars/' + str(avatar_id) + '.' + filename.rsplit(".", 1)[1].lower())
        # 保存头像路径到数据库
        user.avatar_url = str(avatar_id) + '.' + filename.rsplit(".", 1)[1].lower()
        db.session.commit()

        return jsonify({
            "code": 200,
            'message': "头像上传完成"
        })
    else:
        return jsonify({
            "code": 400,
            'message': form.errors
        }), 400


@bp.route("/user_avatars")
@jwt_required()
@swag_from('../apidocs/user/user_avatars.yaml')
def user_avatars():
    User_Email = get_jwt_identity()
    user = UserModel.query.filter_by(email=User_Email).first()
    avatar_url = user.avatar_url
    if avatar_url is None:
        return jsonify({
            "code": 200,
            "User_Avatar": None,
            "User_Name": user.username,
            'message': "用户头像不存在"
        })
    a_url = './data/avatars/' + user.avatar_url
    with open(a_url, 'rb') as image_file:
        image_stream = image_file.read()
        image_stream = base64.b64encode(image_stream).decode()
    return jsonify({
        "code": 200,
        "User_Avatar": image_stream,
        "User_Name": user.username,
        "message": "头像图片流传输成功"
    })


# 查询id头像（以图片流的方式返回base64）
@bp.route("/user_avatars_id")
@swag_from('../apidocs/user/user_avatars_id.yaml')
def user_avatars_id():
    User_Id = request.args.get("User_Id")
    user = UserModel.query.filter_by(id=User_Id).first()
    avatar_url = user.avatar_url
    if avatar_url is None:
        return jsonify({
            "code": 200,
            "User_Avatar": None,
            "User_Name": user.username,
            'message': "用户头像不存在"
        })
    a_url = './data/avatars/' + user.avatar_url
    with open(a_url, 'rb') as image_file:
        image_stream = image_file.read()
        image_stream = base64.b64encode(image_stream).decode()
    return jsonify({
        "code": 200,
        "User_Avatar": image_stream,
        "User_Name": user.username,
        "message": "头像图片流传输成功"
    })


@bp.route("/user/edit", methods=['POST'])
@jwt_required()
@audit_log(operation="用户编辑个人信息")
@swag_from('../apidocs/user/user_edit.yaml')
def user_edit():
    User_Email = get_jwt_identity()
    user = UserModel.query.filter_by(email=User_Email).first()
    form = UserInfoForm()
    if form.validate():
        # 初始化变量为 None
        user_name = None
        Student_Id = None
        Introduction = None
        Sex = None
        Institute = None
        Major = None
        Github_Id = None
        Skill_Tags = None
        College = None

        # 检查每个字段是否有值，如果有值则存储到相应的变量中
        if form.User_Name.data:
            user_name = form.User_Name.data
        if form.Student_Id.data:
            Student_Id = form.Student_Id.data
        if form.Introduction.data:
            Introduction = form.Introduction.data
        if form.Sex.data:
            Sex = form.Sex.data
        if form.Institute.data:
            Institute = form.Institute.data
        if form.Major.data:
            Major = form.Major.data
        if form.Github_Id.data:
            Github_Id = form.Github_Id.data
        if form.Skill_Tags.data:
            Skill_Tags = form.Skill_Tags.data
        if form.College.data:
            College = form.College.data

        # 存储到数据库中
        if user_name is not None:
            user.username = user_name
        if Student_Id is not None:
            user.student_id = Student_Id
        if Introduction is not None:
            user.introduction = Introduction
        if Sex is not None:
            user.sex = Sex
        if Institute is not None:
            user.institute = Institute
        if Major is not None:
            user.major = Major
        if Github_Id is not None:
            user.github_id = Github_Id
        if Skill_Tags is not None:
            user.skill_tags = Skill_Tags
        if College is not None:
            user.college = College

        # 提交更改到数据库
        db.session.commit()

        return jsonify({
            "code": 200,
            "message": "用户信息修改完成"
        })

    else:
        return jsonify({
            "code": 400,
            "message": form.errors
        }), 400


# 创建,修改小组（需要管理员权限）
@bp.route("/group_add", methods=['POST'])
@jwt_required()
@check_permission('user_management')
@audit_log(operation="创建或修改小组")
@swag_from('../apidocs/user/group_add.yaml')
def group_add():
    group_name = request.json.get('Group_Name')
    student_ids = request.json.get('Group_member')
    group_type = request.json.get('Group_Type')
    course_id = request.json.get('Course_Id') 
    group_id = request.json.get('Group_Id')
    User_Email = get_jwt_identity()
    user = UserModel.query.filter_by(email=User_Email).first()
    teacher_id = user.id

    # 验证course_id是否提供
    if not course_id:
        return jsonify({
            "code": 403,
            "message": "课程ID不能为空"
        }), 403
    
    # 验证课程是否存在
    course = CourseModel.query.get(course_id)
    if not course:
        return jsonify({
            "code": 404,
            "message": "课程不存在"
        }), 404

    # 获取当前最大的group_id并加1
    max_group = db.session.query(db.func.max(GroupModel.group_id)).scalar()
    new_group_id = (max_group or 0) + 1

    # 删除同名小组
    if(group_id):
        group_exist = GroupModel.query.filter_by(group_id=group_id).delete()
        new_group_id = group_id


    for student in student_ids:
        student_id = student["student_id"]
        student_user = UserModel.query.filter_by(id=student_id).first()
        if student_user is None:
            return jsonify({
                "code": 400,
                "message": "学生不存在"
            }), 400
        
        # 检查该学生是否已经在学习该课程
        existing_group = GroupModel.query.filter_by(
            student_id=student_user.id,
            course_id=course_id
        ).first()
        
        # 如果已存在且不是同名小组（因为同名小组已经被删除了）
        if existing_group:
            return jsonify({
                "code": 409,
                "message": f"学生 {student_user.username}(ID:{student_user.id}) 已经在小组 '{existing_group.name}' 中学习该课程，一个学生不能在多个小组中学习同一课程"
            }), 409

        # 创建小组时添加course_id和group_id
        group = GroupModel(
            teacher_id=teacher_id,
            name=group_name,
            student_id=student_user.id,
            type=group_type,
            course_id=course_id,  # 添加course_id
            group_id=new_group_id
        )
        db.session.add(group)
        
        # 检查学习进度是否存在
        existing_progress = LearningProgressModel.query.filter_by(
            user_id=student_user.id,
            course_id=course_id
        ).first()
        
        # 如果进度不存在或为0，创建新的进度记录
        if not existing_progress:
            new_progress = LearningProgressModel(
                user_id=student_user.id,
                course_id=course_id,
                progress=1  # 设置初始进度为1
            )
            db.session.add(new_progress)

    db.session.commit()  # 统一提交

    return jsonify({
        "code": 200,
        "message": "创建小组成功"
    })


@bp.route("/group")
@jwt_required()
@swag_from('../apidocs/user/group.yaml')
def group():
    User_Email = get_jwt_identity()
    user = UserModel.query.filter_by(email=User_Email).first()

    def process_groups(groups_by_id):
        """处理小组数据，确保每个小组包含完整成员列表"""
        result = []
        for group_key, groups in groups_by_id.items():
            if not groups:
                continue
                
            # 获取第一个组项来提取共同信息
            first_group = groups[0]
            group_id = first_group.group_id
            group_name = first_group.name
            course_id = first_group.course_id
            
            # 获取教师信息
            teacher = UserModel.query.get(first_group.teacher_id)
            teacher_name = teacher.username if teacher else "未知"
            teacher_id = teacher.id if teacher else 0
            
            # 获取课程信息
            course = CourseModel.query.get(course_id)
            course_title = course.title if course else "未知课程"
            
            # 收集所有学生信息
            students = []
            for g in groups:
                student = UserModel.query.get(g.student_id)
                if student:
                    students.append({
                        'Student_Id': student.id,
                        'Student': student.username
                    })
            
            # 构建小组完整信息
            group_info = {
                'group_id': group_id,
                'course_id': course_id,
                'title': course_title,
                'students': students,
                'teacher': teacher_name,
                'teacher_id': teacher_id,
                'group_name': group_name
            }
            
            result.append(group_info)
            
        return result

    # 先获取用户可能相关的所有小组ID
    relevant_group_ids = set()
    
    # 1. 用户作为教师的小组
    teacher_groups = GroupModel.query.filter_by(teacher_id=user.id).all()
    for group in teacher_groups:
        relevant_group_ids.add(group.group_id)
    
    # 2. 用户作为学生的小组
    student_groups = GroupModel.query.filter_by(student_id=user.id).all()
    for group in student_groups:
        relevant_group_ids.add(group.group_id)
    
    # 查询所有相关小组的完整信息
    all_groups = {}
    for group_id in relevant_group_ids:
        # 获取这个group_id的所有组成员记录
        complete_group = GroupModel.query.filter_by(group_id=group_id).all()
        
        # 按类型分组
        for group in complete_group:
            key = (group.group_id, group.type)
            if key not in all_groups:
                all_groups[key] = []
            all_groups[key].append(group)
    
    # 分离学习小组和项目小组
    study_groups = {k: v for k, v in all_groups.items() if k[1] == "study"}
    project_groups = {k: v for k, v in all_groups.items() if k[1] == "project"}
    
    # 处理分组数据
    study_data = process_groups(study_groups)
    project_data = process_groups(project_groups)
    
    # 返回结果，与原来的格式一致
    return jsonify({
        "code": 200,
        "message": "获取小组成功",
        "study_group": study_data,
        "project_group": project_data
    })


from sqlalchemy.orm import aliased


@bp.route("/group/list")
@jwt_required()
@check_permission('user_management')
@swag_from('../apidocs/user/group_list.yaml')
def group_list():
    # 创建别名用于区分导师和学生
    Student = aliased(UserModel)
    Teacher = aliased(UserModel)

    # 查询所有小组数据，包含type、name和course_id字段
    # 修改查询语句，添加GroupModel.id和GroupModel.course_id
    query_result = (
        db.session.query(
            GroupModel.group_id.label('group_id'),
            GroupModel.course_id.label('course_id'),  # 添加course_id
            Teacher.id.label('teacher_id'),
            Student.id.label('student_id'),
            GroupModel.type,
            GroupModel.name
        )
        .select_from(GroupModel)
        .join(Teacher, GroupModel.teacher_id == Teacher.id)
        .join(Student, GroupModel.student_id == Student.id)
        .all()
    )

    # 修改字典结构，键改为(teacher_id, group_name, group_id, course_id)
    study_groups = {}
    project_groups = {}

    for group_id, course_id, teacher_id, student_id, group_type, group_name in query_result:
        student = UserModel.query.get(student_id)
        student_info = {
            "Student_Id": student_id,
            "Student": student.username
        }

        target_dict = study_groups if group_type == 'study' else project_groups
        group_key = (teacher_id, group_name, group_id, course_id)  # 添加course_id到键中

        if group_key not in target_dict:
            target_dict[group_key] = []
        target_dict[group_key].append(student_info)

    # 修改结果构建，添加group_id、course_id和title
    result1 = []
    for (teacher_id, group_name, group_id, course_id), students in study_groups.items():
        teacher = UserModel.query.get(teacher_id)
        # 获取课程标题
        course = CourseModel.query.get(course_id)
        course_title = course.title if course else "未知课程"
        
        result1.append({
            "group_id": group_id,
            "course_id": course_id,
            "title": course_title,  # 添加课程标题
            "teacher_id": teacher_id,
            "teacher": teacher.username,
            "group_name": group_name,
            "group": students
        })

    result2 = []
    for (teacher_id, group_name, group_id, course_id), students in project_groups.items():
        teacher = UserModel.query.get(teacher_id)
        # 获取课程标题
        course = CourseModel.query.get(course_id)
        course_title = course.title if course else "未知课程"
        
        result2.append({
            "group_id": group_id,
            "course_id": course_id,
            "title": course_title,  # 添加课程标题
            "teacher_id": teacher_id,
            "teacher": teacher.username,
            "group_name": group_name,
            "group": students
        })

    return jsonify({
        "code": 200,
        "message": "获取所有小组成功",
        "study_groups": result1,
        "project_groups": result2
    }), 200


@bp.route("/group/delete", methods=['POST'])
@jwt_required()
@check_permission('user_management')
@audit_log(operation="删除小组")
@swag_from('../apidocs/user/group_delete.yaml')
def group_delete():
    group_id = request.json.get('Group_Id')
    groups = GroupModel.query.filter_by(group_id=group_id).all()
    if not groups:
        return jsonify({
            "code": 401,
            "message": "小组不存在"
        }), 401
    for group in groups:
        db.session.delete(group)
    db.session.commit()
    return jsonify({
        "code": 200,
        "message": "删除小组成功"
    })

@bp.route("/group/attendence_yesterday", methods=['POST'])
@jwt_required()
@swag_from('../apidocs/user/attendence_yesterday.yaml')
def attendence_yesterday():
    # 获取当前用户
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    if not user:
        return jsonify({
            "code": 404,
            "message": "用户不存在"
        }), 404

    # 获取小组ID
    data = request.get_json()
    group_id = data.get('group_id')
    if not group_id:
        return jsonify({
            "code": 400,
            "message": "小组ID不能为空"
        }), 400

    # 查询小组是否存在
    group_members = GroupModel.query.filter_by(group_id=group_id).all()
    if not group_members:
        return jsonify({
            "code": 404,
            "message": "小组不存在"
        }), 404

    # 获取小组名称和组长信息
    group_name = group_members[0].name if group_members else "未知小组"
    teacher_id = group_members[0].teacher_id if group_members else None
    teacher = UserModel.query.filter_by(id=teacher_id).first()
    teacher_name = teacher.username if teacher else "未知组长"

    # 验证用户是否为该小组的成员或组长
    is_teacher = any(group.teacher_id == user.id for group in group_members)
    is_student = any(group.student_id == user.id for group in group_members)

    if not (is_teacher or is_student):
        return jsonify({
            "code": 403,
            "message": "您不是该小组的成员或组长"
        }), 403

    # 获取昨天的日期
    import datetime
    yesterday = datetime.date.today() - datetime.timedelta(days=1)

    # 收集小组成员信息和签到记录
    members_attendance = []
    
    # 获取所有小组成员
    student_ids = [group.student_id for group in group_members]
    
    # 获取每个成员的信息和签到记录
    for student_id in student_ids:
        student = UserModel.query.filter_by(id=student_id).first()
        if not student:
            continue
            
        # 查找昨天的签到记录
        check_record = CheckRecord.query.filter_by(
            user_id=student_id,
            date=yesterday
        ).first()
        
        # 构建成员信息和签到记录
        member_info = {
            "student_id": student_id,
            "student_name": student.username,
            "attendance": {
                "check_in": check_record.check_in.strftime('%Y-%m-%d %H:%M:%S') if check_record and check_record.check_in else None,
                "check_out": check_record.check_out.strftime('%Y-%m-%d %H:%M:%S') if check_record and check_record.check_out else None,
                "duration": check_record.duration if check_record else None
            } if check_record else None
        }
        
        members_attendance.append(member_info)
    
    return jsonify({
        "code": 200,
        "message": "获取小组成员昨日签到记录成功",
        "data": {
            "group_id": group_id,
            "group_name": group_name,
            "teacher_id": teacher_id,
            "teacher_name": teacher_name,
            "date": yesterday.strftime('%Y-%m-%d'),
            "members": members_attendance
        }
    }), 200

@bp.route("/group/attendence_by_date", methods=['POST'])
@jwt_required()
@swag_from('../apidocs/user/attendence_by_date.yaml')
def attendence_by_date():
    # 获取当前用户
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    if not user:
        return jsonify({
            "code": 404,
            "message": "用户不存在"
        }), 404

    # 获取请求参数
    data = request.get_json()
    group_id = data.get('group_id')
    start_date_str = data.get('startdate')
    end_date_str = data.get('enddate')

    if not group_id:
        return jsonify({
            "code": 400,
            "message": "小组ID不能为空"
        }), 400

    if not start_date_str:
        return jsonify({
            "code": 400,
            "message": "起始日期不能为空"
        }), 400

    # 查询小组是否存在
    group_members = GroupModel.query.filter_by(group_id=group_id).all()
    if not group_members:
        return jsonify({
            "code": 404,
            "message": "小组不存在"
        }), 404

    # 获取小组名称和组长信息
    group_name = group_members[0].name if group_members else "未知小组"
    teacher_id = group_members[0].teacher_id if group_members else None
    teacher = UserModel.query.filter_by(id=teacher_id).first()
    teacher_name = teacher.username if teacher else "未知组长"

    # 验证用户是否为该小组的成员或组长
    is_teacher = any(group.teacher_id == user.id for group in group_members)
    is_student = any(group.student_id == user.id for group in group_members)

    if not (is_teacher or is_student):
        return jsonify({
            "code": 403,
            "message": "您不是该小组的成员或组长"
        }), 403

    # 解析日期
    import datetime

    # 处理起始日期
    try:
        parts = start_date_str.split('-')
        if len(parts) == 1:  # 只有年份，如"2024"
            start_date = datetime.date(int(parts[0]), 1, 1)
        elif len(parts) == 2:  # 有年份和月份，如"2024-03"
            start_date = datetime.date(int(parts[0]), int(parts[1]), 1)
        else:  # 完整日期，如"2024-03-15"
            start_date = datetime.date(int(parts[0]), int(parts[1]), int(parts[2]))
    except ValueError:
        return jsonify({
            "code": 400,
            "message": "起始日期格式错误"
        }), 400

    # 处理结束日期
    if not end_date_str:
        # 如果没有提供结束日期，根据起始日期格式决定结束日期
        start_parts = start_date_str.split('-')
        if len(start_parts) == 1:  # 只有年份，如"2024"
            # 使用该年的最后一天
            end_date = datetime.date(int(start_parts[0]), 12, 31)
        elif len(start_parts) == 2:  # 有年份和月份，如"2024-03"
            # 获取该月的最后一天
            year = int(start_parts[0])
            month = int(start_parts[1])
            if month == 12:  # 12月
                last_day = 31
            else:
                # 下个月的第一天减去一天
                next_month = datetime.date(year, month + 1, 1)
                last_day = (next_month - datetime.timedelta(days=1)).day
            end_date = datetime.date(year, month, last_day)
        else:  # 完整日期，如"2024-03-15"
            # 使用起始日期当天
            end_date = start_date
    else:
        try:
            parts = end_date_str.split('-')
            if len(parts) == 1:  # 只有年份，如"2024"
                end_date = datetime.date(int(parts[0]), 12, 31)  # 年份的最后一天
            elif len(parts) == 2:  # 有年份和月份，如"2024-03"
                # 获取该月的最后一天
                if int(parts[1]) == 12:  # 12月
                    last_day = 31
                else:
                    # 下个月的第一天减去一天
                    next_month = datetime.date(int(parts[0]), int(parts[1]) + 1, 1)
                    last_day = (next_month - datetime.timedelta(days=1)).day
                end_date = datetime.date(int(parts[0]), int(parts[1]), last_day)
            else:  # 完整日期，如"2024-03-15"
                end_date = datetime.date(int(parts[0]), int(parts[1]), int(parts[2]))
        except ValueError:
            return jsonify({
                "code": 400,
                "message": "结束日期格式错误"
            }), 400

    # 检查日期范围是否有效
    if start_date > end_date:
        return jsonify({
            "code": 400,
            "message": "起始日期不能晚于结束日期"
        }), 400

    # 构建缓存键
    cache_key = f"group_{group_id}_{start_date}_{end_date}"

    # 尝试从缓存获取数据
    cached_data = redis_client.get(cache_key)
    if cached_data:
        import json
        return jsonify({
            "code": 200,
            "message": "成功获取缓存的签到统计数据",
            "data": json.loads(cached_data)
        }), 200

    # 获取所有小组成员
    student_ids = [group.student_id for group in group_members]

    # 统计每个成员在日期范围内的签到情况
    members_attendance = []
    for student_id in student_ids:
        student = UserModel.query.filter_by(id=student_id).first()
        if not student:
            continue

        # 查询该学生在日期范围内的签到记录
        check_records = CheckRecord.query.filter(
            CheckRecord.user_id == student_id,
            CheckRecord.date >= start_date,
            CheckRecord.date <= end_date
        ).all()

        # 按日期分组签到记录
        from collections import defaultdict
        daily_checkins = defaultdict(list)
        for record in check_records:
            date_str = record.date.strftime('%Y-%m-%d')
            daily_checkins[date_str].append(record)
            
        
        # 计算每天的签到签退时间差并累加
        daily_attendance = []
        for date_str, records in daily_checkins.items():
            total_duration_seconds = 0
            checkin_records = []
            checkout_records = []
            
            # 分离签到和签退记录
            for record in records:
                if record.check_in:
                    checkin_records.append(record.check_in)
                if record.check_out:
                    checkout_records.append(record.check_out)
            
            # 确保签到和签退记录成对出现
            pair_count = min(len(checkin_records), len(checkout_records))
            
            # 计算每对签到签退的时间差并累加
            for i in range(pair_count):
                checkin_time = checkin_records[i]
                checkout_time = checkout_records[i]
                
                # 转换为datetime对象以便计算
                checkin_dt = datetime.datetime.strptime(checkin_time.strftime('%H:%M:%S'), '%H:%M:%S')
                checkout_dt = datetime.datetime.strptime(checkout_time.strftime('%H:%M:%S'), '%H:%M:%S')
                
                # 计算时间差
                duration = checkout_dt - checkin_dt
                total_duration_seconds += duration.total_seconds()
            
            # 格式化总时长
            total_minutes = total_duration_seconds // 60
            hours = int(total_minutes // 60)
            minutes = int(total_minutes % 60)
            duration_str = f"{hours}小时{minutes}分钟"
            
            # 获取最早签到和最晚签退时间
            earliest_checkin = min(checkin_records).strftime('%H:%M:%S') if checkin_records else None
            latest_checkout = max(checkout_records).strftime('%H:%M:%S') if checkout_records else None
            
            daily_attendance.append({
                "date": date_str,
                "earliest_checkin_time": earliest_checkin,
                "latest_checkout_time": latest_checkout,
                "total_checkin_duration": duration_str
            })

        # 统计签到天数（按不同的日期计算，而非签到次数）
        check_days = len(daily_checkins)
        
        # 打印统计汇总信息
        print(f"学生 {student.username} 在指定时间段内的签到统计:")
        print(f"  总签到天数: {check_days}")
        print(f"  总签到次数: {len(check_records)}")
        print(f"  日均签到次数: {len(check_records)/check_days if check_days > 0 else 0:.2f}")

        # 构建成员签到统计
        member_info = {
            "student_id": student_id,
            "student_name": student.username,
            "check_days": check_days,
            "daily_attendance": daily_attendance
        }

        members_attendance.append(member_info)

    # 构建返回数据
    result = {
        "group_id": group_id,
        "group_name": group_name,
        "teacher_id": teacher_id,
        "teacher_name": teacher_name,
        "start_date": start_date.strftime('%Y-%m-%d'),
        "end_date": end_date.strftime('%Y-%m-%d'),
        "members": members_attendance
    }

    # 缓存数据（设置30分钟过期）
    import json
    redis_client.setex(cache_key, 1800, json.dumps(result))

    return jsonify({
        "code": 200,
        "message": "成功获取签到统计数据",
        "data": result
    }), 200
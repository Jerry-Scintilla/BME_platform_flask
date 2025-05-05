import os
from flask import Blueprint, request, redirect, jsonify
import base64

# 导入拓展
from exts import db

# 导入数据库表
from models import UserModel, GroupModel

# 导入表单验证
from .forms import AvatarForm
from .forms import UserInfoForm

# 导入token验证模块
from flask_jwt_extended import (create_access_token, get_jwt_identity, jwt_required, JWTManager)

# 导入api文档模块
from flasgger import swag_from

bp = Blueprint("user", __name__, url_prefix="/user")


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

    User_Mode = user.user_mode

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
        "User_Mode": User_Mode,
        "join_time": User_Time,
        "User_Id": User_Id,
        "Student_Id": Student_Id,
        "Introduction": Introduction,
        "User_Sex": User_Sex,
        "Institute": Institute,
        "Major": Major,
        "Github_Id": Github_Id,
        "Skill_Tags": Skill_Tags,
    }
    return jsonify(data)


@bp.route("/user_list")
@jwt_required()
@swag_from('../apidocs/user/user_list.yaml')
def user_list():
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    mode = user.user_mode
    if mode != 'admin':
        return jsonify({
            "code": 400,
            'message': "用户权限不够"
        }), 400
    a_list = UserModel.query.all()
    data = []
    for user in a_list:
        b_list = {"User_Email": user.email,
                  "User_Id": user.id,
                  "User_Name": user.username,
                  "User_Medal": user.medal,
                  "User_Stage": user.study_stage,
                  "User_Mode": user.user_mode,
                  "join_time": user.join_time
                  }
        data.append(b_list)

    return jsonify(data)


@bp.route("/user_avatars/upgrade", methods=['POST'])
@jwt_required()
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
@swag_from('../apidocs/user/group_add.yaml')
def group_add():
    User_Email = get_jwt_identity()
    user = UserModel.query.filter_by(email=User_Email).first()
    mode = user.user_mode
    if mode != 'admin':
        return jsonify({
            "code": 400,
            'message': "用户权限不够"
        }), 400

    group_name = request.json.get('Group_Name')
    student_ids = request.json.get('Group_member')
    group_type = request.json.get('Group_Type')
    teacher_id = user.id

    # 获取当前最大的group_id并加1
    max_group = db.session.query(db.func.max(GroupModel.group_id)).scalar()
    new_group_id = (max_group or 0) + 1

    # 删除同名小组
    group_exist = GroupModel.query.filter_by(name=group_name).delete()

    for student in student_ids:
        student_id = student["student_id"]
        student_user = UserModel.query.filter_by(id=student_id).first()
        if student_user is None:
            return jsonify({
                "code": 400,
                "message": "学生不存在"
            }), 400

        # 创建小组时添加group_id
        group = GroupModel(
            teacher_id=teacher_id,
            name=group_name,
            student_id=student_user.id,
            type=group_type,
            group_id=new_group_id  # 新增group_id
        )
        db.session.add(group)

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

    def process_groups(groups):
        """处理小组数据并按名称分组"""
        grouped = {}
        for group in groups:
            group_name = group.name
            if group_name not in grouped:
                # 获取教师信息（假设每个小组的 teacher_id 一致）
                teacher = UserModel.query.get(group.teacher_id)
                grouped[group_name] = {
                    'students': [],
                    'teacher': teacher.username,
                    'teacher_id': teacher.id,
                    'group_id': group.group_id
                }
            # 获取学生信息
            student = UserModel.query.get(group.student_id)
            grouped[group_name]['students'].append({
                'Student_Id': student.id,
                'Student': student.username
            })
        # 转换为前端需要的格式
        return [{
            'group_id': info['group_id'],
            'group_name': name,
            'students': info['students'],
            'teacher': info['teacher'],
            'teacher_id': info['teacher_id']
        } for name, info in grouped.items()]

    # 管理员视角
    if user.user_mode == 'admin':
        # 处理学习小组
        study_groups = GroupModel.query.filter_by(
            teacher_id=user.id,
            type="study"
        ).all()
        study_data = process_groups(study_groups)

        # 处理项目小组
        project_groups = GroupModel.query.filter_by(
            teacher_id=user.id,
            type="project"
        ).all()
        project_data = process_groups(project_groups)

        return jsonify({
            "code": 200,
            "message": "获取小组成功",
            "study_group": study_data,
            "project_group": project_data
        })

    # 学生视角
    elif user.user_mode == 'user':
        def get_user_groups(group_type):
            """获取学生所属小组并分组"""
            # 1. 找到学生加入的所有该类型小组
            user_groups = GroupModel.query.filter_by(
                student_id=user.id,
                type=group_type
            ).all()

            # 2. 按教师分组（假设学生可能加入不同教师的小组）
            teachers_map = {}
            for ug in user_groups:
                teacher_id = ug.teacher_id
                if teacher_id not in teachers_map:
                    teachers_map[teacher_id] = []
                teachers_map[teacher_id].append(ug)

            # 3. 处理每个教师的小组
            result = []
            for teacher_id, groups in teachers_map.items():
                # 获取该教师创建的所有该类型小组
                all_groups = GroupModel.query.filter_by(
                    teacher_id=teacher_id,
                    type=group_type
                ).all()
                # 按名称分组处理
                result.extend(process_groups(all_groups))

            return result

        # 获取学习型小组（按类型分别处理）
        study_data = get_user_groups("study")
        project_data = get_user_groups("project")

        return jsonify({
            "code": 200,
            "message": "获取小组成功",
            "study_group": study_data,
            "project_group": project_data
        })

    return jsonify({"code": 403, "message": "权限不足"})


from sqlalchemy.orm import aliased


@bp.route("/group/list")
@jwt_required()
@swag_from('../apidocs/user/group_list.yaml')
def group_list():
    User_Email = get_jwt_identity()
    user = UserModel.query.filter_by(email=User_Email).first()
    mode = user.user_mode
    if mode != 'admin':
        return jsonify({
            "code": 400,
            'message': "用户权限不够"
        }), 400

    # 创建别名用于区分导师和学生
    Student = aliased(UserModel)
    Teacher = aliased(UserModel)

    # 查询所有小组数据，包含type和name字段
    # 修改查询语句，添加GroupModel.id
    query_result = (
        db.session.query(
            GroupModel.group_id.label('group_id'),  # 新增小组ID
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

    # 修改字典结构，键改为(teacher_id, group_name, group_id)
    study_groups = {}
    project_groups = {}

    for group_id, teacher_id, student_id, group_type, group_name in query_result:
        student = UserModel.query.get(student_id)
        student_info = {
            "Student_Id": student_id,
            "Student": student.username
        }

        target_dict = study_groups if group_type == 'study' else project_groups
        group_key = (teacher_id, group_name, group_id)  # 保留group_id

        if group_key not in target_dict:
            target_dict[group_key] = []
        target_dict[group_key].append(student_info)

    # 修改结果构建，添加group_id
    result1 = []
    for (teacher_id, group_name, group_id), students in study_groups.items():
        teacher = UserModel.query.get(teacher_id)
        result1.append({
            "group_id": group_id,  # 新增小组ID
            "teacher_id": teacher_id,
            "teacher": teacher.username,
            "group_name": group_name,
            "group": students
        })

    result2 = []
    for (teacher_id, group_name, group_id), students in project_groups.items():
        teacher = UserModel.query.get(teacher_id)
        result2.append({
            "group_id": group_id,  # 新增小组ID
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
@swag_from('../apidocs/user/group_delete.yaml')
def group_delete():
    User_Email = get_jwt_identity()
    user = UserModel.query.filter_by(email=User_Email).first()
    mode = user.user_mode
    if mode != 'admin':
        return jsonify({
            "code": 400,
            'message': "用户权限不够"
        }), 400

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


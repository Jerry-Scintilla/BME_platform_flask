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
    if mode!= 'admin':
        return jsonify({
            "code": 400,
           'message': "用户权限不够"
        }), 400

    group_name = request.json.get('Group_Name')
    student_ids = request.json.get('Student_Ids')
    teacher_id = user.id
    for student in student_ids:
        student_id = student["student_id"]
        student_user = UserModel.query.filter_by(id=student_id).first()
        if student_user is None:
            return jsonify({
                "code": 400,
                "message": "学生不存在"
            }), 400
        if student_user.user_mode != 'user':
            return jsonify({
                "code": 401,
                "message": "学生不是普通用户"
            }), 401
        student = GroupModel.query.filter_by(student_id=student_id).first()
        if student:
            return jsonify({
                "code": 402,
                "message": "学生已经加入小组"
            })

        group_exist = GroupModel.query.filter_by(teacher_id=teacher_id).delete()

        group = GroupModel(teacher_id=teacher_id, name=group_name, student_id=student_user.id)
        db.session.add(group)
        db.session.commit()

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
    if user.user_mode == 'admin':
        groups = GroupModel.query.filter_by(teacher_id=user.id).all()
        if groups is None:
            return jsonify({
                "code": 400,
                "message": "该导师没有小组"
            }), 400
        data = []
        for group in groups:
            student = UserModel.query.filter_by(id=group.student_id).first()
            b_list = {
                "Student_Id": student.id,
                'Student': student.username
            }
            data.append(b_list)

        return jsonify({
            "code": 200,
            "message": "获取小组成功",
            "teacher": user.username,
            "teacher_id": user.id,
            "group": data
        })

    if user.user_mode == 'user':
        groups = GroupModel.query.filter_by(student_id=user.id).all()
        if groups is None:
            return jsonify({
                "code": 401,
                "message": "该学生没有加入小组"
            }), 401
        data = []
        teacher = UserModel.query.filter_by(id=GroupModel.query.filter_by(student_id=user.id).first().teacher_id).first()
        for group in groups:
            student = UserModel.query.filter_by(id=group.student_id).first()
            b_list = {
                "Student_Id": student.id,
                'Student': student.username
            }
            data.append(b_list)

        return jsonify({
            "code": 200,
            "message": "获取小组成功",
            "teacher": teacher.username,
            "teacher_id": teacher.id,
            "group": data
        })

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

        # 查询所有小组数据
    query_result = (
        db.session.query(
            Teacher.id.label('teacher_name'),
            Student.id.label('student_name')
        )
        .select_from(GroupModel)
        .join(Teacher, GroupModel.teacher_id == Teacher.id)
        .join(Student, GroupModel.student_id == Student.id)
        .all()
    )

    # 按导师分组整理数据
    grouped_data = {}
    for teacher, student in query_result:
        if teacher not in grouped_data:
            grouped_data[teacher] = []
        grouped_data[teacher].append({
            "Student_Id": student,
            "Student": UserModel.query.filter_by(id=student).first().username
        })

    # 构造最终响应格式
    result = [{
        "teacher_id": teacher,
        "teacher": UserModel.query.filter_by(id=teacher).first().username,
        "group": students
    } for teacher, students in grouped_data.items()]

    return jsonify({
        "code": 200,
        "message": "获取所有小组成功",
        "groups": result
    }), 200
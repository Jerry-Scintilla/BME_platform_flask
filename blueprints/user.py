import os
from flask import Blueprint, request, redirect, jsonify
import base64

# 导入拓展
from exts import db

# 导入数据库表
from models import UserModel

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
        })
    a_list = UserModel.query.all()
    data = []
    for user in a_list:
        b_list = {"User_Email": user.email,
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
            os.remove('./data/avatars/' + url)
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
        })


@bp.route("/user_avatars")
@jwt_required()
@swag_from('../apidocs/user/user_avatars.yaml')
def user_avatars():
    User_Email = get_jwt_identity()
    user = UserModel.query.filter_by(email=User_Email).first()
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
        Student_Id = None
        Introduction = None
        Sex = None
        Institute = None
        Major = None
        Github_Id = None
        Skill_Tags = None

        # 检查每个字段是否有值，如果有值则存储到相应的变量中
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
        })
from flask import Blueprint, request, redirect, jsonify

# 导入拓展
from exts import db

# 导入数据库表
from models import MedalModel, MedalUserModel, UserModel

# 导入表单验证
from .forms import MedalForm

# 导入token验证模块
from flask_jwt_extended import (create_access_token, get_jwt_identity, jwt_required, JWTManager)

# 导入api文档模块
from flasgger import swag_from

bp = Blueprint("medal", __name__, url_prefix="/medal")


# 创建勋章
@bp.route("/medal_create", methods=["POST"])
@jwt_required()
@swag_from('../apidocs/medal/medal_create.yaml')
def medal_create():
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    mode = user.user_mode
    if mode != 'admin':
        return jsonify({
            "code": 400,
            'message': "用户权限不够"
        }), 400

    form = MedalForm()
    if form.validate():
        medal_name = form.Medal_Name.data
        description = form.Medal_Name_CN.data
        tag = form.Medal_Tag.data
        medal = MedalModel(medal_name=medal_name, description=description, tags=tag)
        db.session.add(medal)
        db.session.commit()
        return jsonify({
            "code": 200,
            "message": "勋章创建成功"
        })
    else:
        return jsonify({
            "code": 401,
            "message": "勋章创建失败",
            "error": form.errors
        }), 401

# 查询勋章列表
@bp.route("/medal_list")
@jwt_required()
@swag_from('../apidocs/medal/medal_list.yaml')
def medal_list():
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    mode = user.user_mode
    if mode != 'admin':
        return jsonify({
            "code": 400,
            'message': "用户权限不够"
        }), 400

    medals = MedalModel.query.all()
    data = []
    for medal in medals:
        data.append({
            "Medal_Name": medal.medal_name,
            "Medal_Description": medal.description,
            "Medal_Tag": medal.tags,
            "Medal_Id": medal.id
        })

    return jsonify({
        "code": 200,
        "message": "获取勋章列表成功",
        "medal": data
    })

# 删除勋章
@bp.route("/medal_delete", methods=["POST"])
@jwt_required()
@swag_from('../apidocs/medal/medal_delete.yaml')
def medal_delete():
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    mode = user.user_mode
    if mode != 'admin':
        return jsonify({
            "code": 400,
            'message': "用户权限不够"
        }), 400

    medal_id = request.json.get("Medal_Id")
    medal = MedalModel.query.filter_by(id=medal_id).first()
    try:
        db.session.delete(medal)
        db.session.commit()
        return jsonify({
            "code": 200,
            "message": "勋章删除成功"
        })

    except Exception as e:
        return jsonify({
            "code": 402,
            'message': str(e)
        }), 402

# 修改勋章（需要改什么就传什么key）
@bp.route("/medal_edit", methods=["POST"])
@jwt_required()
@swag_from('../apidocs/medal/medal_edit.yaml')
def medal_edit():
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    mode = user.user_mode
    if mode != 'admin':
        return jsonify({
            "code": 400,
            'message': "用户权限不够"
        }), 400

    Medal_Id = request.json.get("Medal_Id")
    Medal_Name = request.json.get("Medal_Name")
    Medal_Description = request.json.get("Medal_Description")
    Medal_Tag = request.json.get("Medal_Tag")

    medal = MedalModel.query.filter_by(id=Medal_Id).first()
    if not medal:
        return jsonify({
            "code": 401,
            "message": "勋章不存在"
        }), 401

    if Medal_Name:
        medal.medal_name = Medal_Name
    if Medal_Tag:
        medal.tags = Medal_Tag
    if Medal_Description:
        medal.description = Medal_Description

    db.session.commit()
    return jsonify({
        "code": 200,
        "message": "勋章修改成功",
        "Medal_Name": medal.medal_name,
        "Medal_Tag": medal.tags,
        "Medal_Description": medal.description
    })

# 创建用户勋章
@bp.route("/user_medal_add", methods=["POST"])
@jwt_required()
@swag_from('../apidocs/medal/user_medal_add.yaml')
def user_medal_add():
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    mode = user.user_mode
    if mode!= 'admin':
        return jsonify({
            "code": 400,
           'message': "用户权限不够"
        }), 400

    student_id = request.json.get("Student_Id")
    medal_name = request.json.get("Medal_Name")
    description = request.json.get("Medal_Description")
    user = UserModel.query.filter_by(id=student_id).first()
    if not user:
        return jsonify({
            "code": 401,
            "message": "学生不存在"
        }), 401
    medal = MedalModel.query.filter_by(medal_name=medal_name).first()
    if not medal:
        return jsonify({
            "code": 402,
            "message": "勋章不存在"
        })
    medal_user = MedalUserModel.query.filter_by(user_id=user.id, medal_id=medal.id).first()
    if medal_user:
        return jsonify({
            "code": 403,
            "message": "用户已经拥有该勋章"
        }), 403
    medal_user = MedalUserModel(user_id=user.id, medal_id=medal.id, description=description)
    user.medal = user.medal + 1
    db.session.add(medal_user)
    db.session.commit()
    return jsonify({
        "code": 200,
        "message": "勋章授予成功"
    })

# 查询勋章列表
@bp.route("/user_medal_list")
@jwt_required()
@swag_from('../apidocs/medal/user_medal_list.yaml')
def user_medal_list():
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    mode = user.user_mode
    if mode!= 'admin':
        return jsonify({
            "code": 400,
           'message': "用户权限不够"
        }), 400

    student_id = request.args.get("Student_Id")
    student_name = UserModel.query.filter_by(id=student_id).first().username
    medals = MedalUserModel.query.filter_by(user_id=student_id).all()
    data = []
    for medal in medals:
        data.append({
            "Medal_Id": medal.id,
            "Medal_Name": medal.medal.medal_name,
            "Medal_Tag": medal.medal.tags,
            "Medal_Name_CN": medal.medal.description
        })

    return jsonify({
        "code": 200,
        "Student": student_name,
        "Medal": data,
        "message": "查询用户勋章列表成功"
    })


@bp.route("/user_medal_show")
@jwt_required()
@swag_from('../apidocs/medal/user_medal_show.yaml')
def user_medal_show():
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    student_id = user.id
    medals = MedalUserModel.query.filter_by(user_id=student_id).all()
    medal_list = MedalModel.query.all()
    data = []
    for medal_ in medal_list:
        found = False  # 添加标记位
        for medal in medals:
            if medal.medal_id == medal_.id:
                data.append({
                    "Medal_Id": medal.medal_id,
                    "Medal_Name": medal_.medal_name,
                    "Medal_Tag": medal_.tags,
                    "Medal_Name_CN": medal_.description,
                    "Get_Time": medal.get_time.strftime('%Y-%m-%d'),
                    "Description": medal.description
                })
                found = True
                break  # 找到后立即跳出内层循环

        # 只有未找到匹配时才添加 Null 条目
        if not found:
            data.append({
                "Medal_Id": medal_.id,
                "Medal_Name": medal_.medal_name,
                "Medal_Tag": medal_.tags,
                "Medal_Name_CN": medal_.description,
                "Get_Time": None,
                "Description": None
            })

    return jsonify({
        "code": 200,
        "Medal": data,
        "User_Id": user.id,
        "message": "查询用户勋章列表成功"
    })



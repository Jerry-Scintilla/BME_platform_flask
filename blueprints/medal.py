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
        description = form.Medal_Description.data
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




from flask import Blueprint, request, redirect, jsonify

# 导入数据库表
from models import UserModel

# 导入token验证模块
from flask_jwt_extended import (create_access_token, get_jwt_identity, jwt_required, JWTManager)


bp = Blueprint("user", __name__, url_prefix="/user")

# 用户信息请求
@bp.route("/user_index")
@jwt_required()
def user_index():
    User_Email = get_jwt_identity()
    # print(User_Email)
    user = UserModel.query.filter_by(email=User_Email).first()
    User_Name = user.username
    User_Medal = user.medal
    User_Stage = user.study_stage
    join_time = user.join_time
    User_Time = join_time.strftime('%Y-%m-%d')


    data = {
        "code": 200,
        "message": "获取用户数据成功",
        "User_Email": User_Email,
        "User_Name": User_Name,
        "User_Medal": User_Medal,
        "User_Stage": User_Stage,
        "join_time": User_Time
    }
    return jsonify(data)

from flask import Blueprint, render_template, request ,redirect
from pyexpat.errors import messages

from .forms import RegisterForm, LoginForm
from models import UserModel
from exts import db
from flask import jsonify
from flask_jwt_extended import JWTManager, create_access_token, jwt_required, get_jwt_identity

bp = Blueprint("auth", __name__, url_prefix="/auth")


@bp.route("/register", methods=["POST"])
def register():
    form = RegisterForm()
    if form.validate():
        email = form.User_Email.data
        password = form.User_Password.data
        username = form.User_Name.data
        user = UserModel.query.filter_by(email=email).first()
        if user:
            data = {
                "code": 400,
                "message": "邮箱已存在",
            }
            return jsonify(data)
        else:
            user = UserModel(email=email, password=password, username=username)
            db.session.add(user)
            db.session.commit()
            token = create_access_token(identity=email)
            data = {
                "code": 200,
                "message":"注册成功",
                "token": token,
                "User_Name": username,
            }
        return jsonify(data)
    else:
        data = {
            "code": 400,
            "message":form.errors,
        }
        return jsonify(data)


@bp.route("/login", methods=["POST"])
def login():
    form = LoginForm()
    if form.validate():
        email = form.User_Email.data
        password = form.User_Password.data
        user = UserModel.query.filter_by(email=email).first()
        if not user:
            code = 400
            msg = "用户不存在，请检查邮箱输入是否正确"
            token = "Null"
            User_Name = "Null"
        if user.password == password:
            code = 200
            msg = "登录成功"
            token = create_access_token(identity=email)
            User_Name = user.username
        else:
            code = 400
            msg = "密码错误"
            token = "Null"
            User_Name = "Null"
        data = {
            "code": code,
            "message": msg,
            "token": token,
            "User_Name": User_Name,
        }
        return jsonify(data)

    else:
        data = {
            "code": 400,
            "message": form.errors,
        }
        return jsonify(data)




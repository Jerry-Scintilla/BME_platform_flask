from flask import Blueprint, render_template, request, redirect
from pyexpat.errors import messages

from .forms import RegisterForm, LoginForm
from models import UserModel, EmailCaptchaModel
from exts import db, mail
from flask import jsonify
from flask_jwt_extended import JWTManager, create_access_token, jwt_required, get_jwt_identity

from flask_mail import Message
import string
import random

bp = Blueprint("auth", __name__, url_prefix="/auth")


@bp.route("/register", methods=["POST"])
def register():
    form = RegisterForm()
    if form.validate():
        email = form.User_Email.data
        password = form.User_Password.data
        username = form.User_Name.data
        captcha = form.User_Captcha.data
        user = UserModel.query.filter_by(email=email).first()
        captcha_model = EmailCaptchaModel.query.filter_by(captcha=captcha).first()
        if not captcha_model:
            data = {
                "code": 400,
                "message": "验证码错误",
            }
            return jsonify(data)

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
                "message": "注册成功",
                "token": token,
                "User_Name": username,
            }

            # 从数据库中删除验证码
            db.session.delete(captcha_model)
            db.session.commit()

        return jsonify(data)
    else:
        data = {
            "code": 400,
            "message": form.errors,
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


# @bp.route("/mail/test")
# def mail_test():
#     messages = Message(subject="mail test", recipients=["jerrycaocao@126.com"], body="mail test")
#     mail.send(messages)
#     return "mail send succeed"


@bp.route("/captcha/email", methods=["POST"])
def get_email_captcha():
    mail_list = request.get_json()
    email = mail_list["User_Email"]
    source = string.digits * 4
    captcha = random.sample(source, 6)
    captcha = "".join(captcha)
    messages = Message(subject="注册验证码", recipients=[email], body=f"您的验证码是:{captcha}")
    mail.send(messages)

    email_captcha = EmailCaptchaModel(email=email, captcha=captcha)
    db.session.add(email_captcha)
    db.session.commit()

    # print(captcha)
    data = {
        "code": 200,
        "message": "邮件发送成功"
        # "User_Captcha": captcha,
    }
    return jsonify(data)

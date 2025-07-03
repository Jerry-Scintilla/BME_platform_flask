from flask import Blueprint, render_template, request, redirect

from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
# import app

from .forms import RegisterForm, LoginForm
from models import UserModel
from exts import db, mail, redis_client
from flask import jsonify
from flask_jwt_extended import JWTManager, create_access_token, jwt_required, get_jwt_identity

from flask_mail import Message
import string
import random

bp = Blueprint("auth", __name__, url_prefix="/auth")

# 导入api文档模块
from flasgger import swag_from


# 注册端口
@bp.route("/register", methods=["POST"])
@swag_from('../apidocs/user/register.yaml')
def register():
    form = RegisterForm()
    if form.validate():
        email = form.User_Email.data
        password = form.User_Password.data
        username = form.User_Name.data
        captcha = form.User_Captcha.data
        user = UserModel.query.filter_by(email=email).first()
        # captcha_model = EmailCaptchaModel.query.filter_by(captcha=captcha).first()

        # 从Redis中获取验证码
        redis_captcha = redis_client.get(f"captcha:{email}")

        if redis_captcha:
            redis_captcha = redis_captcha.decode('utf-8')  # 将bytes解码为字符串

        if not redis_captcha or redis_captcha != captcha:
            print(redis_captcha)
            data = {
                "code": 400,
                "message": "验证码错误",
            }
            return jsonify(data), 400

        if user:
            data = {
                "code": 401,
                "message": "邮箱已存在",
            }
            return jsonify(data), 401

        else:
            user = UserModel(email=email, password=password, username=username, study_stage="未分流")
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
            # captcha_list = EmailCaptchaModel.query.filter_by(email=email).delete()
            # db.session.commit()
            # 从Redis中删除验证码
            redis_client.delete(f"captcha:{email}")

        return jsonify(data),200
    else:
        data = {
            "code": 402,
            "message": form.errors,
        }
        return jsonify(data), 402


# 登录端口
@bp.route("/login", methods=["POST"])
@swag_from('../apidocs/user/login.yaml')
def login():
    form = LoginForm()
    if form.validate():
        email = form.User_Email.data
        password = form.User_Password.data
        user = UserModel.query.filter_by(email=email).first()
        try:
            User_Email = user.email
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


            if user.password == password:
                code = 200
                msg = "登录成功"
                token = create_access_token(identity=email)
                User_Name = user.username
                data = {
                    "code": code,
                    "message": msg,
                    "token": token,
                    "User_Name": User_Name,
                    "User_Email": User_Email,
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

            else:
                code = 402
                msg = "密码错误"
                token = "Null"
                User_Name = "Null"
                data = {
                    "code": code,
                    "message": msg,
                    "token": token,
                    "User_Name": User_Name,
                    "User_Email": User_Email,
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
                return jsonify(data),402



            # data = {
            #     "code": code,
            #     "message": msg,
            #     "token": token,
            #     "User_Name": User_Name,
            #     "User_Email": user.email,
            #     "User_Medal": user.medal,
            #     "User_Stage": user.study_stage,
            #     "User_Mode": user.mode,
            #     "join_time": user.join_time.strftime("%Y-%m-%d %H:%M:%S"),
            #     "User_Id": user.id,
            #     "Student_Id": user.student_id,
            #     "Introduction": user.introduction,
            #     "User_Sex": user.sex,
            #     "Institute": user.institute,
            #     "Major": user.major,
            #     "Github_Id": user.github_id,
            #     "Skill_Tags": user.skill_tags,
            # }

        except:
            return jsonify({
                "code": 400,
                "message": "用户不存在，请检查邮箱输入是否正确",
                "token": "Null",
                "User_Name": "Null",
            }), 400

    else:
        data = {
            "code": 403,
            "message": form.errors,
        }
        return jsonify(data), 403


@bp.route("/admin_login", methods=["POST"])
@swag_from('../apidocs/user/admin_login.yaml')
def admin_login():
    form = LoginForm()
    if form.validate():
        email = form.User_Email.data
        password = form.User_Password.data
        admin = UserModel.query.filter_by(email=email).first()
        # print(email, password)
        # print(admin)
        # if admin.is_(None):
        #     code = 400
        #     msg = "用户不存在，请检查邮箱输入是否正确"
        #     token = "Null"
        #     User_Name = "Null"
        # # print(mode)
        try:
            if admin.user_mode != 'admin':
                return jsonify({
                    "code": 401,
                    'message': "用户权限不够"
                }), 401
            if admin.password != password:
                return jsonify({
                    "code": 402,
                    'msg':"密码错误",
                    'token' : "Null",
                    'User_Name' : "Null"
                }),402

            else:
                return jsonify({
                'code' : 200,
                'msg' : "登录成功",
                'token' : create_access_token(identity=email),
                'User_Name' : admin.username
                }),200


        except:
            return jsonify({
                "code": 400,
                "message": "用户不存在，请检查邮箱输入是否正确",
                "token": "Null",
                "User_Name": "Null",
            }), 400

    else:
        data = {
            "code": 403,
            "message": form.errors,
        }
        return jsonify(data),403



# @bp.route("/mail/test")
# def mail_test():
#     messages = Message(subject="mail test", recipients=["jerrycaocao@126.com"], body="mail test")
#     mail.send(messages)
#     return "mail send succeed"


from exts import limiter


# 邮件验证码获取端口
@bp.route("/captcha/email", methods=["POST"])
@limiter.limit("1/minute")
@swag_from('../apidocs/user/get_email_captcha.yaml')
def get_email_captcha():
    mail_list = request.get_json()
    email = mail_list["User_Email"]
    source = string.digits * 4
    captcha = random.sample(source, 6)
    captcha = "".join(captcha)
    messages = Message(subject="BME卓越工程师在线教育平台", recipients=[email], body=f"您的验证码是:{captcha}")
    mail.send(messages)

    # email_captcha = EmailCaptchaModel(email=email, captcha=captcha)
    # db.session.add(email_captcha)
    # db.session.commit()

    redis_client.setex(f"captcha:{email}", 300, captcha)

    # print(captcha)
    data = {
        "code": 200,
        "message": "邮件发送成功"
        # "User_Captcha": captcha,
    }
    return jsonify(data)


@bp.route("/find_password", methods=["POST"])
@swag_from('../apidocs/user/find_password.yaml')
def find_password():
    data = request.get_json()
    email = data['User_Email']
    password = data['Password']
    captcha = data['Captcha']
    user = UserModel.query.filter_by(email=email).first()
    if user is None:
        return jsonify({
            "code": 400,
            "message": "用户不存在"
        }), 400

    # 从Redis中获取验证码
    redis_captcha = redis_client.get(f"captcha:{email}")
    if redis_captcha:
        redis_captcha = redis_captcha.decode('utf-8')  # 将bytes解码为字符串

    if not redis_captcha:
        return jsonify({
            "code": 401,
            "message": "验证码不存在"
        }), 401

    if redis_captcha == captcha:
        user.password = password
        # 从Redis中删除验证码
        redis_client.delete(f"captcha:{email}")
        db.session.commit()
        return jsonify({
            "code": 200,
            "message": "密码修改成功"
        })

    else:
        return jsonify({
            "code": 402,
            "message": "验证码错误"
        }), 402
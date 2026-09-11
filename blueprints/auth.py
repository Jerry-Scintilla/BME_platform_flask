from flask import Blueprint, render_template, request, redirect

from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
# import app

from .forms import RegisterForm, LoginForm
from models import UserModel, UserPermissionModel
from exts import db, mail, redis_client
from flask import jsonify
from flask_jwt_extended import JWTManager, create_access_token, jwt_required, get_jwt_identity

from flask_mail import Message
import string
import random

from models import AuditLog
from . import check_permission, get_user_permissions

bp = Blueprint("auth", __name__, url_prefix="/auth")

# 导入api文档模块
from flasgger import swag_from

# 导入审计装饰器
from . import audit_log


# 注册端口
@bp.route("/register", methods=["POST"])
@swag_from('../apidocs/user/register.yaml')
@audit_log(operation="用户注册", is_login=True)
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
            user = UserModel(email=email, username=username, study_stage="未分流")
            user.set_password(password)
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
@audit_log(operation="用户登录", is_login=True)
def login():
    form = LoginForm()
    if form.validate():
        email = form.User_Email.data
        password = form.User_Password.data
        user = UserModel.query.filter_by(email=email).first()
        # 封禁拦截（2026-09-11 用户管理）：存量 token 由 app.before_request 统一拦
        if user and (user.status or 'active') == 'banned':
            return jsonify({"code": 403, "message": "账号已被封禁，请联系管理员"}), 403
        try:
            User_Email = user.email
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


            if user.check_password(password):
                # 历史遗留的明文(MD5)密码，登录成功后自动升级为加盐哈希
                if not user.password_is_hashed:
                    user.set_password(password)
                    db.session.commit()
                code = 200
                msg = "登录成功"
                token = create_access_token(identity=email)
                User_Name = user.username
                data = {
                    "code": code,
                    "message": msg,
                    "token": token,
                    "User_Name": User_Name,
                    "role": user.role,
                    "role_rank": user.role_rank,
                    "level": user.level,
                    "permissions": get_user_permissions(user.id),
                    "User_Email": User_Email,
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
@audit_log(operation="管理员登录", is_login=True)
def admin_login():
    form = LoginForm()
    if form.validate():
        email = form.User_Email.data
        password = form.User_Password.data
        admin = UserModel.query.filter_by(email=email).first()

        # 先检查用户是否存在
        if not admin:
            return jsonify({
                "code": 400,
                "message": "用户不存在，请检查邮箱输入是否正确",
                "token": "Null",
                "User_Name": "Null",
            }), 400

        try:
            user_permission = UserPermissionModel.query.filter_by(
                user_id=admin.id,
            ).first()

            if not user_permission and not admin.is_staff():
                return jsonify({
                    "code": 401,
                    'message': "用户权限不够"
                }), 401

            if not admin.check_password(password):
                return jsonify({
                    "code": 402,
                    'msg':"密码错误",
                    'token' : "Null",
                    'User_Name' : "Null"
                }),402

            else:
                # 历史遗留的明文(MD5)密码，登录成功后自动升级为加盐哈希
                if not admin.password_is_hashed:
                    admin.set_password(password)
                    db.session.commit()
                return jsonify({
                'code' : 200,
                'msg' : "登录成功",
                'token' : create_access_token(identity=email),
                'User_Name' : admin.username,
                'role' : admin.role,
                'role_rank' : admin.role_rank,
                'permissions' : get_user_permissions(admin.id),
                }),200


        except Exception as e:
            print(f"Login error: {e}")
            return jsonify({
                "code": 400,
                "message": "登录失败，请稍后重试",
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
@audit_log(operation="获取邮件验证码", is_login=True)
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
@audit_log(operation="找回密码", is_login=True)
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
        user.set_password(password)
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

# 管理员获取审计日志记录
@bp.route("/audit_records", methods=["GET"])
@jwt_required()
@check_permission('system_management')
@swag_from('../apidocs/user/audit_records.yaml')
def get_admin_audit_logs():
    # 获取分页参数
    page = request.args.get('page', 1, type=int)
    per_page = min(request.args.get('per_page', 10, type=int), 100)
    
    # 获取筛选参数
    user_id = request.args.get('user_id', type=int)
    operation = request.args.get('operation', type=str)
    
    # 查询审计日志
    query = AuditLog.query
    
    # 应用筛选条件
    if user_id:
        query = query.filter_by(user_id=user_id)
    
    if operation:
        query = query.filter(AuditLog.operation.contains(operation))
    
    logs_pagination = query.order_by(AuditLog.timestamp.desc()).paginate(
        page=page, per_page=per_page, error_out=False)
    
    # 格式化返回数据
    logs_data = []
    for log in logs_pagination.items:
        user = UserModel.query.get(log.user_id)
        logs_data.append({
            "id": log.id,
            "user_id": log.user_id,
            "username": log.username,
            "ip_address": log.ip_address,
            "user_agent": log.user_agent,
            "operation": log.operation,
            "operation_url": log.operation_url,
            "operation_data": log.operation_data,
            "result": log.result,
            "timestamp": log.timestamp.isoformat() if log.timestamp else None
        })
    
    return jsonify({
        "code": 200,
        "message": "查询成功",
        "data": {
            "logs": logs_data,
            "pagination": {
                "page": logs_pagination.page,
                "per_page": logs_pagination.per_page,
                "total": logs_pagination.total,
                "pages": logs_pagination.pages
            }
        }
    })

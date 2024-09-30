import wtforms
from wtforms.validators import Email, length, EqualTo, input_required
from models import UserModel, EmailCaptchaModel
from flask import request
from exts import db

# 注册表单验证
class RegisterForm(wtforms.Form):
    def __init__(self):
        if "application/json" in request.headers.get("Content-Type"):
            data = request.get_json(silent=True)
            args = request.args.to_dict()
            super(RegisterForm, self).__init__(data=data, **args)
        else:
            # 获取 “application/x-www-form-urlencoded” 或者 “multipart/form-data” 请求
            data = request.form.to_dict()
            args = request.args.to_dict()
            super(RegisterForm, self).__init__(data=data, **args)

    User_Name = wtforms.StringField(validators=[length(min=4, max=20, message='Invalid username')])
    User_Password = wtforms.StringField(validators=[length(min=6, max=100, message='Invalid password')])
    User_Email = wtforms.StringField(validators=[Email(message='邮箱格式错误')])
    User_Captcha = wtforms.StringField(validators=[length(min=6, max=6, message='验证码为6位')])

    # def validate_email(self, field):
    #     User_Email = field.data
    #     user = UserModel.query.filter_by(email=User_Email).first()
    #     if user:
    #         raise wtforms.ValidationError(message='Email already registered')

    # def validate_captcha(self, field):
    #     captcha = field.data
    #     email = self.User_Email.data
    #     captcha_model = EmailCaptchaModel.query.filter_by(email=email, captcha=captcha).first()
    #     if not captcha_model:
    #         raise wtforms.ValidationError(message="验证码错误")
    #     else:
    #         db.session.delete(captcha_model)
    #         db.session.commit()

# 登录表单验证
class LoginForm(wtforms.Form):
    def __init__(self):
        if "application/json" in request.headers.get("Content-Type"):
            data = request.get_json(silent=True)
            args = request.args.to_dict()
            super(LoginForm, self).__init__(data=data, **args)
        else:
            # 获取 “application/x-www-form-urlencoded” 或者 “multipart/form-data” 请求
            data = request.form.to_dict()
            args = request.args.to_dict()
            super(LoginForm, self).__init__(data=data, **args)

    User_Password = wtforms.StringField(validators=[length(min=6, max=100, message='Invalid password')])
    User_Email = wtforms.StringField(validators=[Email(message='Invalid Email')])

class ArticleForm(wtforms.Form):
    def __init__(self):
        if "application/json" in request.headers.get("Content-Type"):
            data = request.get_json(silent=True)
            args = request.args.to_dict()
            super(ArticleForm, self).__init__(data=data, **args)
        else:
            # 获取 “application/x-www-form-urlencoded” 或者 “multipart/form-data” 请求
            data = request.form.to_dict()
            args = request.args.to_dict()
            super(ArticleForm, self).__init__(data=data, **args)

    Article_Title = wtforms.StringField(validators=[length(min=1, max=50, message='Invalid title')])
    Article_Introduction = wtforms.StringField(validators=[length(min=1, max=300, message='Invalid introduction')])
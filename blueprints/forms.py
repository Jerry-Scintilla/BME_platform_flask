import wtforms
from wtforms.validators import Email, length, EqualTo ,input_required
from models import UserModel
from flask import request

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
    User_Email = wtforms.StringField(validators=[Email(message='Invalid Email')])

    # def validate_email(self, field):
    #     User_Email = field.data
    #     user = UserModel.query.filter_by(email=User_Email).first()
    #     if user:
    #         raise wtforms.ValidationError(message='Email already registered')

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

    User_Password = wtforms.StringField(validators=[length(min=6, max=20, message='Invalid password')])
    User_Email = wtforms.StringField(validators=[Email(message='Invalid Email')])
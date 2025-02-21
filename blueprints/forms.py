import wtforms
from flask_wtf.file import FileAllowed, FileSize, FileField
from wtforms.validators import Email, length, EqualTo, input_required, NumberRange, Optional
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

    Article_Title = wtforms.StringField(validators=[length(min=1, max=50, message='标题格式不对')])
    Article_Introduction = wtforms.StringField(validators=[length(min=1, max=300, message='简介格式不对')])
    Html = wtforms.StringField('Html')


class AvatarForm(wtforms.Form):
    avatar = FileField(validators=[FileAllowed(['jpg', 'jpeg', 'png']), FileSize(5 * 1024 * 1024)])


class CourseForm(wtforms.Form):
    def __init__(self):
        if "application/json" in request.headers.get("Content-Type"):
            data = request.get_json(silent=True)
            args = request.args.to_dict()
            super(CourseForm, self).__init__(data=data, **args)
        else:
            # 获取 “application/x-www-form-urlencoded” 或者 “multipart/form-data” 请求
            data = request.form.to_dict()
            args = request.args.to_dict()
            super(CourseForm, self).__init__(data=data, **args)

    Course_title = wtforms.StringField('Course_title',validators=[length(min=1, max=50, message='标题格式不对')])
    Course_Introduction = wtforms.StringField('Course_Introduction',validators=[length(min=1, max=300, message='简介格式不对')])
    Course_Chapters = wtforms.IntegerField('Course_Chapters',validators=[Optional(),NumberRange(min=1, max=300, message='章节数需要在1-300之间')])
    Course_Tags = wtforms.StringField('Course_Tags',validators=[Optional(),length(min=1, max=100, message='标签格式不对')])
    Course_Id = wtforms.IntegerField('Course_Id',validators=[Optional(),input_required()])
    # Cover = FileField('Cover',validators=[FileAllowed(['jpg', 'jpeg', 'png']), FileSize(5 * 1024 * 1024)])


class UserInfoForm(wtforms.Form):
    def __init__(self):
        if "application/json" in request.headers.get("Content-Type"):
            data = request.get_json(silent=True)
            args = request.args.to_dict()
            super(UserInfoForm, self).__init__(data=data, **args)
        else:
            # 获取 “application/x-www-form-urlencoded” 或者 “multipart/form-data” 请求
            data = request.form.to_dict()
            args = request.args.to_dict()
            super(UserInfoForm, self).__init__(data=data, **args)

    User_Name = wtforms.StringField('User_Name',validators=[Optional(),length(min=1, max=20, message='用户名格式不对')])
    Student_Id = wtforms.IntegerField('Student_Id',validators=[Optional(),NumberRange(min=1, max=99999999, message='学号格式不对')])
    Sex = wtforms.StringField('Sex',validators=[Optional(),length(min=1, max=10, message='性别格式不对')])
    Introduction = wtforms.StringField('Introduction',validators=[Optional(),length(min=1, max=300, message='简介超过300字')])
    Institute = wtforms.StringField('Institute',validators=[Optional(),length(min=1, max=100, message='学院格式不对')])
    Major = wtforms.StringField('Major',validators=[Optional(),length(min=1, max=100, message='专业格式不对')])
    Github_Id = wtforms.StringField('Github_Id',validators=[Optional(),length(min=1, max=100, message='Github_id格式不对')])
    Skill_Tags = wtforms.StringField('Skill_Tags',validators=[Optional(),length(min=1, max=100, message='技能标签格式不对')])


class ChapterForm(wtforms.Form):
    def __init__(self):
        if "application/json" in request.headers.get("Content-Type"):
            data = request.get_json(silent=True)
            args = request.args.to_dict()
            super(ChapterForm, self).__init__(data=data, **args)
        else:
            # 获取 “application/x-www-form-urlencoded” 或者 “multipart/form-data” 请求
            data = request.form.to_dict()
            args = request.args.to_dict()
            super(ChapterForm, self).__init__(data=data, **args)

    Course_Id = wtforms.IntegerField('Course_Id',validators=[NumberRange(min=1, max=99999999, message='课程id格式不对')])
    Chapter_Name = wtforms.StringField('Chapter_Name')


class MedalForm(wtforms.Form):
    def __init__(self):
        if "application/json" in request.headers.get("Content-Type"):
            data = request.get_json(silent=True)
            args = request.args.to_dict()
            super(MedalForm, self).__init__(data=data, **args)
        else:
            # 获取 “application/x-www-form-urlencoded” 或者 “multipart/form-data” 请求
            data = request.form.to_dict()
            args = request.args.to_dict()
            super(MedalForm, self).__init__(data=data, **args)

    Medal_Name = wtforms.StringField('Medal_Name',validators=[length(min=1, max=100, message='勋章名称格式不对')])
    Medal_Description = wtforms.StringField('Medal_Name',validators=[length(min=1, max=100, message='勋章描述格式不对')])
    Medal_Tag = wtforms.StringField('Medal_Tag',validators=[length(min=1, max=100, message='勋章标签格式不对')])





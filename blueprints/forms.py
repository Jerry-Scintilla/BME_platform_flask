import wtforms
from flask_wtf.file import FileAllowed, FileSize, FileField
from wtforms.validators import Email, length, EqualTo, input_required, NumberRange, Optional, DataRequired, ValidationError
from models import UserModel
from flask import request
from exts import db
import datetime
import re

# 自定义的日期时间字段，支持多种格式的输入
class FlexibleDateTimeField(wtforms.Field):
    """
    自定义的日期时间字段，支持多种格式的输入，包括：
    - 完整的日期时间格式：YYYY-MM-DD HH:MM:SS
    - 只有日期部分的格式：YYYY-MM-DD（自动设置时间为23:59:59）
    """
    def _value(self):
        if self.data:
            return self.data.strftime('%Y-%m-%d %H:%M:%S')
        return ''

    def process_formdata(self, valuelist):
        if not valuelist or not valuelist[0]:
            self.data = None
            return
        
        date_str = valuelist[0]
        try:
            # 尝试解析完整的日期时间格式
            self.data = datetime.datetime.strptime(date_str, '%Y-%m-%d %H:%M:%S')
        except ValueError:
            try:
                # 尝试解析只有日期部分的格式，设置时间为23:59:59
                date_obj = datetime.datetime.strptime(date_str, '%Y-%m-%d')
                self.data = datetime.datetime(date_obj.year, date_obj.month, date_obj.day, 23, 59, 59)
            except ValueError as e:
                self.data = None
                raise ValueError('Invalid date format. Use YYYY-MM-DD or YYYY-MM-DD HH:MM:SS') from e

# 注册表单验证
class RegisterForm(wtforms.Form):
    def __init__(self):
        if "application/json" in request.headers.get("Content-Type"):
            data = request.get_json(silent=True)
            args = request.args.to_dict()
            super(RegisterForm, self).__init__(data=data, **args)
        else:
            # 获取 "application/x-www-form-urlencoded" 或者 "multipart/form-data" 请求
            data = request.form.to_dict()
            args = request.args.to_dict()
            super(RegisterForm, self).__init__(data=data, **args)

    User_Name = wtforms.StringField('User_Name')
    User_Password = wtforms.StringField(validators=[length(min=6, max=100, message='Invalid password')])
    User_Email = wtforms.StringField(validators=[Email(message='邮箱格式错误')])
    User_Captcha = wtforms.StringField(validators=[length(min=6, max=6, message='验证码为6位')])

# 登录表单验证
class LoginForm(wtforms.Form):
    def __init__(self):
        if "application/json" in request.headers.get("Content-Type"):
            data = request.get_json(silent=True)
            args = request.args.to_dict()
            super(LoginForm, self).__init__(data=data, **args)
        else:
            # 获取 "application/x-www-form-urlencoded" 或者 "multipart/form-data" 请求
            data = request.form.to_dict()
            args = request.args.to_dict()
            super(LoginForm, self).__init__(data=data, **args)

    User_Password = wtforms.StringField(validators=[length(min=8, max=100, message='Invalid password')])
    User_Email = wtforms.StringField(validators=[Email(message='Invalid Email')])


class ArticleForm(wtforms.Form):
    def __init__(self):
        if "application/json" in request.headers.get("Content-Type"):
            data = request.get_json(silent=True)
            args = request.args.to_dict()
            super(ArticleForm, self).__init__(data=data, **args)
        else:
            # 获取 "application/x-www-form-urlencoded" 或者 "multipart/form-data" 请求
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
            # 获取 "application/x-www-form-urlencoded" 或者 "multipart/form-data" 请求
            data = request.form.to_dict()
            args = request.args.to_dict()
            super(CourseForm, self).__init__(data=data, **args)

    Course_title = wtforms.StringField('Course_title',validators=[length(min=1, max=50, message='标题格式不对')])
    Course_Introduction = wtforms.StringField('Course_Introduction',validators=[length(min=1, max=300, message='简介格式不对')])
    Course_Chapters = wtforms.IntegerField('Course_Chapters',validators=[Optional(),NumberRange(min=1, max=300, message='章节数需要在1-300之间')])
    Course_Tags = wtforms.StringField('Course_Tags',validators=[Optional(),length(min=1, max=100, message='标签格式不对')])
    Course_Id = wtforms.IntegerField('Course_Id',validators=[Optional(),input_required()])
    # 添加新字段
    Course_Class_Hour = wtforms.IntegerField('Course_Class_Hour',validators=[Optional(),NumberRange(min=1, max=1000, message='课时数格式不对')])
    Course_Difficulty = wtforms.IntegerField('Course_Difficulty',validators=[Optional(),NumberRange(min=1, max=5, message='难度需要在1-5之间')])
    Course_Other_Tags = wtforms.StringField('Course_Other_Tags',validators=[Optional(),length(min=1, max=500, message='其他标签格式不对')])
    # Cover = FileField('Cover',validators=[FileAllowed(['jpg', 'jpeg', 'png']), FileSize(5 * 1024 * 1024)])


class UserInfoForm(wtforms.Form):
    def __init__(self):
        if "application/json" in request.headers.get("Content-Type"):
            data = request.get_json(silent=True)
            args = request.args.to_dict()
            super(UserInfoForm, self).__init__(data=data, **args)
        else:
            # 获取 "application/x-www-form-urlencoded" 或者 "multipart/form-data" 请求
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
    College = wtforms.StringField('College',validators=[Optional(), length(min=1, max=100, message='院校格式不对')])


class ChapterForm(wtforms.Form):
    def __init__(self):
        if "application/json" in request.headers.get("Content-Type"):
            data = request.get_json(silent=True)
            args = request.args.to_dict()
            super(ChapterForm, self).__init__(data=data, **args)
        else:
            # 获取 "application/x-www-form-urlencoded" 或者 "multipart/form-data" 请求
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
            # 获取 "application/x-www-form-urlencoded" 或者 "multipart/form-data" 请求
            data = request.form.to_dict()
            args = request.args.to_dict()
            super(MedalForm, self).__init__(data=data, **args)

    Medal_Name = wtforms.StringField('Medal_Name',validators=[length(min=1, max=100, message='勋章名称格式不对')])
    Medal_Name_CN = wtforms.StringField('Medal_Name_CN',validators=[length(min=1, max=100, message='勋章中文名格式不对')])
    Medal_Tag = wtforms.StringField('Medal_Tag',validators=[length(min=1, max=100, message='勋章标签格式不对')])


class LearningProgressForm(wtforms.Form):
    def __init__(self):
        if "application/json" in request.headers.get("Content-Type"):
            data = request.get_json(silent=True)
            args = request.args.to_dict()
            super(LearningProgressForm, self).__init__(data=data, **args)
        else:
            # 获取 "application/x-www-form-urlencoded" 或者 "multipart/form-data" 请求
            data = request.form.to_dict()
            args = request.args.to_dict()
            super(LearningProgressForm, self).__init__(data=data, **args)

    User_Id = wtforms.IntegerField('User_Id', validators=[NumberRange(min=1, max=99999999, message='用户id格式不对')])
    Course_Id = wtforms.IntegerField('Course_Id', validators=[NumberRange(min=1, max=99999999, message='课程编号格式不对')])
    Progress = wtforms.IntegerField('Progress', validators=[NumberRange(min=0, max=99999999, message='进度格式不对')])

class HomeCoverForm(wtforms.Form):
    HomeCover = FileField('HomeCover', validators=[FileAllowed(['jpg', 'jpeg', 'png']), FileSize(5 * 1024 * 1024), DataRequired()])

class LeaveForm(wtforms.Form):
    def __init__(self):
        if "application/json" in request.headers.get("Content-Type"):
            data = request.get_json(silent=True)
            args = request.args.to_dict()
            super(LeaveForm, self).__init__(data=data, **args)
        else:
            # 获取 "application/x-www-form-urlencoded" 或者 "multipart/form-data" 请求
            data = request.form.to_dict()
            args = request.args.to_dict()
            super(LeaveForm, self).__init__(data=data, **args)
    
    Group_Id = wtforms.IntegerField('Group_Id', validators=[DataRequired(message='小组ID不能为空')])
    Title = wtforms.StringField('Title', validators=[DataRequired(message='请假标题不能为空'), length(min=1, max=100, message='标题长度需在1-100之间')])
    Content = wtforms.StringField('Content', validators=[Optional(), length(max=500, message='请假内容不能超过500字')])
    Start_Time = FlexibleDateTimeField('Start_Time', validators=[DataRequired(message='开始时间不能为空')])
    End_Time = FlexibleDateTimeField('End_Time', validators=[DataRequired(message='结束时间不能为空')])

class TaskForm(wtforms.Form):
    def __init__(self):
        if "application/json" in request.headers.get("Content-Type"):
            data = request.get_json(silent=True)
            args = request.args.to_dict()
            super(TaskForm, self).__init__(data=data, **args)
        else:
            # 获取 "application/x-www-form-urlencoded" 或者 "multipart/form-data" 请求
            data = request.form.to_dict()
            args = request.args.to_dict()
            super(TaskForm, self).__init__(data=data, **args)
    
    Id = wtforms.IntegerField('Id', validators=[Optional()])  # 非必须，用于更新操作
    Group_Id = wtforms.IntegerField('Group_Id', validators=[DataRequired(message='小组ID不能为空')])
    Title = wtforms.StringField('Title', validators=[DataRequired(message='任务标题不能为空'), length(min=1, max=100, message='标题长度需在1-100之间')])
    Content = wtforms.StringField('Content', validators=[Optional(), length(max=500, message='任务内容不能超过500字')])
    End_Time = FlexibleDateTimeField('End_Time', validators=[DataRequired(message='截止时间不能为空')])
    Priority = wtforms.IntegerField('Priority', validators=[Optional(), NumberRange(min=1, max=5, message='优先级必须在1-5之间')])

class NoticeForm(wtforms.Form):
    def __init__(self):
        if "application/json" in request.headers.get("Content-Type"):
            data = request.get_json(silent=True)
            args = request.args.to_dict()
            super(NoticeForm, self).__init__(data=data, **args)
        else:
            # 获取 "application/x-www-form-urlencoded" 或者 "multipart/form-data" 请求
            data = request.form.to_dict()
            args = request.args.to_dict()
            super(NoticeForm, self).__init__(data=data, **args)
    
    Id = wtforms.IntegerField('Id', validators=[Optional()])  # 非必须，用于更新操作
    Group_Id = wtforms.IntegerField('Group_Id', validators=[DataRequired(message='小组ID不能为空')])
    Title = wtforms.StringField('Title', validators=[DataRequired(message='通知标题不能为空'), length(min=1, max=100, message='标题长度需在1-100之间')])
    Content = wtforms.StringField('Content', validators=[Optional(), length(max=500, message='通知内容不能超过500字')])
    Range = wtforms.StringField('Range', validators=[Optional()])




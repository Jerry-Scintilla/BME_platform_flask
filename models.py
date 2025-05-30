from datetime import datetime

from pygments.lexer import default

from exts import db


class UserModel(db.Model):
    __tablename__ = 'user'
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    username = db.Column(db.String(100), nullable=False)
    password = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(100), nullable=False, unique=True)
    join_time = db.Column(db.DateTime, default=datetime.now)
    # 签出测试
    # 添加勋章，学习阶段
    medal = db.Column(db.Integer, server_default='0')
    study_stage = db.Column(db.Text)
    user_mode = db.Column(db.String(20), default='user')
    avatar_url = db.Column(db.String(100))
    # 添加详细个人信息
    student_id = db.Column(db.Integer)
    introduction = db.Column(db.Text)
    sex = db.Column(db.String(10))
    institute = db.Column(db.String(100))
    major = db.Column(db.String(100))
    github_id = db.Column(db.String(100))
    skill_tags = db.Column(db.String(100))

    # down_code = db.Column(db.String(100))
    # down_id = db.Column(db.Integer)


# 已弃用，改用redis存储
# class EmailCaptchaModel(db.Model):
#     __tablename__ = 'email_captcha'
#     id = db.Column(db.Integer, primary_key=True, autoincrement=True)
#     email = db.Column(db.String(100), nullable=False)
#     captcha = db.Column(db.String(100), nullable=False)


class ArticleModel(db.Model):
    __tablename__ = 'article'
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    title = db.Column(db.String(100), nullable=False)
    introduction = db.Column(db.Text, nullable=False)
    publish_time = db.Column(db.DateTime, default=datetime.now)
    url = db.Column(db.String(100))
    # 外键
    author_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    author = db.relationship(UserModel, backref="articles")


class CourseModel(db.Model):
    __tablename__ = 'course'
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    title = db.Column(db.String(100), nullable=False)
    introduction = db.Column(db.Text, nullable=False)
    chapters = db.Column(db.Integer, nullable=False)
    cover = db.Column(db.String(100))
    url = db.Column(db.String(100))
    tags = db.Column(db.String(100))
    publish_time = db.Column(db.DateTime, default=datetime.now)


class LearningProgressModel(db.Model):
    __tablename__ = 'learning_progress'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    course_id = db.Column(db.Integer, db.ForeignKey('course.id'), nullable=False)

    progress = db.Column(db.Integer, nullable=False)  # 假设进度是一个整数

    user = db.relationship('UserModel', backref=db.backref('learning_progress', lazy=True))
    course = db.relationship('CourseModel', backref=db.backref('learning_progress', lazy=True))


class Chapter(db.Model):
    __tablename__ = 'chapter'
    id = db.Column(db.Integer, primary_key=True)
    course_id = db.Column(db.Integer, db.ForeignKey('course.id'), nullable=False)
    name = db.Column(db.Text, nullable=False)
    url = db.Column(db.String(100))
    order = db.Column(db.Integer, nullable=False)  # 用于确定章节顺序
    priority = db.Column(db.Integer, nullable=False)  # 用于确定章节级别


class MedalModel(db.Model):
    __tablename__ = 'medal'
    id = db.Column(db.Integer, primary_key=True)
    medal_name = db.Column(db.String(100))
    description = db.Column(db.String(100))
    tags = db.Column(db.String(100))

class MedalUserModel(db.Model):
    __tablename__ = 'medal_user'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    medal_id = db.Column(db.Integer, db.ForeignKey('medal.id'), nullable=False)
    get_time = db.Column(db.DateTime, default=datetime.now)
    description = db.Column(db.String(100))

    user = db.relationship('UserModel', backref=db.backref('medal_user', lazy=True))
    medal = db.relationship('MedalModel', backref=db.backref('medal_user', lazy=True))


class GroupModel(db.Model):
    __tablename__ = 'group'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    type = db.Column(db.String(10), nullable=False)
    teacher_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    student_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    group_id = db.Column(db.Integer)


class CheckRecord(db.Model):
    __tablename__ = 'check_record'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    check_in = db.Column(db.DateTime)
    check_out = db.Column(db.DateTime)
    duration = db.Column(db.Float)
    date = db.Column(db.Date, index=True)





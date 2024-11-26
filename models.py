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


class EmailCaptchaModel(db.Model):
    __tablename__ = 'email_captcha'
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    email = db.Column(db.String(100), nullable=False)
    captcha = db.Column(db.String(100), nullable=False)


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

    publish_time = db.Column(db.DateTime, default=datetime.now)


class LearningProgress(db.Model):
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

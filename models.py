from datetime import datetime

from pygments.lexer import default
from sqlalchemy import and_
from sqlalchemy.orm import foreign, remote

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
    college = db.Column(db.String(50))

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
    class_hour = db.Column(db.Integer, nullable=True)
    difficulty = db.Column(db.Integer, nullable=True)
    other_tags = db.Column(db.String(100))



class LearningProgressModel(db.Model):
    __tablename__ = 'learning_progress'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    course_id = db.Column(db.Integer, db.ForeignKey('course.id'), nullable=False)

    progress = db.Column(db.Integer, nullable=False)  # 假设进度是一个整数

    user = db.relationship('UserModel', backref=db.backref('learning_progress', lazy=True))
    course = db.relationship('CourseModel', backref=db.backref('learning_progress', lazy=True))

    def get_chapter_info(self):
        """
        获取当前进度对应的章节信息
        返回: (章数, 节数, 章名, 节名)
        """
        # 获取当前课程的所有章节，按order排序
        chapters = Chapter.query.filter_by(course_id=self.course_id).order_by(Chapter.order).all()
        
        # 获取当前进度对应的章节
        current_chapter = None
        for chapter in chapters:
            if chapter.order == self.progress:
                current_chapter = chapter
                break
        
        if not current_chapter:
            return None, None, None, None
        
        # 判断当前是章还是节
        if current_chapter.priority == 0:  # 当前是章
            # 计算是第几章（统计当前章节之前的priority=0的数量 + 1）
            chapter_num = sum(1 for ch in chapters if ch.priority == 0 and ch.order <= current_chapter.order)
            return chapter_num, 0, current_chapter.name, None
        else:  # 当前是节
            # 找到当前节所属的章
            parent_chapter = None
            for i in range(len(chapters)):
                if chapters[i].id == current_chapter.id:  # 找到当前节
                    # 向前查找最近的一个priority=0的章
                    for j in range(i-1, -1, -1):
                        if chapters[j].priority == 0:
                            parent_chapter = chapters[j]
                            break
                    break
            
            if not parent_chapter:
                return None, None, None, None
            
            # 计算是第几章
            chapter_num = sum(1 for ch in chapters if ch.priority == 0 and ch.order <= parent_chapter.order)
            
            # 计算是第几节（从章到当前节之间的priority=1的数量）
            section_num = sum(1 for ch in chapters 
                             if ch.priority == 1 
                             and parent_chapter.order < ch.order <= current_chapter.order)
            
            return chapter_num, section_num, parent_chapter.name, current_chapter.name


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
    course_id = db.Column(db.Integer, db.ForeignKey('course.id'), nullable=False)
    type = db.Column(db.String(10), nullable=False)
    teacher_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    student_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    group_id = db.Column(db.Integer)

    progress = db.relationship(
        'LearningProgressModel',
        primaryjoin=and_(
            foreign(student_id) == remote(LearningProgressModel.user_id),
            foreign(course_id) == remote(LearningProgressModel.course_id)
        ),
        uselist=False,  # 设置为False表示返回单个对象而非列表
        viewonly=True   # 设置为True表示这是只读关系，不会级联保存
    )
    course = db.relationship('CourseModel', backref=db.backref('groups', lazy=True))


class CheckRecord(db.Model):
    __tablename__ = 'check_record'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    check_in = db.Column(db.DateTime)
    check_out = db.Column(db.DateTime)
    duration = db.Column(db.Float)
    date = db.Column(db.Date, index=True)

class HomeCover(db.Model):
    __tablename__ = 'home_cover'
    id = db.Column(db.Integer, primary_key=True)
    url = db.Column(db.String(100))
    cover_id = db.Column(db.Integer, nullable=False)

class InformationModel(db.Model):
    __tablename__ = 'information'
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    # 公共字段
    group_id = db.Column(db.Integer, nullable=False)
    type = db.Column(db.Integer, nullable=False)  # 1: 请假信息, 2: 任务信息, 3: 通知信息, 4: 报错信息
    title = db.Column(db.String(100), nullable=False)
    content = db.Column(db.Text)
    create_time = db.Column(db.DateTime, default=datetime.now)
    
    # 请假信息特有字段
    start_time = db.Column(db.DateTime)
    end_time = db.Column(db.DateTime)  # 也用于任务信息的截止时间
    student_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    status = db.Column(db.Integer, default=0)  # 0: 未批准, 1: 已批准
    
    # 任务信息特有字段
    priority = db.Column(db.Integer)  # 1-5, 数字越小优先级越高
    
    # 通知信息特有字段
    range = db.Column(db.String(100)) # 如果对应多个用户id，就用逗号隔开，如果是小组全选，则为0

    # 报错信息特有字段
    resource = db.Column(db.String(100)) # 资源链接
    
    def get_info_by_type(self):
        """
        根据类型返回相应的信息
        """
        if self.type == 1:  # 请假信息
            return {
                'id': self.id,
                'title': self.title,
                'content': self.content,
                'start_time': self.start_time,
                'end_time': self.end_time,
                'student_id': self.student_id,
                'status': self.status,
                'create_time': self.create_time
            }
        elif self.type == 2:  # 任务信息
            return {
                'id': self.id,
                'title': self.title,
                'content': self.content,
                'end_time': self.end_time,
                'priority': self.priority,
                'create_time': self.create_time
            }
        elif self.type == 3:  # 通知信息
            return {
                'id': self.id,
                'title': self.title,
                'content': self.content,
                'range': self.range,
                'create_time': self.create_time
            }
        elif self.type == 4:  # 报错信息
            return {
                'id': self.id,
                'title': self.title,
                'content': self.content,
                'resource': self.resource,
                'student_id': self.student_id,
                'create_time': self.create_time
            }
        return None


class ArticleComment(db.Model):
    __tablename__ = 'article_comment'
    id = db.Column(db.Integer, primary_key=True)
    like_time = db.Column(db.DateTime)  # 点赞时间
    view_time = db.Column(db.DateTime)  # 浏览时间
    article_id = db.Column(db.Integer, db.ForeignKey('article.id'))
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))



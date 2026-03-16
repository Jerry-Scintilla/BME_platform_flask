from datetime import datetime

from pygments.lexer import default
from sqlalchemy import and_
from sqlalchemy.dialects.mysql import LONGTEXT
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
    chapters = db.Column(db.Integer, nullable=True)
    cover = db.Column(db.String(100))
    url = db.Column(db.String(100))
    tags = db.Column(db.String(100))
    publish_time = db.Column(db.DateTime, default=datetime.now)
    class_hour = db.Column(db.Integer, nullable=True)
    difficulty = db.Column(db.Integer, nullable=True)
    other_tags = db.Column(db.String(100))

    # 课程创建者，用于权限管理
    creator_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)



class LearningProgressModel(db.Model):
    """学习进度模型 - 每课时一条记录"""
    __tablename__ = 'learning_progress'

    # 状态常量
    STATUS_NOT_STARTED = 'not_started'
    STATUS_LEARNING = 'learning'
    STATUS_COMPLETED = 'completed'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    course_id = db.Column(db.Integer, db.ForeignKey('course.id'), nullable=False)
    lesson_id = db.Column(db.Integer, db.ForeignKey('lesson.id'), nullable=False)

    # 学习状态: not_started / learning / completed
    status = db.Column(db.String(20), default=STATUS_NOT_STARTED)

    # 学习时长（分钟）
    duration = db.Column(db.Integer, default=0)

    # 详情 - JSON格式，不同类型课时有不同字段
    detail = db.Column(db.JSON)

    # 时间戳
    start_time = db.Column(db.DateTime)
    completed_time = db.Column(db.DateTime)

    # 保留旧的 progress 字段用于兼容
    progress = db.Column(db.Integer, nullable=False, default=0)

    user = db.relationship('UserModel', backref=db.backref('learning_progress', lazy=True))
    course = db.relationship('CourseModel', backref=db.backref('learning_progress', lazy=True))
    lesson = db.relationship('LessonModel', backref=db.backref('learning_progress', lazy=True))

    def to_dict(self):
        """转换为字典"""
        return {
            'id': self.id,
            'user_id': self.user_id,
            'course_id': self.course_id,
            'lesson_id': self.lesson_id,
            'status': self.status,
            'duration': self.duration,
            'detail': self.detail,
            'start_time': self.start_time.strftime('%Y-%m-%d %H:%M:%S') if self.start_time else None,
            'completed_time': self.completed_time.strftime('%Y-%m-%d %H:%M:%S') if self.completed_time else None
        }

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


class UserCourseModel(db.Model):
    """用户选课表 - 将选课与小组解耦"""
    __tablename__ = 'user_course'

    # 状态常量
    STATUS_ACTIVE = 'active'      # 学习中
    STATUS_COMPLETED = 'completed'  # 已完成
    STATUS_DROPPED = 'dropped'    # 已退课

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    course_id = db.Column(db.Integer, db.ForeignKey('course.id'), nullable=False)

    # 选课时间
    enroll_time = db.Column(db.DateTime, default=datetime.now)

    # 状态: active / completed / dropped
    status = db.Column(db.String(20), default=STATUS_ACTIVE)

    # 关联关系
    user = db.relationship('UserModel', backref=db.backref('user_courses', lazy=True))
    course = db.relationship('CourseModel', backref=db.backref('user_courses', lazy=True))

    # 联合唯一约束：防止用户重复选同一门课
    __table_args__ = (db.UniqueConstraint('user_id', 'course_id'),)

    def to_dict(self):
        """转换为字典"""
        return {
            'id': self.id,
            'user_id': self.user_id,
            'course_id': self.course_id,
            'enroll_time': self.enroll_time.strftime('%Y-%m-%d %H:%M:%S') if self.enroll_time else None,
            'status': self.status
        }


class Chapter(db.Model):
    __tablename__ = 'chapter'
    id = db.Column(db.Integer, primary_key=True)
    course_id = db.Column(db.Integer, db.ForeignKey('course.id'), nullable=False)
    name = db.Column(db.Text, nullable=False)
    url = db.Column(db.String(100))
    order = db.Column(db.Integer, nullable=False, default=0)  # 排序字段
    level = db.Column(db.Integer, nullable=False, default=1)  # 层级深度：1=一级章节, 2=二级章节...
    parent_id = db.Column(db.Integer, db.ForeignKey('chapter.id'), nullable=True)  # 父章节ID，最高级为null

    # 自关联：子章节
    children = db.relationship('Chapter', backref=db.backref('parent', remote_side=[id]), cascade='all, delete-orphan')
    # 关联课时
    lessons = db.relationship('LessonModel', backref='chapter', lazy=True, cascade='all, delete-orphan')


class LessonModel(db.Model):
    """课时模型 - 课程的最小学习单元"""
    __tablename__ = 'lesson'

    # 课时类型常量
    TYPE_VIDEO = 'video'      # 视频
    TYPE_TEXT = 'text'        # 图文
    TYPE_LINK = 'link'        # 外链
    TYPE_QUIZ = 'quiz'        # 测验
    TYPE_HOMEWORK = 'homework'  # 作业

    id = db.Column(db.Integer, primary_key=True)
    chapter_id = db.Column(db.Integer, db.ForeignKey('chapter.id'), nullable=False)
    course_id = db.Column(db.Integer, db.ForeignKey('course.id'), nullable=False)
    title = db.Column(db.String(200), nullable=False)  # 课时标题

    # 课时类型: video=视频, text=图文, link=外链, quiz=测验, homework=作业
    type = db.Column(db.String(20), nullable=False, default=TYPE_TEXT)

    content = db.Column(LONGTEXT)  # 图文内容或外链URL
    duration = db.Column(db.Integer, default=0)  # 时长（分钟）
    order = db.Column(db.Integer, nullable=False, default=0)  # 排序
    is_preview = db.Column(db.Boolean, default=False)  # 是否可免费预览
    resource_url = db.Column(db.String(200))  # 附件/视频资源URL

    create_time = db.Column(db.DateTime, default=datetime.now)

    def to_dict(self):
        """转换为字典格式"""
        return {
            'id': self.id,
            'chapter_id': self.chapter_id,
            'course_id': self.course_id,
            'title': self.title,
            'type': self.type,
            'content': self.content,
            'duration': self.duration,
            'order': self.order,
            'resource_url': self.resource_url,
            'create_time': self.create_time.strftime('%Y-%m-%d %H:%M:%S') if self.create_time else None
        }


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


# ============== 新课程小组模型 (CourseGroup) ==============

class CourseGroup(db.Model):
    """课程学习小组"""
    __tablename__ = 'course_group'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)  # 小组名称
    description = db.Column(db.String(1000), default='')  # 小组描述
    course_id = db.Column(db.Integer, db.ForeignKey('course.id'), nullable=False)  # 课程ID
    teacher_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)  # 导师ID
    term = db.Column(db.String(20), default='2026-spring')  # 学期，如 2026-spring
    student_limit = db.Column(db.Integer, default=30)  # 人数限制
    status = db.Column(db.String(20), default='active')  # 状态: active(进行中), completed(已完成), paused(已暂停)

    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)

    # 联合唯一约束：(course_id, teacher_id, term) 保证同一老师在同一课程同一学期只有一个组
    # (id, course_id) 用于复合外键引用
    __table_args__ = (
        db.UniqueConstraint('course_id', 'teacher_id', 'term', name='uq_course_teacher_term'),
        db.UniqueConstraint('id', 'course_id', name='uq_id_course'),
    )

    # 关联
    course = db.relationship('CourseModel', backref=db.backref('course_groups', lazy=True))
    teacher = db.relationship('UserModel', foreign_keys=[teacher_id])


class CourseGroupMember(db.Model):
    """课程小组成员"""
    __tablename__ = 'course_group_member'

    # 角色常量
    ROLE_LEADER = 'leader'
    ROLE_MEMBER = 'member'

    # 状态常量
    STATUS_ACTIVE = 'active'
    STATUS_INACTIVE = 'inactive'

    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, nullable=False)  # 使用复合外键
    course_id = db.Column(db.Integer, nullable=False)  # 显式记录 course_id
    student_id = db.Column(db.Integer, nullable=False)  # 使用复合外键

    role = db.Column(db.String(20), default=ROLE_MEMBER)  # 角色: leader/member
    status = db.Column(db.String(20), default=STATUS_ACTIVE)  # 状态: active/inactive
    last_active = db.Column(db.DateTime, nullable=True)  # 最近活跃时间
    completion_rate = db.Column(db.Float, default=0.0)  # 任务完成率 0-100

    joined_at = db.Column(db.DateTime, default=datetime.now)

    # 联合唯一约束：(student_id, course_id) 防止同课程多组
    # 复合外键：(group_id, course_id) -> course_group(id, course_id)
    # 复合外键：(student_id, course_id) -> user_course(user_id, course_id)
    __table_args__ = (
        db.UniqueConstraint('student_id', 'course_id', name='uq_student_course'),
        db.ForeignKeyConstraint(
            ['group_id', 'course_id'],
            ['course_group.id', 'course_group.course_id'],
            name='fk_member_group_course'
        ),
        db.ForeignKeyConstraint(
            ['student_id', 'course_id'],
            ['user_course.user_id', 'user_course.course_id'],
            name='fk_member_user_course'
        ),
    )

    # 关联 - 使用 primaryjoin 明确指定连接条件
    student = db.relationship('UserModel',
        primaryjoin="CourseGroupMember.student_id==UserModel.id",
        foreign_keys=[student_id]
    )
    group = db.relationship('CourseGroup',
        primaryjoin="and_(CourseGroupMember.group_id==CourseGroup.id, CourseGroupMember.course_id==CourseGroup.course_id)",
        foreign_keys=[group_id, course_id],
        backref='members'
    )


class CourseGroupJoinRequest(db.Model):
    """课程小组加入申请表"""
    __tablename__ = 'course_group_join_request'

    # 状态常量
    STATUS_PENDING = 'pending'
    STATUS_APPROVED = 'approved'
    STATUS_REJECTED = 'rejected'
    STATUS_CANCELED = 'canceled'

    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, nullable=False)
    course_id = db.Column(db.Integer, nullable=False)
    student_id = db.Column(db.Integer, nullable=False)

    # 申请理由
    apply_reason = db.Column(db.Text, nullable=True)

    # 审核备注
    review_note = db.Column(db.Text, nullable=True)

    # 审核老师
    reviewed_by = db.Column(db.Integer, nullable=True)

    # 状态: pending / approved / rejected / canceled
    status = db.Column(db.String(20), default=STATUS_PENDING)

    # 时间戳
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)
    reviewed_at = db.Column(db.DateTime, nullable=True)

    # 索引和外键
    __table_args__ = (
        db.Index('idx_group_status_created', 'group_id', 'status', 'created_at'),
        db.Index('idx_student_course_status', 'student_id', 'course_id', 'status'),
        db.ForeignKeyConstraint(['group_id', 'course_id'], ['course_group.id', 'course_group.course_id']),
        db.ForeignKeyConstraint(['student_id', 'course_id'], ['user_course.user_id', 'user_course.course_id']),
    )

    # 关联
    student = db.relationship('UserModel', primaryjoin="CourseGroupJoinRequest.student_id==UserModel.id", foreign_keys=[student_id])
    group = db.relationship('CourseGroup', primaryjoin="CourseGroupJoinRequest.group_id==CourseGroup.id", foreign_keys=[group_id])
    reviewer = db.relationship('UserModel', primaryjoin="CourseGroupJoinRequest.reviewed_by==UserModel.id", foreign_keys=[reviewed_by])


# ==================== 任务模块 ====================

class Task(db.Model):
    """任务主表"""
    __tablename__ = 'task'

    # 状态常量
    STATUS_DRAFT = 'draft'
    STATUS_PUBLISHED = 'published'
    STATUS_CLOSED = 'closed'

    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, nullable=False)
    course_id = db.Column(db.Integer, nullable=False)
    term = db.Column(db.String(20), nullable=False)
    teacher_id = db.Column(db.Integer, nullable=False)

    title = db.Column(db.String(200), nullable=False)  # 任务标题
    requirement_text = db.Column(db.Text, nullable=True)  # 任务要求

    deadline_at = db.Column(db.DateTime, nullable=True)  # 截止时间 (UTC)
    allow_late = db.Column(db.Boolean, default=False)  # 是否允许逾期提交
    max_attempts = db.Column(db.Integer, default=0)  # 0=不限制次数，>0=限制次数

    # 成绩策略
    is_scored = db.Column(db.Boolean, default=False)  # 是否打分
    score_min = db.Column(db.Integer, default=0)  # 最低分
    score_max = db.Column(db.Integer, default=100)  # 最高分

    # 状态
    status = db.Column(db.String(20), default=STATUS_DRAFT)  # draft/published/closed

    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)

    # 索引
    __table_args__ = (
        db.Index('idx_task_group_status_deadline', 'group_id', 'status', 'deadline_at'),
    )

    # 关联
    group = db.relationship('CourseGroup', primaryjoin="Task.group_id==CourseGroup.id", foreign_keys=[group_id])
    teacher = db.relationship('UserModel', primaryjoin="Task.teacher_id==UserModel.id", foreign_keys=[teacher_id])
    assignees = db.relationship('TaskAssignee', back_populates='task', cascade='all, delete-orphan')
    submissions = db.relationship('TaskSubmission', back_populates='task', cascade='all, delete-orphan')


class TaskAssignee(db.Model):
    """任务派发对象"""
    __tablename__ = 'task_assignee'

    # 状态常量
    STATUS_NOT_STARTED = 'not_started'
    STATUS_SUBMITTED = 'submitted'
    STATUS_LATE = 'late'
    STATUS_GRADED = 'graded'
    STATUS_MISSED = 'missed'

    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer, db.ForeignKey('task.id'), nullable=False)
    student_id = db.Column(db.Integer, nullable=False)

    assigned_at = db.Column(db.DateTime, default=datetime.now)  # 分配时间
    status = db.Column(db.String(20), default=STATUS_NOT_STARTED)  # not_started/submitted/late/graded/missed
    extension_deadline_at = db.Column(db.DateTime, nullable=True)  # 延期截止时间

    # 唯一约束
    __table_args__ = (
        db.UniqueConstraint('task_id', 'student_id', name='uq_task_student'),
    )

    # 关联
    task = db.relationship('Task', back_populates='assignees')
    student = db.relationship('UserModel', primaryjoin="TaskAssignee.student_id==UserModel.id", foreign_keys=[student_id])


class TaskSubmission(db.Model):
    """任务提交记录"""
    __tablename__ = 'task_submission'

    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer, db.ForeignKey('task.id'), nullable=False)
    student_id = db.Column(db.Integer, nullable=False)
    attempt_no = db.Column(db.Integer, default=1)  # 第几次提交

    content_text = db.Column(db.Text, nullable=True)  # 提交内容
    submitted_at = db.Column(db.DateTime, default=datetime.now)  # 提交时间

    is_late = db.Column(db.Boolean, default=False)  # 是否逾期
    score = db.Column(db.Integer, nullable=True)  # 得分
    feedback = db.Column(db.Text, nullable=True)  # 老师反馈
    graded_by = db.Column(db.Integer, nullable=True)  # 评分老师ID
    graded_at = db.Column(db.DateTime, nullable=True)  # 评分时间

    # 唯一约束：同一学生同一任务同一尝试次数唯一
    __table_args__ = (
        db.UniqueConstraint('task_id', 'student_id', 'attempt_no', name='uq_task_student_attempt'),
    )

    # 关联
    task = db.relationship('Task', back_populates='submissions')
    student = db.relationship('UserModel', primaryjoin="TaskSubmission.student_id==UserModel.id", foreign_keys=[student_id])
    grader = db.relationship('UserModel', primaryjoin="TaskSubmission.graded_by==UserModel.id", foreign_keys=[graded_by])
    attachments = db.relationship('TaskSubmissionAttachment', back_populates='submission', cascade='all, delete-orphan')


class TaskSubmissionAttachment(db.Model):
    """任务提交附件"""
    __tablename__ = 'task_submission_attachment'

    id = db.Column(db.Integer, primary_key=True)
    submission_id = db.Column(db.Integer, db.ForeignKey('task_submission.id'), nullable=False)
    file_name = db.Column(db.String(255), nullable=False)  # 文件名
    storage_key = db.Column(db.String(500), nullable=False)  # 存储路径
    mime_type = db.Column(db.String(100), nullable=True)  # MIME类型
    size_bytes = db.Column(db.Integer, nullable=True)  # 文件大小
    sha256 = db.Column(db.String(64), nullable=True)  # 文件哈希
    kind = db.Column(db.String(20), default='file')  # image/file

    created_at = db.Column(db.DateTime, default=datetime.now)

    # 关联
    submission = db.relationship('TaskSubmission', back_populates='attachments')


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
    type = db.Column(db.Integer, nullable=False)  # 0:信息提醒信息, 1: 请假信息, 2: 任务信息, 3: 通知信息, 4: 报错信息 5: 作业信息
    title = db.Column(db.String(100), nullable=False)
    content = db.Column(db.Text)
    create_time = db.Column(db.DateTime, default=datetime.now)
    
    # 请假信息特有字段
    start_time = db.Column(db.DateTime)
    end_time = db.Column(db.DateTime)  # 也用于任务信息的截止时间
    student_id = db.Column(db.String(100))
    status = db.Column(db.Integer, default=0)  # 0: 未批准, 1: 已批准
    
    # 任务信息特有字段
    priority = db.Column(db.Integer)  # 1-5, 数字越小优先级越高
    students_id = db.Column(db.String(100)) # 如果对应多个用户id，就用逗号隔开
    
    # 通知信息特有字段
    range = db.Column(db.String(100)) # 如果对应多个用户id，就用逗号隔开，如果是小组全选，则为0

    # 报错信息特有字段
    resource = db.Column(db.String(100)) # 资源链接

    # 作业信息特有字段
    comment = db.Column(db.Text) # 批改意见
    score = db.Column(db.String(100)) # 作业分数
    
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
                'create_time': self.create_time,
                'students_id': self.students_id
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
        elif self.type == 5:  # 作业信息
            return {
                'id': self.id,
                'title': self.title,
                'content': self.content,
                'student_id': self.student_id,
                'resource': self.resource,          #作业链接
                'create_time': self.create_time,
                'status': self.status,               #批改情况
                'range': self.range,                 #作业对应的任务信息id
                'comment': self.comment,             #批改意见
                'score': self.score                  #作业分数
            }
        
        return None


class ArticleComment(db.Model):
    __tablename__ = 'article_comment'
    id = db.Column(db.Integer, primary_key=True)
    like_time = db.Column(db.DateTime)  # 点赞时间
    view_time = db.Column(db.DateTime)  # 浏览时间
    article_id = db.Column(db.Integer, db.ForeignKey('article.id'))
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))


# 审计日志模型
class AuditLog(db.Model):
    __tablename__ = 'audit_log'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    username = db.Column(db.String(100), nullable=False)
    ip_address = db.Column(db.String(45), nullable=False)  # 支持IPv4和IPv6
    user_agent = db.Column(db.Text)  # 浏览器信息
    operation = db.Column(db.String(200), nullable=False)  # 操作内容
    operation_url = db.Column(db.String(200))  # 操作的URL
    operation_data = db.Column(db.Text)  # 操作的数据（JSON格式）
    result = db.Column(db.String(50))  # 操作结果（成功/失败）
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    
    # 关联用户
    user = db.relationship('UserModel', backref=db.backref('audit_logs', lazy='dynamic'))


# 权限模块表
class PermissionModel(db.Model):
    __tablename__ = 'permission'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False, unique=True)  # 权限名称
    description = db.Column(db.String(200))  # 权限描述


# 用户权限关联表
class UserPermissionModel(db.Model):
    __tablename__ = 'user_permission'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    permission_id = db.Column(db.Integer, db.ForeignKey('permission.id'), nullable=False)
    
    # 添加联合唯一约束，防止重复分配相同权限
    __table_args__ = (db.UniqueConstraint('user_id', 'permission_id'),)
    
    user = db.relationship('UserModel', backref=db.backref('user_permissions', lazy=True))
    permission = db.relationship('PermissionModel', backref=db.backref('user_permissions', lazy=True))


# ==================== 讨论区模块 ====================

class DiscussionThread(db.Model):
    """讨论主题帖"""
    __tablename__ = 'discussion_thread'

    # 状态常量
    STATUS_NORMAL = 'normal'
    STATUS_HIDDEN = 'hidden'
    STATUS_LOCKED = 'locked'
    STATUS_DELETED = 'deleted'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    scope_type = db.Column(db.String(20), nullable=False)  # global/article/course/group/task
    scope_id = db.Column(db.Integer, nullable=True)  # 关联对象ID，global时为空
    title = db.Column(db.String(200), nullable=False)
    content = db.Column(db.Text, nullable=False)
    author_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    status = db.Column(db.String(20), default=STATUS_NORMAL)
    is_pinned = db.Column(db.Boolean, default=False)
    reply_count = db.Column(db.Integer, default=0)
    like_count = db.Column(db.Integer, default=0)
    view_count = db.Column(db.Integer, default=0)
    last_reply_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)

    # 索引
    __table_args__ = (
        db.Index('idx_thread_scope_status', 'scope_type', 'scope_id', 'status', 'last_reply_at'),
        db.Index('idx_thread_author', 'author_id', 'created_at'),
    )

    # 关系
    author = db.relationship('UserModel', backref=db.backref('discussion_threads', lazy='dynamic'))
    replies = db.relationship('DiscussionReply', backref='thread', lazy='dynamic',
                             cascade='all, delete-orphan', order_by='DiscussionReply.created_at')


class DiscussionReply(db.Model):
    """讨论回复"""
    __tablename__ = 'discussion_reply'

    STATUS_NORMAL = 'normal'
    STATUS_HIDDEN = 'hidden'
    STATUS_DELETED = 'deleted'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    thread_id = db.Column(db.Integer, db.ForeignKey('discussion_thread.id'), nullable=False)
    parent_reply_id = db.Column(db.Integer, db.ForeignKey('discussion_reply.id'), nullable=True)  # 楼中楼
    author_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    content = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(20), default=STATUS_NORMAL)
    like_count = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)

    # 索引
    __table_args__ = (
        db.Index('idx_reply_thread', 'thread_id', 'created_at'),
        db.Index('idx_reply_parent', 'parent_reply_id', 'created_at'),
    )

    # 关系
    author = db.relationship('UserModel', backref=db.backref('discussion_replies', lazy='dynamic'))
    children = db.relationship('DiscussionReply', backref=db.backref('parent', remote_side=[id]), lazy='dynamic')


class DiscussionReaction(db.Model):
    """讨论互动（点赞等）"""
    __tablename__ = 'discussion_reaction'

    TARGET_THREAD = 'thread'
    TARGET_REPLY = 'reply'
    REACTION_LIKE = 'like'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    target_type = db.Column(db.String(20), nullable=False)  # thread/reply
    target_id = db.Column(db.Integer, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    reaction_type = db.Column(db.String(20), default=REACTION_LIKE)
    created_at = db.Column(db.DateTime, default=datetime.now)

    # 唯一约束：防止重复点赞
    __table_args__ = (
        db.UniqueConstraint('user_id', 'target_type', 'target_id', 'reaction_type', name='uq_discussion_reaction'),
    )

    # 关系
    user = db.relationship('UserModel', backref=db.backref('discussion_reactions', lazy='dynamic'))

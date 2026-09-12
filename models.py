from datetime import datetime

from pygments.lexer import default
from sqlalchemy import and_
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.orm import foreign, remote

from werkzeug.security import generate_password_hash, check_password_hash

from exts import db


class UserModel(db.Model):
    __tablename__ = 'user'
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    username = db.Column(db.String(100), nullable=False)
    # 存储的是经过加盐哈希后的密码（前端先做 MD5，后端再用 pbkdf2 加盐哈希）。
    # 历史数据可能仍是前端 MD5 明文，由 check_password 兼容并在登录时自动升级。
    password = db.Column(db.String(255), nullable=False)
    email = db.Column(db.String(100), nullable=False, unique=True)
    join_time = db.Column(db.DateTime, default=datetime.now)
    # 签出测试
    # 添加勋章，学习阶段
    medal = db.Column(db.Integer, server_default='0')
    study_stage = db.Column(db.Text)
    # 身份解耦（2026-09 Phase 1a）：全局角色只分两级 super_admin / user；
    # 「导生/组长/学员」等是营期内任职（CampMember），不再看本字段。
    # 旧 user_mode 列已随 migrate_10_identity 删除（约 48 处裸门禁同步清账）。
    role = db.Column(db.String(20), nullable=False, server_default='user')
    # super_admin 内部标签（teacher/developer），仅审计日志与界面展示，无权限语义
    admin_tag = db.Column(db.String(20))
    # 用户等级地基（LV1-4）：当前仅作为导生候选人等筛选策略的数据源，管理员手动调整；
    # 贡献/学业评价引擎与自动升级属阶段 3。
    level = db.Column(db.Integer, nullable=False, server_default='1')
    # 账号状态（2026-09-11 用户管理）：active/banned。封禁=禁登录+存量token入口拦截，
    # 内容与营期归属全保留、可逆——取代删除（user.id 被 25+ 表引用，删除不可行，用户定）
    status = db.Column(db.String(20), nullable=False, server_default='active')
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
    # 邮件通知接收开关：关闭后不再收到通知/报告类邮件（登录验证码不受影响）。
    # 存量用户默认开启（server_default='1'），历史行为不变；列由
    # scripts/migrate/migrate_17_email_notify.py 添加（create_all 不补已存在表的列）。
    email_notify_enabled = db.Column(db.Boolean, nullable=False, server_default='1')

    # down_code = db.Column(db.String(100))
    # down_id = db.Column(db.Integer)

    # 用于识别 password 字段是否已是加盐哈希值（而非历史遗留的 MD5 明文）
    _PWHASH_PREFIXES = ('pbkdf2:', 'scrypt:', 'argon2')

    @property
    def password_is_hashed(self):
        """判断当前存储的密码是否已经过加盐哈希处理。"""
        return isinstance(self.password, str) and self.password.startswith(self._PWHASH_PREFIXES)

    def set_password(self, raw_password):
        """对前端传来的密码（已是 MD5）再做加盐哈希后存储。"""
        self.password = generate_password_hash(raw_password, method='pbkdf2:sha256')

    def check_password(self, raw_password):
        """校验密码，兼容历史遗留的明文(MD5)存储。

        返回 True/False。若需在校验通过后把历史明文升级为哈希，
        由调用方判断 password_is_hashed 后调用 set_password 并提交。
        """
        if not isinstance(self.password, str) or not self.password:
            return False
        if self.password_is_hashed:
            return check_password_hash(self.password, raw_password)
        # 历史遗留：数据库中直接存的是前端 MD5 明文
        return self.password == raw_password

    # ── 全局角色（两级）：super_admin / user ──
    # 营内身份（导生/学员等）查 CampMember，不查这里。
    ROLE_RANK = {'super_admin': 1, 'user': 0}

    @property
    def role_rank(self):
        return self.ROLE_RANK.get(self.role or 'user', 0)

    def has_role_at_least(self, role):
        """历史兼容：两级模型下等价于「是否 super_admin」"""
        return self.role_rank >= self.ROLE_RANK.get(role, 0)

    def is_staff(self):
        """管理端准入（身份解耦后仅 super_admin；导生事务在用户端完成）"""
        return self.role == 'super_admin'

    def is_admin(self):
        """系统级全权。全局管理员判断的唯一收口，禁止裸比较 role 字符串"""
        return self.role == 'super_admin'

    def is_admin_like(self):
        """deprecated：is_admin() 的旧名，保留别名避免散落改动"""
        return self.role == 'super_admin'


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


class ArticleV2Model(db.Model):
    """文章 V2：正文存 Markdown（content_md 字段，不写文件）。

    与旧 ArticleModel 完全隔离（独立表 article_v2）。backref 用 articles_v2，
    避免与 ArticleModel 的 backref="articles" 在 UserModel 上冲突。

    status：draft（草稿，仅作者本人/管理员可见）/ published（已发布，公开）。
    草稿的内容字段允许空；created_at/updated_at 记录创建与最后编辑；
    publish_time 仅已发布文章有（草稿为 None）。
    """
    __tablename__ = 'article_v2'
    STATUS_DRAFT = 'draft'
    STATUS_PUBLISHED = 'published'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    title = db.Column(db.String(100), nullable=True)        # 草稿允许空
    introduction = db.Column(db.Text, nullable=True)        # 草稿允许空
    content_md = db.Column(db.Text, nullable=True)          # Markdown 正文，不写文件；草稿允许空
    status = db.Column(db.String(20), default=STATUS_PUBLISHED)
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)
    publish_time = db.Column(db.DateTime, nullable=True)    # 仅已发布有；草稿为 None
    # 外键
    author_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    author = db.relationship(UserModel, backref="articles_v2")


class CourseModel(db.Model):
    __tablename__ = 'course'
    STATUS_NORMAL = 'normal'
    STATUS_DELETED = 'deleted'

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
    status = db.Column(db.String(20), default=STATUS_NORMAL)

    # 课程创建者，用于权限管理
    creator_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)


class CourseResourceModel(db.Model):
    """课程相关资源。文件本体在对象存储（见 storage.py），
    本表只存元数据；object_key 形如 courses/{course_id}/{uuid}"""
    __tablename__ = 'course_resource'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    course_id = db.Column(db.Integer, db.ForeignKey('course.id'), nullable=False, index=True)
    name = db.Column(db.String(200), nullable=False)        # 展示名（含扩展名）
    object_key = db.Column(db.String(300), nullable=False)  # 对象存储 key
    size = db.Column(db.Integer, nullable=False, default=0) # 字节数
    content_type = db.Column(db.String(100))
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, default=datetime.now)

    course = db.relationship('CourseModel', backref=db.backref('resources', lazy='dynamic'))

    def to_dict(self):
        return {
            'id': self.id,
            'course_id': self.course_id,
            'name': self.name,
            'size': self.size,
            'content_type': self.content_type,
            'sort_order': self.sort_order,
            'created_at': self.created_at.strftime('%Y-%m-%d %H:%M') if self.created_at else None
        }



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
    camp_session_id = db.Column(db.Integer, db.ForeignKey('camp_session.id'), nullable=True)  # 营期选课

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
    camp_session_id = db.Column(db.Integer, db.ForeignKey('camp_session.id'), nullable=True)  # 营期内发放
    issued_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)                # 发放人(导生/老师)

    user = db.relationship('UserModel', foreign_keys=[user_id], backref=db.backref('medal_user', lazy=True))
    issuer = db.relationship('UserModel', foreign_keys=[issued_by])
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
    __table_args__ = (
        db.Index('ix_check_record_user_date', 'user_id', 'date'),
    )
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    check_in = db.Column(db.DateTime)
    check_out = db.Column(db.DateTime)
    duration = db.Column(db.Float)
    date = db.Column(db.Date, index=True)
    camp_session_id = db.Column(db.Integer, db.ForeignKey('camp_session.id'), nullable=True, index=True)
    seat_id = db.Column(db.Integer, db.ForeignKey('seat.id'), nullable=True)

class RoomModel(db.Model):
    __tablename__ = 'study_room'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False)  # 如 '106'
    description = db.Column(db.String(200))
    created_at = db.Column(db.DateTime, default=datetime.now)

class SeatModel(db.Model):
    __tablename__ = 'seat'
    id = db.Column(db.Integer, primary_key=True)
    room_id = db.Column(db.Integer, db.ForeignKey('study_room.id'), nullable=False)
    label = db.Column(db.String(20), nullable=False)  # 如 'A1'（字母=八角形，数字=三角形）
    bound_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), unique=True)  # 固定绑定，可空；unique=一人一座

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


class NotificationModel(db.Model):
    """通知表 — 独立于 information 表，语义清晰的通知记录"""
    __tablename__ = 'notification'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    user_id = db.Column(db.Integer, nullable=False, index=True)          # 接收人
    title = db.Column(db.String(200), nullable=False)
    content = db.Column(db.Text)
    category = db.Column(db.String(20), nullable=False, index=True)
        # 'system'  — 系统公告、维护通知
        # 'group'   — 小组内业务通知（请假/任务/作业/通知等）
        # 'course'  — 课程相关通知（预留）
        # 'camp'    — 营期通知（请假审批/奖励发放/考勤提醒等）
        # 'gratitude' — 感谢信送达提醒（source_id 指向 gratitude 表）
    camp_session_id = db.Column(db.Integer, db.ForeignKey('camp_session.id'), nullable=True, index=True)
    source_type = db.Column(db.String(20), nullable=True)
        # 触发来源：'leave', 'task', 'homework', 'notice', 'admin', 'reward', 'join_request', 'gratitude'
    source_id = db.Column(db.Integer, nullable=True)
        # 关联的原始记录 ID（如请假ID、任务ID）
    group_id = db.Column(db.Integer, nullable=True, index=True)
    is_read = db.Column(db.Boolean, default=False, index=True)
    is_important = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.now, index=True)

    def to_dict(self):
        return {
            'id': self.id,
            'user_id': self.user_id,
            'title': self.title,
            'content': self.content,
            'category': self.category,
            'camp_session_id': self.camp_session_id,
            'source_type': self.source_type,
            'source_id': self.source_id,
            'group_id': self.group_id,
            'is_read': self.is_read,
            'is_important': self.is_important,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class GratitudeModel(db.Model):
    """感谢信 — 学员写给导生的感谢留言

    依托用户对（sender/recipient），camp_session_id 仅作展示上下文，
    不校验匹配状态机；同营期内每对用户限一封（唯一约束），
    无营期上下文的信由接口层频控兜底。
    """
    __tablename__ = 'gratitude'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    sender_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    recipient_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    camp_session_id = db.Column(db.Integer, db.ForeignKey('camp_session.id'), nullable=True, index=True)
    content = db.Column(db.Text, nullable=False)
    visibility = db.Column(db.String(20), default='private')
        # 'private' — 仅收件导生可见；预留 'public'（导生主页感谢墙，见前端方案）
    is_read = db.Column(db.Boolean, default=False, index=True)   # 收件侧已读
    created_at = db.Column(db.DateTime, default=datetime.now, index=True)

    __table_args__ = (
        db.UniqueConstraint('sender_id', 'recipient_id', 'camp_session_id',
                            name='uq_gratitude_sender_recipient_session'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'sender_id': self.sender_id,
            'recipient_id': self.recipient_id,
            'camp_session_id': self.camp_session_id,
            'content': self.content,
            'visibility': self.visibility,
            'is_read': self.is_read,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


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


# ==================== 大模型（LiteLLM）模块 ====================

class LLMProjectModel(db.Model):
    """大模型项目：对应 LiteLLM 的 Team，不限额，仅监控用量"""
    __tablename__ = 'llm_project'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.String(100), nullable=False, unique=True)  # 项目名
    description = db.Column(db.Text)
    litellm_team_id = db.Column(db.String(100))  # LiteLLM team_id
    litellm_key = db.Column(db.String(200))  # 项目 virtual key（sk-...）
    models = db.Column(db.String(500))  # 允许的模型，逗号分隔，空表示全部
    created_by = db.Column(db.Integer, db.ForeignKey('user.id'))
    created_at = db.Column(db.DateTime, default=datetime.now)
    is_active = db.Column(db.Boolean, default=True)

    creator = db.relationship('UserModel', backref=db.backref('llm_projects', lazy='dynamic'))


class LLMUserKeyModel(db.Model):
    """平台用户自建的大模型 API Key（本地映射，预算挂在 LiteLLM user 上）"""
    __tablename__ = 'llm_user_key'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    key_alias = db.Column(db.String(100))  # key 别名
    litellm_key = db.Column(db.String(200), nullable=False)  # virtual key（sk-...）
    created_at = db.Column(db.DateTime, default=datetime.now)
    is_active = db.Column(db.Boolean, default=True)

    user = db.relationship('UserModel', backref=db.backref('llm_user_keys', lazy='dynamic'))


class LLMQuotaConfigModel(db.Model):
    """平台用户默认配额配置（单例，id=1）"""
    __tablename__ = 'llm_quota_config'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    default_max_budget = db.Column(db.Float, nullable=False, default=5.0)  # 默认额度（美元）
    budget_duration = db.Column(db.String(20), default='30d')  # 重置周期
    allowed_models = db.Column(db.String(500))  # 允许模型，逗号分隔，空表示全部
    updated_by = db.Column(db.Integer, db.ForeignKey('user.id'))
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)


class LLMQuotaRequestModel(db.Model):
    """平台用户增额申请"""
    __tablename__ = 'llm_quota_request'

    STATUS_PENDING = 'pending'
    STATUS_APPROVED = 'approved'
    STATUS_REJECTED = 'rejected'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    current_budget = db.Column(db.Float)  # 申请时的当前额度
    requested_budget = db.Column(db.Float, nullable=False)  # 期望的新额度
    reason = db.Column(db.Text)  # 申请理由
    status = db.Column(db.String(20), default=STATUS_PENDING)
    review_comment = db.Column(db.Text)  # 审批意见
    reviewed_by = db.Column(db.Integer, db.ForeignKey('user.id'))
    created_at = db.Column(db.DateTime, default=datetime.now)
    reviewed_at = db.Column(db.DateTime)
    override_expires_at = db.Column(db.DateTime, nullable=True)  # 临时增额到期时间
    reverted_at = db.Column(db.DateTime, nullable=True)          # 回滚完成时间，null=未回滚

    user = db.relationship('UserModel', foreign_keys=[user_id],
                           backref=db.backref('llm_quota_requests', lazy='dynamic'))
    reviewer = db.relationship('UserModel', foreign_keys=[reviewed_by])


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


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 营期（Camp）系统
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# 营期类型封闭枚举与行为默认值（老师不可自建类型；research/competition 预留，阶段 3 实现。
# 建营时按此模板落一行 CampPolicy，营期行可覆盖——避免主表继续膨胀，方案 §3.3/附录）
CAMP_CATEGORY_DEFAULTS = {
    'learning': {
        'label': '培训营（学习型）',
        'application': 'join_request',      # 报名方式：学员申请入池 + 管理员审批
        'formation': 'preference_export',   # 组队方式：单轮志愿收集 + 导出 CSV + 线下协调 + 批量回填
        'match_rule': 'single_mentor',      # 学习营单归属（每生一导生，强唯一）
        'project_limit': None,
        'course_policy': 'admin_managed',   # 阶段 5 生效：管理员负责课程，导生范围内共建
        # v1.3 能力开关（营期行可覆盖；后端按位门禁端点，前端按位渲染 tab）
        'capabilities': {'attendance': True, 'leave': True, 'seat': True},
    },
    'project': {
        'label': '项目营',
        'application': 'join_request',
        'formation': 'preference_export',   # 项目志愿单轮 + 导出 + 线下协调 + 回填
        'match_rule': 'multi_project',      # 每人最多参与 N 个项目（负责人自己的计入）
        'project_limit': 3,
        'course_policy': 'unit_creator',    # 阶段 5 生效：负责人在自己项目范围开课
        'capabilities': {'attendance': False, 'leave': False, 'seat': False},  # 首期全关（09-12 拍板）
    },
}


class CampCycle(db.Model):
    """营期周期（教学周期）：一年四段（寒假/春季/暑期/秋季），只做归类与统计。

    无起止日期（每年时间略有出入，不影响办营）、无状态、无草稿（R-003 / Q-002）。
    生命周期归具体营期（CampSession），周期不驱动其下营期的状态流转。
    """
    __tablename__ = 'camp_cycle'
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(20), nullable=False, unique=True)   # 如 2026-summer
    name = db.Column(db.String(50), nullable=False)                # 如 2026 暑期
    sort_order = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.now)

    sessions = db.relationship('CampSession', backref='cycle', lazy='dynamic')


class CampPolicy(db.Model):
    """营期策略（阶段 1 落表，阶段 3/5 开始消费）：报名方式 / 组队方式 / 归属规则 / 项目上限 / 课程策略 / 能力开关。
    建营时按 CAMP_CATEGORY_DEFAULTS[category] 生成一行，营期行可覆盖（方案 §3.3）。
    capabilities（v1.3）：JSON 位图文本 {'attendance':bool,'leave':bool,'seat':bool,...}——
    考勤/请假/座位等能力不定死于 category，CATEGORY_DEFAULTS 给默认值（learning 开、project 首期关），
    营期行可覆盖（某项目营要考勤=管理端打开，零代码）；后端按位门禁端点，前端按位渲染 tab。"""
    __tablename__ = 'camp_policy'
    id = db.Column(db.Integer, primary_key=True)
    application = db.Column(db.String(30), nullable=False, default='join_request')
    formation = db.Column(db.String(30), nullable=False, default='preference_export')
    match_rule = db.Column(db.String(30), nullable=False, default='single_mentor')
    project_limit = db.Column(db.Integer)                       # None = 不限（学习营）
    course_policy = db.Column(db.String(30), nullable=False, default='admin_managed')
    capabilities = db.Column(db.Text)                            # JSON 能力位图；NULL=按类型默认值
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)


class CampSession(db.Model):
    """营期：如 2026暑期营 / 2026-1学期营"""
    __tablename__ = 'camp_session'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    camp_type = db.Column(db.String(20), default='short_term')   # 退役：阶段 1 起停读写，保留列便于回滚，下个大版本删除
    # ── 阶段 1 共用骨架（2026-09）──
    category = db.Column(db.String(20), nullable=False, server_default='learning')  # learning / project（research/competition 预留）
    cycle_id = db.Column(db.Integer, db.ForeignKey('camp_cycle.id'))                 # 归属教学周期（CampCycle）
    policy_id = db.Column(db.Integer, db.ForeignKey('camp_policy.id'))               # 营期策略（CampPolicy，建营时按类型默认值生成）
    start_date = db.Column(db.Date, nullable=False)
    end_date = db.Column(db.Date, nullable=False)
    status = db.Column(db.String(20), default='draft')           # draft / active / archived
    # 弹性考勤规则
    expected_check_in = db.Column(db.Time)                       # 期望到岗时间（判迟到基准）
    min_daily_hours = db.Column(db.Float)                        # 每日最低有效时长（判达标）
    weekdays_only = db.Column(db.Boolean, default=True)          # 承诺出勤日 = 范围内工作日
    is_featured = db.Column(db.Boolean, default=False)          # 招募指针（全局唯一，管理端设置）：/camp-home 招募页与 /camp 空状态指向它；成员工作台不消费
    # 选导生（可选的开营前置阶段，单轮制，规则见 docs/营期选导生-规划.md）
    mentor_selection_enabled = db.Column(db.Boolean, default=False)   # 是否启用
    ms_preference_start = db.Column(db.DateTime)                # 阶段开始（导生即可建名片）
    ms_preference_deadline = db.Column(db.DateTime)             # 学员志愿截止（之后老师导出 CSV 线下协调再批量指派）
    ms_round1_deadline = db.Column(db.DateTime)                 # 已废弃（单轮化）：保留列兼容旧数据，写入/阶段计算一律忽略
    ms_round2_deadline = db.Column(db.DateTime)                 # 已废弃（单轮化）：保留列兼容旧数据，写入/阶段计算一律忽略
    ms_tags = db.Column(db.Text)                                # 分类标签 JSON 数组字符串（导生名片从中勾选）
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)

    policy = db.relationship('CampPolicy')   # 营期策略（一对一，建营生成）


class CampMember(db.Model):
    """营期成员（独立于 CourseGroup，隔离安全）。
    role=student/mentor；team_mentor_id 仅 student 行填，指向其导生。"""
    __tablename__ = 'camp_member'
    id = db.Column(db.Integer, primary_key=True)
    camp_session_id = db.Column(db.Integer, db.ForeignKey('camp_session.id'), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    role = db.Column(db.String(20), nullable=False, default='student')          # student / mentor
    team_mentor_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True, index=True)
    joined_at = db.Column(db.DateTime, default=datetime.now)
    __table_args__ = (
        db.UniqueConstraint('camp_session_id', 'user_id', name='uq_camp_member_camp_user'),
    )


class CampCourse(db.Model):
    """营期可选课程目录"""
    __tablename__ = 'camp_course'
    id = db.Column(db.Integer, primary_key=True)
    camp_session_id = db.Column(db.Integer, db.ForeignKey('camp_session.id'), nullable=False, index=True)
    course_id = db.Column(db.Integer, db.ForeignKey('course.id'), nullable=False, index=True)
    sort_order = db.Column(db.Integer, default=0)
    __table_args__ = (
        db.UniqueConstraint('camp_session_id', 'course_id', name='uq_camp_course'),
    )


class CampAttendancePlan(db.Model):
    """承诺出勤日期（营期范围 × 工作日展开；阈值冗余自 CampSession，便于按日判定）"""
    __tablename__ = 'camp_attendance_plan'
    id = db.Column(db.Integer, primary_key=True)
    camp_session_id = db.Column(db.Integer, db.ForeignKey('camp_session.id'), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    date = db.Column(db.Date, nullable=False)
    expected_check_in = db.Column(db.Time)    # 冗余：判迟到
    min_daily_hours = db.Column(db.Float)     # 冗余：判达标
    __table_args__ = (
        db.UniqueConstraint('camp_session_id', 'user_id', 'date', name='uq_camp_plan_user_date'),
    )


class CampSeat(db.Model):
    """营期座位分配（复用物理 Seat，按营期独立分配；不动全局 Seat.bound_user_id）"""
    __tablename__ = 'camp_seat'
    id = db.Column(db.Integer, primary_key=True)
    camp_session_id = db.Column(db.Integer, db.ForeignKey('camp_session.id'), nullable=False, index=True)
    seat_id = db.Column(db.Integer, db.ForeignKey('seat.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)    # null = 未分配
    __table_args__ = (
        db.UniqueConstraint('camp_session_id', 'seat_id', name='uq_camp_seat'),
    )


class CampLeave(db.Model):
    """营期请假（整天 + 连续日期段）"""
    __tablename__ = 'camp_leave'
    id = db.Column(db.Integer, primary_key=True)
    camp_session_id = db.Column(db.Integer, db.ForeignKey('camp_session.id'), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    start_date = db.Column(db.Date, nullable=False)
    end_date = db.Column(db.Date, nullable=False)
    reason = db.Column(db.Text)
    status = db.Column(db.String(20), default='pending')   # pending / approved / rejected
    approver_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    approved_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.now)


class CampJoinRequest(db.Model):
    apply_role = db.Column(db.String(20), default='student')   # 申请身份：student 学员 / mentor 导生（导生报名需管理员审核）
    """营期加入申请（学员自助申请 → teacher/super_admin 审批 → 通过即 member_assign 入营）。
    不加 UQ(camp,user)：rejected 后允许重新提交（新行）；端点校验"无 pending 申请 + 非成员"。"""
    __tablename__ = 'camp_join_request'
    id = db.Column(db.Integer, primary_key=True)
    camp_session_id = db.Column(db.Integer, db.ForeignKey('camp_session.id'), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    reason = db.Column(db.Text, nullable=True)
    preferred_tag = db.Column(db.String(50), nullable=True)   # 【已退役 2026-09-12】报名时选的意向大组（组别改随归属导生继承）；列保留存历史行，新申请不写
    selected_days = db.Column(db.Text, nullable=True)   # 学员手选承诺出勤日（JSON 数组字符串，approve 后展开为 CampAttendancePlan）
    status = db.Column(db.String(20), default='pending', index=True)   # pending / approved / rejected
    reviewed_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    reviewed_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.now)


class CampMentorEligibilityBatch(db.Model):
    """【已退役 2026-09-12】导生资格名单批次（阶段 2，Q-007）。导生改自由报名
    （LV≥2 在 upcoming/selecting 窗口自助提交，管理员审核），资格名单机制下线；
    表保留不删（免迁移），蓝图已无引用，后续清债再移除。"""
    __tablename__ = 'camp_mentor_eligibility_batch'
    id = db.Column(db.Integer, primary_key=True)
    camp_session_id = db.Column(db.Integer, db.ForeignKey('camp_session.id'), index=True)
    imported_by = db.Column(db.Integer, db.ForeignKey('user.id'))
    status = db.Column(db.String(20), default='confirmed')   # 预留 preview/confirmed，当前一步确认
    created_at = db.Column(db.DateTime, default=datetime.now)


class CampMentorEligibility(db.Model):
    """【已退役 2026-09-12】导生资格明细（资格名单机制，随自由报名上线退役；
    表保留不删，见 CampMentorEligibilityBatch 说明）。"""
    __tablename__ = 'camp_mentor_eligibility'
    id = db.Column(db.Integer, primary_key=True)
    batch_id = db.Column(db.Integer, db.ForeignKey('camp_mentor_eligibility_batch.id'))
    camp_session_id = db.Column(db.Integer, db.ForeignKey('camp_session.id'), index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)
    source = db.Column(db.String(20), default='manual')   # 进池方式：manual 手工导入 / level 按等级生成
    created_at = db.Column(db.DateTime, default=datetime.now)
    __table_args__ = (db.UniqueConstraint('camp_session_id', 'user_id', name='uq_cme_session_user'),)


class CampMentorProfile(db.Model):
    """选导生·导生名片（每营每人一张；无名片导生对学生不可见、不可被选）。
    资料仅在 upcoming/collecting 阶段可改（防协调期改容量/换照片），见 _ms_phase。"""
    __tablename__ = 'camp_mentor_profile'
    id = db.Column(db.Integer, primary_key=True)
    camp_session_id = db.Column(db.Integer, db.ForeignKey('camp_session.id'), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    photo = db.Column(db.String(100))            # 相对文件名 {camp}_{user}.{ext}，存 ./data/mentor_photos/
    bio = db.Column(db.Text)
    tags = db.Column(db.Text)                    # JSON 数组字符串，⊆ 营期 ms_tags（服务端校验）
    capacity = db.Column(db.Integer)             # 名额上限;NULL=不限(用户 2026-09-11 定,旧 default=8 废除)
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)
    __table_args__ = (
        db.UniqueConstraint('camp_session_id', 'user_id', name='uq_ms_profile_camp_user'),
    )


class CampMentorPreference(db.Model):
    """选导生·学员志愿（单轮：1~3 条有序；提交 = 整组替换，截止前可改）。
    round 恒为 1（旧两轮制历史数据可能存 2，读取一律按 round==1）。"""
    __tablename__ = 'camp_mentor_preference'
    id = db.Column(db.Integer, primary_key=True)
    camp_session_id = db.Column(db.Integer, db.ForeignKey('camp_session.id'), nullable=False, index=True)
    student_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    mentor_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    round = db.Column(db.Integer, nullable=False, default=1)   # 单轮化后恒为 1
    rank = db.Column(db.Integer, nullable=False)               # 1-3
    note = db.Column(db.String(200))                           # 学员可选留言（导生挑选时可见）
    created_at = db.Column(db.DateTime, default=datetime.now)
    __table_args__ = (
        db.UniqueConstraint('camp_session_id', 'student_user_id', 'round', 'rank', name='uq_ms_pref_rank'),
        db.UniqueConstraint('camp_session_id', 'student_user_id', 'round', 'mentor_user_id', name='uq_ms_pref_mentor'),
    )


class CampMentorMatch(db.Model):
    """选导生·配对账本（provenance：哪轮/谁配的）。
    live 真相是 camp_member.team_mentor_id——写入账本时同步设置；下游（考勤看板/请假
    审批/团队范围）只读 live 链接，本表仅用于结果展示与追溯。"""
    __tablename__ = 'camp_mentor_match'
    id = db.Column(db.Integer, primary_key=True)
    camp_session_id = db.Column(db.Integer, db.ForeignKey('camp_session.id'), nullable=False, index=True)
    mentor_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    student_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    round = db.Column(db.Integer, nullable=True)               # 1 | NULL(admin 指派/导生勾选)；2 仅历史数据
    source = db.Column(db.String(20), nullable=False, default='mentor_pick')   # admin 老师指派 / mentor_pick 导生自助勾选（协调期）
    created_at = db.Column(db.DateTime, default=datetime.now)
    __table_args__ = (
        db.UniqueConstraint('camp_session_id', 'student_user_id', name='uq_ms_match_student'),
    )


class CampMentorFavorite(db.Model):
    """选导生·学员收藏（市集个人便签：不限数量、不参与配对，仅收集期可标记）。
    与志愿（CampMentorPreference）解耦——收藏只服务浏览整理，导出/协调一律不读此表。"""
    __tablename__ = 'camp_mentor_favorite'
    id = db.Column(db.Integer, primary_key=True)
    camp_session_id = db.Column(db.Integer, db.ForeignKey('camp_session.id'), nullable=False, index=True)
    student_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    mentor_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.now)
    __table_args__ = (
        db.UniqueConstraint('camp_session_id', 'student_user_id', 'mentor_user_id', name='uq_ms_fav_mentor'),
    )


class CampChapterCertification(db.Model):
    """方向制学习·导生按章认证（2026-09-12，migrate_24）：学员随导生继承方向课程后，
    导生逐章认证其学习进度；全章认证齐 → user_course.status 自动置 completed（汇总态，撤销不回滚）。
    认证人留痕（改派后新导师可继续认证/撤销自己名下的行）。"""
    __tablename__ = 'camp_chapter_certification'
    id = db.Column(db.Integer, primary_key=True)
    camp_session_id = db.Column(db.Integer, db.ForeignKey('camp_session.id'), nullable=False, index=True)
    student_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    chapter_id = db.Column(db.Integer, db.ForeignKey('chapter.id'), nullable=False, index=True)
    course_id = db.Column(db.Integer, db.ForeignKey('course.id'), nullable=False, index=True)
    mentor_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)   # 认证导生（留痕）
    certified_at = db.Column(db.DateTime, default=datetime.now)
    __table_args__ = (
        db.UniqueConstraint('camp_session_id', 'student_user_id', 'chapter_id', name='uq_cert_camp_student_chapter'),
    )


# ── 项目营组织与申报组队（设计方案 v1.3 阶段3，migrate_20）──

class CampUnit(db.Model):
    """营期组织单元。unit_type=mentor_team（学习营，导生报名确认时自动建，1 导生 1 组；
    首期学习营现役流不落本表，统一 API 建成 unit 通用型，迁移窗口=下个学习营开营前）
    / project（项目营，申报过审时建，owner=负责人）。"""
    __tablename__ = 'camp_unit'
    id = db.Column(db.Integer, primary_key=True)
    camp_session_id = db.Column(db.Integer, db.ForeignKey('camp_session.id'), nullable=False, index=True)
    unit_type = db.Column(db.String(20), nullable=False, default='project')   # mentor_team / project
    name = db.Column(db.String(100), nullable=False)
    status = db.Column(db.String(20), nullable=False, default='active')       # active / paused / terminated
    visibility = db.Column(db.String(20), nullable=False, default='camp')     # camp（营内）/ public（可发布到项目展示平台）
    owner_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)  # 负责人
    created_at = db.Column(db.DateTime, default=datetime.now)
    __table_args__ = (
        db.UniqueConstraint('camp_session_id', 'unit_type', 'name', name='uq_unit_camp_type_name'),
    )


class ProjectProfile(db.Model):
    """项目档案（申报快照的现行版；随结营档案冻结）。visibility 与 CampUnit.visibility 同步冗余，
    是发布到项目展示平台的权限基础（v1.3 §3.7）。"""
    __tablename__ = 'project_profile'
    id = db.Column(db.Integer, primary_key=True)
    unit_id = db.Column(db.Integer, db.ForeignKey('camp_unit.id'), nullable=False, unique=True)
    background = db.Column(db.Text)          # 项目背景
    goal = db.Column(db.Text)                # 目标
    required_abilities = db.Column(db.Text)  # 所需能力
    recruit_note = db.Column(db.Text)        # 招募说明
    plan = db.Column(db.Text)                # 计划
    visibility = db.Column(db.String(20), nullable=False, default='camp')
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)


class ProjectApplicationVersion(db.Model):
    """项目申报版本（退回重提=新行不覆盖；过审时才建 CampUnit）。
    一人一营最多 1 个进行中申报 + 最多负责 1 个过审项目（Q-006，服务端双重校验）。"""
    __tablename__ = 'project_application_version'
    id = db.Column(db.Integer, primary_key=True)
    camp_session_id = db.Column(db.Integer, db.ForeignKey('camp_session.id'), nullable=False, index=True)
    unit_id = db.Column(db.Integer, db.ForeignKey('camp_unit.id'), nullable=True, index=True)  # 过审时回填
    version = db.Column(db.Integer, nullable=False, default=1)
    submitted_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    leader_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    # 申报快照（过审时落入 ProjectProfile）
    name = db.Column(db.String(100), nullable=False)
    background = db.Column(db.Text)
    goal = db.Column(db.Text)
    required_abilities = db.Column(db.Text)
    recruit_note = db.Column(db.Text)
    plan = db.Column(db.Text)
    status = db.Column(db.String(20), nullable=False, default='pending', index=True)  # pending / approved / rejected
    reject_reason = db.Column(db.String(500))
    reviewed_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    reviewed_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.now)
    __table_args__ = (
        db.UniqueConstraint('camp_session_id', 'leader_user_id', 'version', name='uq_pav_camp_leader_ver'),
    )


class CampUnitMember(db.Model):
    """单元成员（单元身份：leader/member）。项目 3 上限按 status=active 行计数（负责人自己的计入）。
    变更一律行状态化 ended（H-005 定稿：无锁定环节，running 起变更走管理员通道），不物理删。"""
    __tablename__ = 'camp_unit_member'
    id = db.Column(db.Integer, primary_key=True)
    unit_id = db.Column(db.Integer, db.ForeignKey('camp_unit.id'), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    role = db.Column(db.String(20), nullable=False, default='member')   # leader / member
    status = db.Column(db.String(20), nullable=False, default='active')  # active / ended
    started_at = db.Column(db.DateTime, default=datetime.now)
    ended_at = db.Column(db.DateTime, nullable=True)
    __table_args__ = (
        db.UniqueConstraint('unit_id', 'user_id', name='uq_unit_member'),
    )


class CampMembershipEvent(db.Model):
    """单元成员变更事件（只追加，不更新不删除）：勾选/移除/退出/调剂/负责人变更均落一行，
    before/after 记录变更前后状态，作为审计与追溯底座（方案 §3.8）。"""
    __tablename__ = 'camp_membership_event'
    id = db.Column(db.Integer, primary_key=True)
    camp_session_id = db.Column(db.Integer, db.ForeignKey('camp_session.id'), nullable=False, index=True)
    unit_id = db.Column(db.Integer, db.ForeignKey('camp_unit.id'), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    action = db.Column(db.String(30), nullable=False)   # select/deselect/exit/remove/adjust/leader_change/unit_status
    source = db.Column(db.String(30), nullable=False, default='leader_pick')  # leader_pick/admin_adjust/apply/approve
    operator_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    before = db.Column(db.Text)     # JSON：变更前 {role,status,unit...}
    after = db.Column(db.Text)      # JSON：变更后
    reason = db.Column(db.String(500))   # 管理员通道原因必填（H-005）
    occurred_at = db.Column(db.DateTime, default=datetime.now)


class CampProjectPreference(db.Model):
    """项目志愿（单轮 1~3 条有序；提交=整组替换，selecting 期内可改）。
    与导生志愿 CampMentorPreference 同构不共表（FK 主体不同，防多态外键），v1.3 §3.7。"""
    __tablename__ = 'camp_project_preference'
    id = db.Column(db.Integer, primary_key=True)
    camp_session_id = db.Column(db.Integer, db.ForeignKey('camp_session.id'), nullable=False, index=True)
    student_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    unit_id = db.Column(db.Integer, db.ForeignKey('camp_unit.id'), nullable=False, index=True)
    rank = db.Column(db.Integer, nullable=False)     # 1-3
    note = db.Column(db.String(200))                 # 学员可选留言
    created_at = db.Column(db.DateTime, default=datetime.now)
    __table_args__ = (
        db.UniqueConstraint('camp_session_id', 'student_user_id', 'rank', name='uq_pp_rank'),
        db.UniqueConstraint('camp_session_id', 'student_user_id', 'unit_id', name='uq_pp_unit'),
    )


# ── 项目营模板交付与档案（设计方案 v1.3 阶段4，migrate_21）──
# 三层解耦：模板管共性（节点施工图）/ 里程碑管交付（关卡实际发生）/ 课程管学习（阶段5）。
# 依赖单向：里程碑←模板，模板可引用课程（软链），课程不感知另外两者。

class ProjectTemplate(db.Model):
    """项目模板=节点施工图（管共性）。scope='platform'=平台默认模板（admin 维护，负责人选起点）；
    scope='unit'=项目模板（负责人创建）。结营冻结时 status→'archived'——可被后来负责人复制起步
    =资产回流入口（cloned_from 溯源链）。"""
    __tablename__ = 'project_template'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text)
    scope = db.Column(db.String(20), nullable=False, default='unit')       # platform / unit
    category = db.Column(db.String(50))                                    # 适用项目类别（筛选用，可空）
    camp_session_id = db.Column(db.Integer, db.ForeignKey('camp_session.id'), nullable=True, index=True)
    unit_id = db.Column(db.Integer, db.ForeignKey('camp_unit.id'), nullable=True, unique=True)
    created_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    cloned_from_id = db.Column(db.Integer, db.ForeignKey('project_template.id'), nullable=True)  # 复制链
    status = db.Column(db.String(20), nullable=False, default='active')    # active / archived
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)


class ProjectTemplateNode(db.Model):
    """模板节点：序号/标题/说明/交付要求/材料模板说明/推荐课程引用（软链 JSON）/提交主体（节点级）。"""
    __tablename__ = 'project_template_node'
    id = db.Column(db.Integer, primary_key=True)
    template_id = db.Column(db.Integer, db.ForeignKey('project_template.id'), nullable=False, index=True)
    sort_order = db.Column(db.Integer, nullable=False, default=1)
    title = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text)
    deliverable_req = db.Column(db.Text)              # 交付要求
    material_note = db.Column(db.Text)                # 材料模板说明
    recommended_course_ids = db.Column(db.Text)       # JSON 课程 id 数组（软链不复制）
    submit_mode = db.Column(db.String(10), nullable=False, default='team')  # team 整队交 / member 个人交


class CampMilestone(db.Model):
    """里程碑=关卡的实际发生（管交付）：实例化自模板节点（node_id 溯源，删节点不级联）；
    实例化后负责人可增删调时。status 聚合最新审核态（member 模式=全员 approved 才 approved）。"""
    __tablename__ = 'camp_milestone'
    id = db.Column(db.Integer, primary_key=True)
    camp_session_id = db.Column(db.Integer, db.ForeignKey('camp_session.id'), nullable=False, index=True)
    unit_id = db.Column(db.Integer, db.ForeignKey('camp_unit.id'), nullable=False, index=True)
    node_id = db.Column(db.Integer, db.ForeignKey('project_template_node.id'), nullable=True)
    title = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text)
    requirement = db.Column(db.Text)                  # 交付要求（实例化随节点，可改）
    due_date = db.Column(db.Date)
    order_no = db.Column(db.Integer, nullable=False, default=1)
    submit_mode = db.Column(db.String(10), nullable=False, default='team')
    status = db.Column(db.String(20), nullable=False, default='open')     # open/submitted/returned/approved
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)


class CampSubmissionVersion(db.Model):
    """版本化提交：退回重提=新版本（旧版 superseded）。member 模式每人一条版本链
    （负责人自己份额交老师审——防自审红线），team 模式一条链（负责人交老师审）。"""
    __tablename__ = 'camp_submission_version'
    id = db.Column(db.Integer, primary_key=True)
    milestone_id = db.Column(db.Integer, db.ForeignKey('camp_milestone.id'), nullable=False, index=True)
    version = db.Column(db.Integer, nullable=False)
    submitted_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    content = db.Column(db.Text)
    status = db.Column(db.String(20), nullable=False, default='submitted')  # submitted/returned/approved/superseded
    reviewed_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    reviewed_at = db.Column(db.DateTime, nullable=True)
    review_note = db.Column(db.String(500))
    created_at = db.Column(db.DateTime, default=datetime.now)
    __table_args__ = (
        db.UniqueConstraint('milestone_id', 'submitted_by', 'version', name='uq_csv_milestone_sub_ver'),
    )


class CampSubmissionAttachment(db.Model):
    """提交附件（MinIO 对象引用；course resources 同款 multipart 上传+代理下载）。
    结营冻结时对验收通过版本的附件打 is_asset=true——数字资产留存标记（资产回流）。"""
    __tablename__ = 'camp_submission_attachment'
    id = db.Column(db.Integer, primary_key=True)
    submission_id = db.Column(db.Integer, db.ForeignKey('camp_submission_version.id'), nullable=False, index=True)
    object_key = db.Column(db.String(255), nullable=False)
    filename = db.Column(db.String(200), nullable=False)
    size = db.Column(db.Integer)
    content_type = db.Column(db.String(100))
    is_asset = db.Column(db.Boolean, default=False)   # 资产回流打标（冻结时对 approved 版本置位）
    created_at = db.Column(db.DateTime, default=datetime.now)


class CampOutcome(db.Model):
    """项目成果：负责人登记 → admin 核验。未核验（verified）不入结营档案（纪律沿用）。"""
    __tablename__ = 'camp_outcome'
    id = db.Column(db.Integer, primary_key=True)
    unit_id = db.Column(db.Integer, db.ForeignKey('camp_unit.id'), nullable=False, index=True)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    contributor_ids = db.Column(db.Text)              # JSON 贡献者 user_id 数组
    status = db.Column(db.String(20), nullable=False, default='submitted')  # submitted/verified/rejected
    submitted_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    verified_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    verified_at = db.Column(db.DateTime, nullable=True)
    reject_reason = db.Column(db.String(500))
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)


class CampArchive(db.Model):
    """结营档案：close 迁移自动触发冻结（幂等：已有行跳过；旧 archived 营不补建，v1.3 裁定）。
    snapshot=关键事实 JSON（成员/项目/里程碑终态/已核验成果/事件计数）；此后全端点只读，
    修正走 CampArchiveRevision 版本化留痕。"""
    __tablename__ = 'camp_archive'
    id = db.Column(db.Integer, primary_key=True)
    camp_session_id = db.Column(db.Integer, db.ForeignKey('camp_session.id'), nullable=False, unique=True)
    snapshot = db.Column(db.Text)                     # JSON 快照
    version = db.Column(db.Integer, nullable=False, default=1)
    frozen_at = db.Column(db.DateTime, default=datetime.now)
    frozen_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)


class CampArchiveRevision(db.Model):
    """档案受控修正：专门权限（admin）+原因必填+期望版本→新版本递增；修正以 patch 描述留痕
    （不重写 snapshot 原文，快照不可变原则——展示层按需要叠加修订说明）。"""
    __tablename__ = 'camp_archive_revision'
    id = db.Column(db.Integer, primary_key=True)
    archive_id = db.Column(db.Integer, db.ForeignKey('camp_archive.id'), nullable=False, index=True)
    version = db.Column(db.Integer, nullable=False)   # 对应档案修正后的版本号
    reason = db.Column(db.String(500), nullable=False)
    patch = db.Column(db.Text)                        # JSON：修正内容描述
    operator_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now)


# ── 项目广场（功能扩展轮 §五，双来源展示板块）──
# camp=营期项目「发布」投影（显式动作非自动同步；发布即已审）；community=用户自由分享（免审上架+管理员下架）。
# 红线：展示不反向驱动营期流程；档案附件/模板引用不复制（引用为安全，档案冻结后不可变）。

class ShowcaseProject(db.Model):
    """展示条目：全站项目广场的统一单元。camp 条目 UQ(source, source_ref)——一个营期项目只发一条；
    project_status：community 手标（构思/进行/完成），camp 发布时随营期状态、结营冻结时自动置 done。"""
    __tablename__ = 'showcase_project'
    id = db.Column(db.Integer, primary_key=True)
    source = db.Column(db.String(20), nullable=False)            # camp / community
    source_ref = db.Column(db.Integer, nullable=True, index=True)  # camp→camp_unit.id；community 空
    owner_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    title = db.Column(db.String(120), nullable=False)
    summary = db.Column(db.String(300))                          # 列表页简介
    description = db.Column(db.Text)                             # 详情正文
    cover = db.Column(db.String(255))                            # 封面（可空，MVP 用色块兜底）
    tags = db.Column(db.Text)                                    # JSON 字符串数组
    project_status = db.Column(db.String(20), nullable=False, default='ongoing')  # idea/ongoing/done
    status = db.Column(db.String(20), nullable=False, default='visible')          # visible/hidden（治理）
    members_json = db.Column(db.Text)                            # JSON 展示成员（community 可选公开）
    links_json = db.Column(db.Text)                              # JSON 资料区链接 [{label,url}]
    archive_ref = db.Column(db.Integer, nullable=True)           # camp：结营档案 id（引用不复制）
    view_count = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)
    __table_args__ = (
        db.UniqueConstraint('source', 'source_ref', name='uq_showcase_source_ref'),
    )


class ShowcaseFavorite(db.Model):
    """项目广场收藏（复用收藏模式；个人便签性质，与志愿/互动解耦）。"""
    __tablename__ = 'showcase_favorite'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    project_id = db.Column(db.Integer, db.ForeignKey('showcase_project.id'), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.now)
    __table_args__ = (
        db.UniqueConstraint('user_id', 'project_id', name='uq_showcase_fav'),
    )


class AiTopicLedger(db.Model):
    """AI 每日话题选题账本：全局跨日去重(同一 source_url 不重复选) + 每日幂等(同一天最多 1 篇)。
    选题维度是全局 url/date、非用户级，故不复用 DiscussionReaction 的多态印记结构。"""
    __tablename__ = 'ai_topic_ledger'
    id = db.Column(db.Integer, primary_key=True)
    source_url = db.Column(db.String(500), nullable=False)
    url_hash = db.Column(db.String(64), nullable=False, unique=True)   # sha256(source_url)，跨日去重
    title = db.Column(db.String(200), nullable=True)
    picked_date = db.Column(db.Date, nullable=False, index=True)        # 选题日期；job 开头查今日是否已选
    article_v2_id = db.Column(db.Integer, db.ForeignKey('article_v2.id'), nullable=True)
    status = db.Column(db.String(20), default='published')              # published / draft / failed / skipped
    reason = db.Column(db.String(500), nullable=True)                   # LLM 选题理由，便于回查
    created_at = db.Column(db.DateTime, default=datetime.now)


# ── 社团干事身份（功能扩展轮 §四，轻量任职档案）──
# 两套体系并存：管理职位（社长/副社长/团支书/副团支书，固定枚举）+ 分组体系（组长=每组一个；
# 组员=普通组员归属，无头衔语义不进社区徽章）；
# 无任职行 = 普通社员。红线：不挂任何操作权限（权限仍走 super_admin + 营内角色），纯身份/档案语义；
# department 存组织树叶子组名（15 组名全树唯一，树固化在前端常量，后端不校验组名——换届重组只改前端）。

class ClubOfficer(db.Model):
    """社团干事任职行。卸任 = status 置 ended 不删行（appointed_by/ended_by 操作人留痕）。
    约束在应用层（blueprints/officers.py）：同 (department, title) 至多 1 条 active（社长全局唯一/
    每组一个组长；组员豁免可多行）；同一社员至多 2 条 active 且组不重复（兼两组上限）；
    社长不挂组、组长/组员必挂组。"""
    __tablename__ = 'club_officer'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    title = db.Column(db.String(20), nullable=False)              # 职位白名单：社长/副社长/团支书/副团支书/组长
    department = db.Column(db.String(50))                         # 归属组名；社长统领全局为空
    term_start = db.Column(db.Date, nullable=False)               # 任期起
    term_end = db.Column(db.Date)                                 # 任期止（空 = 在任）
    status = db.Column(db.String(20), nullable=False, default='active', index=True)  # active / ended
    appointed_by = db.Column(db.Integer)                          # 任命操作人（审计留痕，非 FK）
    ended_by = db.Column(db.Integer)                              # 卸任操作人
    end_reason = db.Column(db.String(200))                        # 卸任原因（选填）
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)

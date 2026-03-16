import os

from flask import Blueprint, request, jsonify, send_file

# 导入拓展
from exts import db, redis_client

# 导入数据库表
from models import UserModel, CourseModel, Chapter, LessonModel

# 导入表单验证
from .forms import CourseForm
from .forms import ChapterForm

# 导入token验证模块
from flask_jwt_extended import (get_jwt_identity, jwt_required)

# 导入api文档模块
from flasgger import swag_from

# 导入权限检查模块
from . import check_permission, audit_log

bp = Blueprint("course", __name__, url_prefix="")


# 发布课程
@bp.route("/course/public", methods=["POST"])
@jwt_required()
@check_permission('course_management')
@audit_log(operation="发布课程")
@swag_from('../apidocs/course/public.yaml')
def public():
    form = CourseForm()
    if form.validate():
        title = form.Course_title.data
        introduction = form.Course_Introduction.data
        chapters = form.Course_Chapters.data

        # 添加对新字段的支持（课时数自动计算，初始为0）
        difficulty = form.Course_Difficulty.data if hasattr(form, 'Course_Difficulty') else None
        other_tags = form.Course_Other_Tags.data if hasattr(form, 'Course_Other_Tags') else None

        # 从 JWT 获取当前用户
        user_email = get_jwt_identity()
        from models import UserModel
        user = UserModel.query.filter_by(email=user_email).first()

        # 初始课时数为0（后续通过添加课时自动更新）
        course = CourseModel(title=title, introduction=introduction, chapters=chapters,
                             class_hour=0, difficulty=difficulty, other_tags=other_tags,
                             creator_id=user.id if user else None)

        db.session.add(course)
        # 获取文章id
        db.session.flush()
        db.session.refresh(course)

        # filename = cover.filename
        # cover.save('./data/cover/' + str(course.id) + '.' + filename.rsplit(".", 1)[1].lower())
        # course.cover = str(course.id) + '.' + filename.rsplit(".", 1)[1].lower()
        #
        db.session.commit()

        data = {
            "code": 200,
            "message": "课程信息存储成功",
            "Course_Id": course.id,
            "Course_Title": title,
            "Course_Introduction": introduction,
        }
        return jsonify(data)

    else:
        data = {
            "code": 402,
            "message": form.errors,
        }
    return jsonify(data), 402


@bp.route("/course/edit", methods=["POST"])
@jwt_required()
@check_permission('course_management')
@audit_log(operation="编辑课程")
@swag_from('../apidocs/course/course_edit.yaml')
def course_edit():
    form = CourseForm()
    if form.validate():
        course_id = form.Course_Id.data
        course = CourseModel.query.filter_by(id=course_id).first()
        title = None
        introduction = None
        chapters = None
        tag = None
        difficulty = None
        other_tags = None

        if form.Course_title.data:
            title = form.Course_title.data
        if form.Course_Introduction.data:
            introduction = form.Course_Introduction.data
        if form.Course_Chapters.data:
            chapters = form.Course_Chapters.data
        if form.Course_Tags.data:
            tag = form.Course_Tags.data
        if form.Course_Difficulty.data:
            difficulty = form.Course_Difficulty.data
        if form.Course_Other_Tags.data:
            other_tags = form.Course_Other_Tags.data

        if title is not None:
            course.title = title
        if introduction is not None:
            course.introduction = introduction
        if chapters is not None:
            course.chapters = chapters
        if tag is not None:
            course.tags = tag
        if difficulty is not None:
            course.difficulty = difficulty
        if other_tags is not None:
            course.other_tags = other_tags

        # 重新统计课时数（自动计算，不允许手动编辑）
        lesson_count = LessonModel.query.filter_by(course_id=course_id).count()
        course.class_hour = lesson_count

        db.session.commit()

        # 获取章节列表
        chapter_list = Chapter.query.filter_by(course_id=course_id).order_by(Chapter.order).all()
        chapters_data = []
        for chapter in chapter_list:
            chapters_data.append({
                'Chapter_Id': chapter.id,
                'Chapter_Name': chapter.name,
                'Chapter_Order': chapter.order,
                'Chapter_Level': chapter.level,
                'Chapter_Parent_Id': chapter.parent_id
            })

        # 获取课时列表（带章节信息）
        lessons_data = []
        for chapter in chapter_list:
            lessons = LessonModel.query.filter_by(chapter_id=chapter.id).order_by(LessonModel.order).all()
            lessons_data.append({
                'Chapter_Id': chapter.id,
                'Chapter_Name': chapter.name,
                'Chapter_Level': chapter.level,
                'Chapter_Parent_Id': chapter.parent_id,
                'lessons': [lesson.to_dict() for lesson in lessons]
            })

        return jsonify({
            "code": 200,
            "message": "课程信息修改完成",
            "chapters": chapters_data,
            "lessons": lessons_data,
            "class_hour": lesson_count or 0
        })

    else:
        data = {
            "code": 402,
            "message": form.errors,
        }
        return jsonify(data), 402


# 展示所有课程
@bp.route("/course/list")
@swag_from('../apidocs/course/list.yaml')
def course_list():
    a_list = CourseModel.query.all()
    data = []
    for course in a_list:
        # 处理other_tags，将逗号分隔的字符串转为数组
        # 同时兼容中文逗号和英文逗号
        other_tags_list = []
        if course.other_tags:
            # 先将中文逗号替换为英文逗号，然后分割
            normalized_tags = course.other_tags.replace('，', ',')
            other_tags_list = [tag.strip() for tag in normalized_tags.split(',') if tag.strip()]
        
        b_list = {'Course_title': course.title,
                  'Course_Introduction': course.introduction,
                  'Course_Chapters': course.chapters,
                  'Course_Time': course.publish_time.strftime('%Y-%m-%d %H:%M:%S'),
                  'Course_Id': str(course.id),
                  'Course_Tags': course.tags,
                  'Course_Class_Hour': course.class_hour or 0,
                  'Course_Difficulty': course.difficulty,
                  'Course_Other_Tags': other_tags_list,
                  }
        data.append(b_list)

    return jsonify(data)


# 创建章节（全量替换）
@bp.route("/course/chapter_public", methods=["POST"])
@jwt_required()
@swag_from('../apidocs/course/chapter_public.yaml')
@audit_log(operation="发布课程章节")
def chapter_public():
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    mode = user.user_mode
    if mode != 'admin':
        return jsonify({
            "code": 400,
            'message': "用户权限不够"
        }), 400
    form = ChapterForm()
    if form.validate():
        course_id = form.Course_Id.data
        chapter_name = form.Chapter_Name.data

        courses = Chapter.query.filter_by(course_id=course_id)
        courses.delete()
        db.session.commit()

        for chapters in chapter_name:
            chapter_name = chapters["name"]
            order = chapters.get("order", 0)
            level = chapters.get("level", 1)
            parent_id = chapters.get("parent_id")  # 最顶级为 null

            chapter = Chapter(name=chapter_name, order=order, level=level, parent_id=parent_id, course_id=course_id)
            db.session.add(chapter)
            db.session.commit()

        return jsonify({
            "code": 200,
            'message': "章节上传完成"
        })
    else:
        data = {
            "code": 402,
            "message": form.errors,
        }, 402
        return jsonify(data)


# 添加章节（增量添加）
@bp.route("/course/chapter_add", methods=["POST"])
@jwt_required()
@audit_log(operation="添加课程章节")
def chapter_add():
    """增量添加章节，不删除原有章节"""
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    mode = user.user_mode
    if mode != 'admin':
        return jsonify({
            "code": 400,
            'message': "用户权限不够"
        }), 400

    course_id = request.json.get('Course_Id')
    chapter_name = request.json.get('Chapter_Name', [])

    if not course_id:
        return jsonify({"code": 400, "message": "缺少课程ID"}), 400
    if not chapter_name:
        return jsonify({"code": 400, "message": "章节数据不能为空"}), 400

    # 增量添加
    for chapter in chapter_name:
        name = chapter.get("name")
        order = chapter.get("order", 0)
        level = chapter.get("level", 1)
        parent_id = chapter.get("parent_id")  # 最顶级为 null

        if name:
            new_chapter = Chapter(name=name, order=order, level=level, parent_id=parent_id, course_id=course_id)
            db.session.add(new_chapter)

    db.session.flush()

    # 更新课程的顶级章节数量（level=1 的一级章节）
    course = CourseModel.query.filter_by(id=course_id).first()
    if course:
        top_level_chapters_count = Chapter.query.filter_by(course_id=course_id, level=1).count()
        course.chapters = top_level_chapters_count
        db.session.commit()

    return jsonify({
        "code": 200,
        "message": "章节添加成功"
    })


# 编辑单个章节
@bp.route("/course/chapter_edit", methods=["POST"])
@jwt_required()
@audit_log(operation="编辑课程章节")
def chapter_edit():
    """更新单个章节信息"""
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    mode = user.user_mode
    if mode != 'admin':
        return jsonify({
            "code": 400,
            'message': "用户权限不够"
        }), 400

    data = request.json
    chapter_id = data.get('Chapter_Id')
    name = data.get('Chapter_Name')
    order = data.get('Chapter_Order')
    level = data.get('Chapter_Level')
    parent_id = data.get('Chapter_Parent_Id')

    if not chapter_id:
        return jsonify({"code": 400, "message": "缺少章节ID"}), 400

    chapter = Chapter.query.get(chapter_id)
    if not chapter:
        return jsonify({"code": 404, "message": "章节不存在"}), 404

    if name:
        chapter.name = name
    if order is not None:
        chapter.order = order
    if level is not None:
        chapter.level = level
    # parent_id 可以设置为 null（顶级章节）或其他章节ID

    db.session.commit()

    return jsonify({
        "code": 200,
        "message": "章节更新成功"
    })


# 删除单个章节
@bp.route("/course/chapter_del", methods=["POST"])
@jwt_required()
@audit_log(operation="删除课程章节")
def chapter_delete():
    """删除单个章节"""
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    mode = user.user_mode
    if mode != 'admin':
        return jsonify({
            "code": 400,
            'message': "用户权限不够"
        }), 400

    data = request.json
    chapter_id = data.get('Chapter_Id')

    if not chapter_id:
        return jsonify({"code": 400, "message": "缺少章节ID"}), 400

    chapter = Chapter.query.get(chapter_id)
    if not chapter:
        return jsonify({"code": 404, "message": "章节不存在"}), 404

    # 递归删除子章节
    def delete_chapter_recursive(chap_id):
        # 先删除所有子章节
        child_chapters = Chapter.query.filter_by(parent_id=chap_id).all()
        for child in child_chapters:
            delete_chapter_recursive(child.id)
        # 再删除当前章节
        chapter = Chapter.query.get(chap_id)
        if chapter:
            db.session.delete(chapter)

    delete_chapter_recursive(chapter_id)
    db.session.commit()

    return jsonify({
        "code": 200,
        "message": "章节删除成功"
    })


# 查询章节详情（需要加参数，如 ?Course_Id=xxx）
@bp.route("/course/chapter_list")
@swag_from('../apidocs/course/chapter_list.yaml')
def chapter_list():
    course_id = request.args.get('Course_Id')
    if course_id is None:
        return jsonify({
            "code": 400,
            "message": "传参格式有误",
        }), 400
    a_list = Chapter.query.filter_by(course_id=course_id).order_by(Chapter.order).all()
    data = []
    for chapter in a_list:
        b_list = {'Chapter_Id': chapter.id,
                  'Chapter_Name': chapter.name,
                  'Chapter_Order': chapter.order,
                  'Chapter_Level': chapter.level,
                  'Chapter_Parent_Id': chapter.parent_id
                  }
        data.append(b_list)

    return jsonify(data)


# 删除课程
@bp.route("/course/course_delete", methods=["POST"])
@jwt_required()
@swag_from('../apidocs/course/course_delete.yaml')
@audit_log(operation="删除课程")
def course_delete():
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    mode = user.user_mode
    if mode != 'admin':
        return jsonify({
            "code": 400,
            'message': "用户权限不够"
        }), 400

    course_id = request.json.get('Course_Id')
    courses = CourseModel.query.filter_by(id=course_id).first()
    if courses is None:
        return jsonify({
            "code": 402,
            'message': "课程不存在"
        }), 402
    chapter = Chapter.query.filter_by(course_id=course_id).delete()
    db.session.delete(courses)
    db.session.commit()
    return jsonify({
        "code": 200,
        "Course_Id": course_id,
        'message': "课程删除完成"
    })


# 查询课程（需要加参数，如 ?Course_Id=xxx，?Query=xxx）
@bp.route("/course/search")
@swag_from('../apidocs/course/search_courses.yaml')
def search_courses():
    search_query = request.args.get('Query')
    if search_query:
        courses = CourseModel.query.filter(CourseModel.title.like(f'%{search_query}%')).all()
        if not courses:
            return jsonify({
                "code": 402,
                'message': "课程不存在"
            }), 402
        course_list = []
        for course in courses:
            # 处理other_tags，将逗号分隔的字符串转为数组
            # 同时兼容中文逗号和英文逗号
            other_tags_list = []
            if course.other_tags:
                # 先将中文逗号替换为英文逗号，然后分割
                normalized_tags = course.other_tags.replace('，', ',')
                other_tags_list = [tag.strip() for tag in normalized_tags.split(',') if tag.strip()]

            course_info = {
                'Course_Id': str(course.id),
                'Course_Title': course.title,
                'Introduction': course.introduction,
                'Chapters': course.chapters,
                'Course_Tags': course.tags,
                'Course_Class_Hour': course.class_hour or 0,
                'Course_Difficulty': course.difficulty,
                'Course_Other_Tags': other_tags_list,
                # 'Cover': course.cover
            }
            course_list.append(course_info)
        return jsonify({
            "code": 200,
            'message': "查询成功",
            'Course_List': course_list
        })
    course_id = request.args.get('Course_Id')
    if course_id:
        course = CourseModel.query.filter_by(id=course_id).first()
        if course is None:
            return jsonify({
                "code": 402,
                'message': "课程不存在"
            }), 402

        # 处理other_tags，将逗号分隔的字符串转为数组
        # 同时兼容中文逗号和英文逗号
        other_tags_list = []
        if course.other_tags:
            # 先将中文逗号替换为英文逗号，然后分割
            normalized_tags = course.other_tags.replace('，', ',')
            other_tags_list = [tag.strip() for tag in normalized_tags.split(',') if tag.strip()]
            
        return jsonify({
            "code": 200,
            'Course_Id': str(course.id),
            'Course_Title': course.title,
            'Introduction': course.introduction,
            'Chapters': course.chapters,
            'Course_Tags': course.tags,
            'Course_Class_Hour': course.class_hour or 0,
            'Course_Difficulty': course.difficulty,
            'Course_Other_Tags': other_tags_list,
            # 'Cover': course.cover
        })
    return jsonify({
        "code": 402,
        'message': "参数错误"
    }), 402


@bp.route("/course/book_upgrade", methods=["POST"])
@jwt_required()
@swag_from('../apidocs/course/book_upgrade.yaml')
@audit_log(operation="更新课程教材")
def book_upgrade():
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    mode = user.user_mode
    if mode != 'admin':
        return jsonify({
            "code": 400,
            'message': "用户权限不够"
        }), 400

    book = request.files['Book']
    course_id = request.form.get('Course_Id')
    if book is None:  # 表示没有发送文件
        return jsonify({
            "code": 402,
            'message': "没有发送文件"
        }), 402

    if course_id is None:  # 表示没有发送课程 ID
        return jsonify({
            "code": 402,
            'message': "没有发送课程 ID"
        }), 402

    course = CourseModel.query.filter_by(id=course_id).first()
    url = course.url
    if url:
        os.remove('./data/course/book/' + url)

    book_name = str(course.id) + '_' + course.title + '.zip'
    book.save('./data/course/book/' + book_name)

    # 更新课程的 url
    course.url = book_name
    db.session.commit()

    return jsonify({
        "code": 200,
        'message': "文件保存成功",
        'book_name': book_name
    })


import random
import string
import uuid
import hashlib


@bp.route("/course/book_down")
@jwt_required()
@swag_from('../apidocs/course/book_down.yaml')
def book_down():
    course_id = request.args.get('Course_Id')
    if course_id:
        course = CourseModel.query.filter_by(id=course_id).first()
        url = course.url
        if url is None:
            return jsonify({
                "code": 402,
                'message': "课程pdf不存在"
            }), 402
        """生成下载码路由（包含所有逻辑）"""
        # 获取客户端IP
        if request.headers.getlist("X-Forwarded-For"):
            ip = request.headers.getlist("X-Forwarded-For")[0]
        else:
            ip = request.remote_addr
        # 生成下载码
        code = hashlib.sha256(f"{uuid.uuid4()}{ip}{course_id}".encode()).hexdigest()[:16]
        # 存储到Redis，10秒过期
        redis_client.setex(f"download_code:{code}", 100, f"{ip}:{course_id}")

        return jsonify({
            "code": 200,
            'message': "下载链接生成成功",
            'Down_Code': code
        })

    else:
        return jsonify({
            "code": 402,
            'message': "参数错误"
        })


@bp.route("/course/book_download")
@swag_from('../apidocs/course/book_download.yaml')
def book_download():
    Down_Code = request.args.get('Down_Code')
    if Down_Code:
        # 获取客户端IP
        if request.headers.getlist("X-Forwarded-For"):
            ip = request.headers.getlist("X-Forwarded-For")[0]
        else:
            ip = request.remote_addr
        # 验证下载码
        stored_value = redis_client.get(f"download_code:{Down_Code}")
        if stored_value is None:
            return jsonify({
                "code": 402,
               'message': "下载码不存在或已过期"
            })
        stored_ip, stored_course_id = stored_value.decode('utf-8').split(':')
        # print(stored_ip, stored_course_id)
        if stored_ip != ip :
            return jsonify({
                "code": 402,
                'message': "下载码错误"
            })

        course_id = stored_course_id
        course = CourseModel.query.filter_by(id=course_id).first()
        url = course.url
        if url is None:
            return jsonify({
                "code": 403,
                'message': "课程pdf不存在"
            }), 403

        return send_file('./data/course/book/' + url, as_attachment=True)
    else:
        return jsonify({
            "code": 404,
            'message': "参数错误"
        })


# ==================== 课时管理 API ====================

@bp.route("/course/lesson/add", methods=["POST"])
@jwt_required()
@swag_from('../apidocs/course/lesson_add.yaml')
def lesson_add():
    """添加课时"""
    from .forms import LessonForm

    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()

    form = LessonForm()
    if not form.validate():
        return jsonify({"code": 402, "message": form.errors}), 402

    course_id = form.Course_Id.data
    chapter_id = form.Chapter_Id.data
    title = form.Lesson_Title.data
    lesson_type = form.Lesson_Type.data

    # 验证课程存在
    course = CourseModel.query.filter_by(id=course_id).first()
    if not course:
        return jsonify({"code": 404, "message": "课程不存在"}), 404

    # 权限检查：课程创建者或管理员可添加课时
    if course.creator_id != user.id and user.user_mode != 'admin':
        return jsonify({"code": 403, "message": "无课程管理权限"}), 403

    # 验证章节存在且属于该课程
    chapter = Chapter.query.filter_by(id=chapter_id, course_id=course_id).first()
    if not chapter:
        return jsonify({"code": 404, "message": "章节不存在"}), 404

    # 验证课时类型
    valid_types = ['video', 'text', 'link', 'quiz', 'homework']
    if lesson_type not in valid_types:
        return jsonify({"code": 400, "message": f"课时类型必须为: {', '.join(valid_types)}"}), 400

    # 创建课时
    lesson = LessonModel(
        chapter_id=chapter_id,
        course_id=course_id,
        title=title,
        type=lesson_type,
        content=form.Lesson_Content.data or '',
        duration=form.Lesson_Duration.data if form.Lesson_Duration.data is not None else 0,
        order=form.Lesson_Order.data if form.Lesson_Order.data is not None else 0,
        resource_url=form.Resource_Url.data
    )

    db.session.add(lesson)
    db.session.flush()

    # 更新课程总学时（根据所有课时的 duration 之和计算，单位：分钟）
    total_duration = db.session.query(db.func.sum(LessonModel.duration)).filter(
        LessonModel.course_id == course_id
    ).scalar() or 0
    course.class_hour = total_duration
    db.session.commit()

    return jsonify({
        "code": 200,
        "message": "课时添加成功",
        "lesson": lesson.to_dict()
    })


@bp.route("/course/lesson/edit", methods=["POST"])
@jwt_required()
@swag_from('../apidocs/course/lesson_edit.yaml')
def lesson_edit():
    """编辑课时"""
    from .forms import LessonForm

    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()

    lesson_id = request.json.get('lesson_id')
    if not lesson_id:
        return jsonify({"code": 400, "message": "缺少课时ID"}), 400

    lesson = LessonModel.query.filter_by(id=lesson_id).first()
    if not lesson:
        return jsonify({"code": 404, "message": "课时不存在"}), 404

    # 权限检查：课程创建者或管理员可编辑
    course = CourseModel.query.filter_by(id=lesson.course_id).first()
    if course.creator_id != user.id and user.user_mode != 'admin':
        return jsonify({"code": 403, "message": "无课程管理权限"}), 403

    form = LessonForm()
    if not form.validate():
        return jsonify({"code": 402, "message": form.errors}), 402

    # 可更新的字段
    if form.Lesson_Title.data:
        lesson.title = form.Lesson_Title.data
    if form.Lesson_Type.data:
        valid_types = ['video', 'text', 'link', 'quiz', 'homework']
        if form.Lesson_Type.data not in valid_types:
            return jsonify({"code": 400, "message": f"课时类型必须为: {', '.join(valid_types)}"}), 400
        lesson.type = form.Lesson_Type.data
    if form.Lesson_Content.data is not None:
        lesson.content = form.Lesson_Content.data
    if form.Lesson_Duration.data is not None:
        lesson.duration = form.Lesson_Duration.data
    if form.Lesson_Order.data is not None:
        lesson.order = form.Lesson_Order.data
    if form.Resource_Url.data is not None:
        lesson.resource_url = form.Resource_Url.data

    db.session.flush()

    # 更新课程总学时（根据所有课时的 duration 之和计算）
    total_duration = db.session.query(db.func.sum(LessonModel.duration)).filter(
        LessonModel.course_id == course.id
    ).scalar() or 0
    course.class_hour = total_duration
    db.session.commit()

    return jsonify({
        "code": 200,
        "message": "课时更新成功",
        "lesson": lesson.to_dict()
    })


@bp.route("/course/lesson/delete", methods=["POST"])
@jwt_required()
@swag_from('../apidocs/course/lesson_delete.yaml')
def lesson_delete():
    """删除课时"""
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()

    lesson_id = request.json.get('lesson_id')
    if not lesson_id:
        return jsonify({"code": 400, "message": "缺少课时ID"}), 400

    lesson = LessonModel.query.filter_by(id=lesson_id).first()
    if not lesson:
        return jsonify({"code": 404, "message": "课时不存在"}), 404

    # 权限检查：课程创建者或管理员可删除
    course = CourseModel.query.filter_by(id=lesson.course_id).first()
    if course.creator_id != user.id and user.user_mode != 'admin':
        return jsonify({"code": 403, "message": "无课程管理权限"}), 403

    db.session.delete(lesson)
    db.session.flush()

    # 更新课程总学时（根据所有课时的 duration 之和计算）
    total_duration = db.session.query(db.func.sum(LessonModel.duration)).filter(
        LessonModel.course_id == course.id
    ).scalar() or 0
    course.class_hour = total_duration
    db.session.commit()

    return jsonify({
        "code": 200,
        "message": "课时删除成功"
    })


@bp.route("/course/lesson/list")
@swag_from('../apidocs/course/lesson_list.yaml')
def lesson_list():
    """获取课程下的所有课时"""
    course_id = request.args.get('Course_Id')
    if not course_id:
        return jsonify({"code": 400, "message": "缺少课程ID"}), 400

    course = CourseModel.query.filter_by(id=course_id).first()
    if not course:
        return jsonify({"code": 404, "message": "课程不存在"}), 404

    # 获取所有章节
    chapters = Chapter.query.filter_by(course_id=course_id).order_by(Chapter.order).all()

    result = []
    for chapter in chapters:
        # 获取该章节下的所有课时
        lessons = LessonModel.query.filter_by(chapter_id=chapter.id).order_by(LessonModel.order).all()

        chapter_data = {
            'Chapter_Id': chapter.id,
            'Chapter_Name': chapter.name,
            'Chapter_Order': chapter.order,
            'Chapter_Level': chapter.level,
            'Chapter_Parent_Id': chapter.parent_id,
            'lessons': [lesson.to_dict() for lesson in lessons]
        }
        result.append(chapter_data)

    return jsonify({
        "code": 200,
        "message": "查询成功",
        "data": result
    })


@bp.route("/course/lesson/detail")
@swag_from('../apidocs/course/lesson_detail.yaml')
def lesson_detail():
    """获取单个课时详情"""
    lesson_id = request.args.get('Lesson_Id')
    if not lesson_id:
        return jsonify({"code": 400, "message": "缺少课时ID"}), 400

    lesson = LessonModel.query.filter_by(id=lesson_id).first()
    if not lesson:
        return jsonify({"code": 404, "message": "课时不存在"}), 404

    return jsonify({
        "code": 200,
        "message": "查询成功",
        "lesson": lesson.to_dict()
    })

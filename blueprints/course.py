import os

from flask import Blueprint, request, jsonify, send_file

# 导入拓展
from exts import db, redis_client

# 导入数据库表
from models import UserModel, CourseModel, Chapter

# 导入表单验证
from .forms import CourseForm
from .forms import ChapterForm

# 导入token验证模块
from flask_jwt_extended import (get_jwt_identity, jwt_required)

# 导入api文档模块
from flasgger import swag_from

bp = Blueprint("course", __name__, url_prefix="")


# 发布课程
@bp.route("/course/public", methods=["POST"])
@jwt_required()
@swag_from('../apidocs/course/public.yaml')
def public():
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    mode = user.user_mode
    if mode != 'admin':
        return jsonify({
            "code": 400,
            'message': "用户权限不够"
        }), 400
    form = CourseForm()
    if form.validate():
        title = form.Course_title.data
        introduction = form.Course_Introduction.data
        chapters = form.Course_Chapters.data
        # cover_ = CourseForm(request.files)

        # cover = cover_.Cover.data

        # print(cover)
        # print(title)

        course = CourseModel(title=title, introduction=introduction, chapters=chapters)

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
            "code": 401,
            "message": form.errors,
        }
    return jsonify(data), 401


@bp.route("/course/edit", methods=["POST"])
@jwt_required()
@swag_from('../apidocs/course/course_edit.yaml')
def course_edit():
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    mode = user.user_mode
    if mode != 'admin':
        return jsonify({
            "code": 400,
            'message': "用户权限不够"
        }), 400

    form = CourseForm()
    if form.validate():
        course_id = form.Course_Id.data
        course = CourseModel.query.filter_by(id=course_id).first()
        title = None
        introduction = None
        chapters = None
        tag = None

        if form.Course_title.data:
            title = form.Course_title.data
        if form.Course_Introduction.data:
            introduction = form.Course_Introduction.data
        if form.Course_Chapters.data:
            chapters = form.Course_Chapters.data
        if form.Course_Tags.data:
            tag = form.Course_Tags.data

        if title is not None:
            course.title = title
        if introduction is not None:
            course.introduction = introduction
        if chapters is not None:
            course.chapters = chapters
        if tag is not None:
            course.tags = tag

        db.session.commit()

        return jsonify({
            "code": 200,
            "message": "课程信息修改完成"
        })

    else:
        data = {
            "code": 401,
            "message": form.errors,
        }
        return jsonify(data), 401


# 展示所有课程
@bp.route("/course/list")
@swag_from('../apidocs/course/list.yaml')
def course_list():
    a_list = CourseModel.query.all()
    data = []
    for course in a_list:
        b_list = {'Course_title': course.title,
                  'Course_Introduction': course.introduction,
                  'Course_Chapters': course.chapters,
                  'Course_Time': course.publish_time.strftime('%Y-%m-%d %H:%M:%S'),
                  'Course_Id': str(course.id),
                  'Course_Tags': course.tags,
                  }
        data.append(b_list)

    return jsonify(data)


# 创建章节
@bp.route("/course/chapter_public", methods=["POST"])
@jwt_required()
@swag_from('../apidocs/course/chapter_public.yaml')
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
            chapter = chapters["name"]
            order = chapters["order"]
            priority = chapters["priority"]

            chapter = Chapter(name=chapter, order=order, course_id=course_id, priority=priority)
            db.session.add(chapter)
            db.session.commit()

        return jsonify({
            "code": 200,
            'message': "章节上传完成"
        })
    else:
        data = {
            "code": 401,
            "message": form.errors,
        }, 401
        return jsonify(data)


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
    if not a_list:
        return jsonify({
            "code": 401,
            "message": "课程不存在",
        }), 401
    data = []
    for chapter in a_list:
        b_list = {'Chapter_Name': chapter.name,
                  'Chapter_Order': chapter.order,
                  'Chapter_Priority': chapter.priority
                  }
        data.append(b_list)

    return jsonify(data)


# 删除课程
@bp.route("/course/course_delete", methods=["POST"])
@jwt_required()
@swag_from('../apidocs/course/course_delete.yaml')
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
            "code": 401,
            'message': "课程不存在"
        }), 401
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
                "code": 401,
                'message': "课程不存在"
            }), 401
        course_list = []
        for course in courses:
            course_info = {
                'Course_Id': str(course.id),
                'Course_Title': course.title,
                'Introduction': course.introduction,
                'Chapters': course.chapters,
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
                "code": 401,
                'message': "课程不存在"
            }), 401
        return jsonify({
            "code": 200,
            'Course_Id': str(course.id),
            'Course_Title': course.title,
            'Introduction': course.introduction,
            'Chapters': course.chapters,
            'Course_Tags': course.tags,
            # 'Cover': course.cover
        })
    return jsonify({
        "code": 402,
        'message': "参数错误"
    }), 402


@bp.route("/course/book_upgrade", methods=["POST"])
@jwt_required()
@swag_from('../apidocs/course/book_upgrade.yaml')
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
            "code": 401,
            'message': "没有发送文件"
        }), 401

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
                "code": 401,
                'message': "课程pdf不存在"
            }), 401
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
                "code": 401,
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

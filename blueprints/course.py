from flask import Blueprint, request, jsonify, send_file

# 导入拓展
from exts import db

# 导入数据库表
from models import UserModel, CourseModel, Chapter

# 导入表单验证
from .forms import CourseForm
from.forms import ChapterForm

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
    form = CourseForm(request.form)
    if form.validate():
        title = form.Course_title.data
        introduction = form.Course_Introduction.data
        chapters = form.Course_Chapters.data
        cover_ = CourseForm(request.files)

        cover = cover_.Cover.data

        # print(cover)
        # print(title)

        course = CourseModel(title=title, introduction=introduction, chapters=chapters)

        db.session.add(course)
        # 获取文章id
        db.session.flush()
        db.session.refresh(course)

        filename = cover.filename
        cover.save('./data/cover/' + str(course.id) + '.' + filename.rsplit(".", 1)[1].lower())
        course.cover = str(course.id) + '.' + filename.rsplit(".", 1)[1].lower()

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

    form = ChapterForm()
    if form.validate():
        course_id = form.Course_Id.data
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
            # 'Cover': course.cover
        })
    return jsonify({
        "code": 402,
        'message': "参数错误"
    }), 402

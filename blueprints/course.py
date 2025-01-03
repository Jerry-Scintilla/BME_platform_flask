from flask import Blueprint, request, jsonify, send_file

# 导入拓展
from exts import db

# 导入数据库表
from models import UserModel, CourseModel

# 导入表单验证
from .forms import CourseForm

# 导入token验证模块
from flask_jwt_extended import (get_jwt_identity, jwt_required)

# 导入api文档模块
from flasgger import swag_from

bp = Blueprint("course", __name__, url_prefix="")


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
        })
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
    return jsonify(data)


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
                  'Course_Id': course.id,
                  }
        data.append(b_list)

    return jsonify(data)

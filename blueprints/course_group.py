from flask import Blueprint, request, jsonify

from exts import db

from models import UserModel, CourseModel, CourseGroup, CourseGroupMember, LearningProgressModel
from flask_jwt_extended import get_jwt_identity, jwt_required
from flasgger import swag_from
from . import check_permission

bp = Blueprint("course_group", __name__, url_prefix="")


# 检查学生是否已加入课程小组
@bp.route("/course-group/check", methods=["GET"])
@jwt_required()
def check_enrollment():
    """
    检查当前用户是否已加入指定课程的小组
    参数: course_id
    """
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    if not user:
        return jsonify({"code": 404, "message": "用户不存在"}), 404

    course_id = request.args.get('course_id')
    if not course_id:
        return jsonify({"code": 400, "message": "课程ID不能为空"}), 400

    # 查找该用户是否在该课程有小组
    membership = db.session.query(CourseGroupMember).join(
        CourseGroup, CourseGroupMember.group_id == CourseGroup.id
    ).filter(
        CourseGroupMember.student_id == user.id,
        CourseGroup.course_id == course_id
    ).first()

    if not membership:
        return jsonify({
            "code": 200,
            "enrolled": False,
            "group": None
        })

    group = membership.group
    return jsonify({
        "code": 200,
        "enrolled": True,
        "group": {
            "id": group.id,
            "name": group.name,
            "course_id": group.course_id,
            "teacher_id": group.teacher_id,
            "teacher_name": group.teacher.username if group.teacher else ""
        }
    })


# 获取课程小组详情
@bp.route("/course-group/<int:group_id>", methods=["GET"])
@jwt_required()
def get_group_detail(group_id):
    """
    获取课程小组详情
    """
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    if not user:
        return jsonify({"code": 404, "message": "用户不存在"}), 404

    group = CourseGroup.query.get(group_id)
    if not group:
        return jsonify({"code": 404, "message": "小组不存在"}), 404

    # 获取成员列表及进度
    members_data = []
    for member in group.members:
        # 获取该学生的学习进度
        progress = LearningProgressModel.query.filter_by(
            user_id=member.student_id,
            course_id=group.course_id
        ).first()

        members_data.append({
            "student_id": member.student_id,
            "student_name": member.student.username if member.student else "",
            "progress": progress.progress if progress else 0,
            "last_activity": progress.updated_at.strftime('%Y-%m-%d') if progress and progress.updated_at else None
        })

    return jsonify({
        "code": 200,
        "group": {
            "id": group.id,
            "name": group.name,
            "course_id": group.course_id,
            "teacher": {
                "id": group.teacher_id,
                "name": group.teacher.username if group.teacher else ""
            },
            "members": members_data
        }
    })


# 导师创建/修改小组
@bp.route("/course-group", methods=["POST"])
@jwt_required()
@check_permission('course_management')
def create_or_update_group():
    """
    创建或修改课程小组
    请求参数:
    {
        "name": "小组名称",
        "course_id": 1,
        "student_ids": [1,2,3]
    }
    """
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    if not user:
        return jsonify({"code": 404, "message": "用户不存在"}), 404

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"code": 400, "message": "请求参数错误"}), 400

    group_name = data.get('name')
    course_id = data.get('course_id')
    student_ids = data.get('student_ids', [])

    if not group_name or not course_id:
        return jsonify({"code": 400, "message": "小组名称和课程ID不能为空"}), 400

    # 验证课程是否存在
    course = CourseModel.query.get(course_id)
    if not course:
        return jsonify({"code": 404, "message": "课程不存在"}), 404

    # 检查是否已存在该课程的小组（每课程一个小组）
    existing_group = CourseGroup.query.filter_by(
        course_id=course_id,
        teacher_id=user.id
    ).first()

    if existing_group:
        # 更新现有小组
        existing_group.name = group_name
        # 清除旧成员
        CourseGroupMember.query.filter_by(group_id=existing_group.id).delete()
        group = existing_group
    else:
        # 创建新小组
        group = CourseGroup(
            name=group_name,
            course_id=course_id,
            teacher_id=user.id
        )
        db.session.add(group)
        db.session.flush()

    # 添加新成员
    for student_id in student_ids:
        student = UserModel.query.get(student_id)
        if student:
            member = CourseGroupMember(
                group_id=group.id,
                student_id=student_id
            )
            db.session.add(member)

    db.session.commit()

    return jsonify({
        "code": 200,
        "message": "小组创建/更新成功",
        "group": {
            "id": group.id,
            "name": group.name,
            "course_id": group.course_id
        }
    })


# 获取小组成员学习进度
@bp.route("/course-group/<int:group_id>/progress", methods=["GET"])
@jwt_required()
def get_group_progress(group_id):
    """
    获取小组所有成员的学习进度
    """
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    if not user:
        return jsonify({"code": 404, "message": "用户不存在"}), 404

    group = CourseGroup.query.get(group_id)
    if not group:
        return jsonify({"code": 404, "message": "小组不存在"}), 404

    # 获取成员列表及进度
    members_data = []
    for member in group.members:
        progress = LearningProgressModel.query.filter_by(
            user_id=member.student_id,
            course_id=group.course_id
        ).first()

        members_data.append({
            "student_id": member.student_id,
            "student_name": member.student.username if member.student else "",
            "progress": progress.progress if progress else 0,
            "chapter_num": progress.chapter_num if progress else 0,
            "section_num": progress.section_num if progress else 0,
            "last_activity": progress.updated_at.strftime('%Y-%m-%d %H:%M') if progress and progress.updated_at else None
        })

    return jsonify({
        "code": 200,
        "progress": members_data
    })

from flask import Blueprint, request, jsonify
from flask_cors import cross_origin
from sqlalchemy import and_

from exts import db

from models import UserModel, CourseModel, CourseGroup, CourseGroupMember, LearningProgressModel, UserCourseModel
from flask_jwt_extended import get_jwt_identity, jwt_required
from flasgger import swag_from
from . import check_permission

bp = Blueprint("course_group", __name__, url_prefix="/course-groups")

# 为所有路由添加 CORS 支持
_cors_config = {
    "origins": "*",
    "methods": ["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    "allow_headers": ["Content-Type", "Authorization"]
}


# OPTIONS 预检请求处理
@bp.route('', defaults={'path': ''}, methods=['OPTIONS'])
@bp.route('/<path:path>', methods=['OPTIONS'])
@cross_origin(**_cors_config)
def options_handler(path):
    return jsonify({"code": 200}), 200


# ==================== 小组 CRUD ====================

# 创建小组 POST /course-groups
@bp.route("", methods=["POST"])
@jwt_required()
def create_group():
    """
    创建小组
    请求体: { "name": "xxx", "course_id": 1, "student_limit": 30, "status": "active" }
    """
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    if not user:
        return jsonify({"code": 404, "message": "用户不存在"}), 404

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"code": 400, "message": "请求参数错误"}), 400

    name = data.get('name')
    course_id = data.get('course_id')
    student_limit = data.get('student_limit', 30)
    status = data.get('status', 'active')
    term = data.get('term', '2026-spring')  # 默认学期

    if not name or not course_id:
        return jsonify({"code": 400, "message": "名称和课程ID不能为空"}), 400

    # 校验课程存在
    course = CourseModel.query.get(course_id)
    if not course:
        return jsonify({"code": 404, "message": "课程不存在"}), 404

    # 校验状态枚举
    valid_status = ['active', 'completed', 'paused']
    if status not in valid_status:
        return jsonify({"code": 400, "message": f"状态必须为: {', '.join(valid_status)}"}), 400

    # 校验 student_limit > 0
    if student_limit <= 0:
        return jsonify({"code": 400, "message": "student_limit 必须大于 0"}), 400

    # 校验 (course_id, teacher_id, term) 不重复
    existing = CourseGroup.query.filter_by(
        course_id=course_id,
        teacher_id=user.id,
        term=term
    ).first()
    if existing:
        return jsonify({"code": 409, "message": f"该课程在此学期({term})已存在您的小组"}), 409

    # 创建小组
    group = CourseGroup(
        name=name,
        course_id=course_id,
        teacher_id=user.id,
        term=term,
        student_limit=student_limit,
        status=status
    )
    db.session.add(group)
    db.session.commit()

    return jsonify({
        "code": 201,
        "message": "created",
        "data": {
            "id": group.id,
            "name": group.name,
            "course_id": group.course_id,
            "teacher_id": group.teacher_id,
            "term": group.term,
            "student_limit": group.student_limit,
            "status": group.status
        }
    }), 201



# 小组列表 GET /course-groups
@bp.route("", methods=["GET"])
@jwt_required()
def list_groups():
    """
    获取小组列表
    参数: course_id, teacher_id, status, mine
    mine: learning(我学习的) / teaching(我教学的) / all(全部)
    """
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    if not user:
        return jsonify({"code": 404, "message": "用户不存在"}), 404

    course_id = request.args.get('course_id')
    teacher_id = request.args.get('teacher_id')
    status = request.args.get('status')
    mine = request.args.get('mine')  # learning / teaching / all

    # 校验 mine 参数
    if mine not in (None, 'learning', 'teaching', 'all'):
        return jsonify({"code": 400, "message": "mine must be learning|teaching|all"}), 400

    query = CourseGroup.query

    # mine 参数过滤
    if mine == 'teaching':
        query = query.filter(CourseGroup.teacher_id == user.id)
    elif mine == 'learning':
        query = query.join(
            CourseGroupMember,
            and_(
                CourseGroupMember.group_id == CourseGroup.id,
                CourseGroupMember.course_id == CourseGroup.course_id
            )
        ).filter(CourseGroupMember.student_id == user.id).distinct()

    if course_id:
        query = query.filter_by(course_id=course_id)
    if teacher_id:
        query = query.filter_by(teacher_id=teacher_id)
    if status:
        query = query.filter_by(status=status)

    groups = query.order_by(CourseGroup.created_at.desc()).all()

    result = []
    for group in groups:
        result.append({
            "id": group.id,
            "name": group.name,
            "course_id": group.course_id,
            "teacher_id": group.teacher_id,
            "term": group.term,
            "member_count": len(group.members),
            "student_limit": group.student_limit,
            "status": group.status
        })

    return jsonify({
        "code": 200,
        "data": result,
        "total": len(result)
    })


# 小组详情 GET /course-groups/{group_id}
@bp.route("/<int:group_id>", methods=["GET"])
@jwt_required()
def get_group_detail(group_id):
    """
    获取小组详情
    """
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    if not user:
        return jsonify({"code": 404, "message": "用户不存在"}), 404

    group = CourseGroup.query.get(group_id)
    if not group:
        return jsonify({"code": 404, "message": "小组不存在"}), 404

    members_data = []
    for member in group.members:
        members_data.append({
            "student_id": member.student_id,
            "student_name": member.student.username if member.student else "",
            "joined_at": member.joined_at.strftime('%Y-%m-%d %H:%M:%S') if member.joined_at else None
        })

    return jsonify({
        "code": 200,
        "data": {
            "id": group.id,
            "name": group.name,
            "course_id": group.course_id,
            "term": group.term,
            "teacher": {
                "id": group.teacher_id,
                "name": group.teacher.username if group.teacher else ""
            },
            "members": members_data
        }
    })


# 更新小组 PUT /course-groups/{group_id}
@bp.route("/<int:group_id>", methods=["PUT"])
@jwt_required()
def update_group(group_id):
    """
    更新小组信息（不含成员）
    请求体: { "name": "xxx", "student_limit": 35, "status": "active" }
    """
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    if not user:
        return jsonify({"code": 404, "message": "用户不存在"}), 404

    group = CourseGroup.query.get(group_id)
    if not group:
        return jsonify({"code": 404, "message": "小组不存在"}), 404

    # 权限检查：仅组老师可更新
    if group.teacher_id != user.id:
        return jsonify({"code": 403, "message": "无权限操作"}), 403

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"code": 400, "message": "请求参数错误"}), 400

    # 更新字段
    if 'name' in data:
        group.name = data['name']
    if 'student_limit' in data:
        # 校验 student_limit >= 当前成员数
        if data['student_limit'] < len(group.members):
            return jsonify({"code": 400, "message": f"人数限制不能少于当前成员数({len(group.members)})"}), 400
        group.student_limit = data['student_limit']
    if 'status' in data:
        valid_status = ['active', 'completed', 'paused']
        if data['status'] not in valid_status:
            return jsonify({"code": 400, "message": f"状态必须为: {', '.join(valid_status)}"}), 400
        group.status = data['status']

    db.session.commit()

    return jsonify({
        "code": 200,
        "message": "updated",
        "data": {
            "id": group.id,
            "name": group.name,
            "course_id": group.course_id,
            "teacher_id": group.teacher_id,
            "term": group.term,
            "student_limit": group.student_limit,
            "status": group.status
        }
    })


# 删除小组 DELETE /course-groups/{group_id}
@bp.route("/<int:group_id>", methods=["DELETE"])
@jwt_required()
def delete_group(group_id):
    """
    删除小组
    """
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    if not user:
        return jsonify({"code": 404, "message": "用户不存在"}), 404

    group = CourseGroup.query.get(group_id)
    if not group:
        return jsonify({"code": 404, "message": "小组不存在"}), 404

    # 权限检查
    if group.teacher_id != user.id:
        return jsonify({"code": 403, "message": "无权限删除此小组"}), 403

    # 删除成员
    CourseGroupMember.query.filter_by(group_id=group_id).delete()
    # 删除小组
    db.session.delete(group)
    db.session.commit()

    return jsonify({
        "code": 200,
        "message": "deleted"
    })


# ==================== 成员管理 ====================

# 老师添加成员 POST /course-groups/{group_id}/members
@bp.route("/<int:group_id>/members", methods=["POST"])
@jwt_required()
def add_member(group_id):
    """
    老师添加成员
    请求体: { "student_ids": [1, 2, 3] }
    """
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    if not user:
        return jsonify({"code": 404, "message": "用户不存在"}), 404

    group = CourseGroup.query.get(group_id)
    if not group:
        return jsonify({"code": 404, "message": "小组不存在"}), 404

    # 权限检查
    if group.teacher_id != user.id:
        return jsonify({"code": 403, "message": "无权限操作"}), 403

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"code": 400, "message": "请求参数错误"}), 400

    student_ids = data.get('student_ids', [])
    if not student_ids:
        return jsonify({"code": 400, "message": "学生ID不能为空"}), 400

    # 检查人数限制
    current_count = len(group.members)
    if current_count + len(student_ids) > group.student_limit:
        return jsonify({"code": 400, "message": f"超出小组人数限制（{group.student_limit}人）"}), 400

    added = []
    skip_not_enrolled = []
    skip_already_in_other_group = []

    for student_id in student_ids:
        # 1. 校验学生已选修该课程
        enrollment = UserCourseModel.query.filter_by(
            user_id=student_id,
            course_id=group.course_id,
            status=UserCourseModel.STATUS_ACTIVE
        ).first()
        if not enrollment:
            skip_not_enrolled.append(student_id)
            continue

        # 2. 校验学生未在该课程其他组中（业务层返回 409）
        existing = CourseGroupMember.query.filter_by(
            student_id=student_id,
            course_id=group.course_id
        ).first()
        if existing:
            skip_already_in_other_group.append(student_id)
            continue

        # 添加成员
        member = CourseGroupMember(
            group_id=group_id,
            course_id=group.course_id,
            student_id=student_id
        )
        db.session.add(member)
        added.append(student_id)

    db.session.commit()

    return jsonify({
        "code": 200,
        "added": added,
        "skipped_not_enrolled": skip_not_enrolled,
        "skipped_already_in_other_group": skip_already_in_other_group
    })


# 老师移除成员 DELETE /course-groups/{group_id}/members/{student_id}
@bp.route("/<int:group_id>/members/<int:student_id>", methods=["DELETE"])
@jwt_required()
def remove_member(group_id, student_id):
    """
    老师移除成员
    """
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    if not user:
        return jsonify({"code": 404, "message": "用户不存在"}), 404

    group = CourseGroup.query.get(group_id)
    if not group:
        return jsonify({"code": 404, "message": "小组不存在"}), 404

    # 权限检查
    if group.teacher_id != user.id:
        return jsonify({"code": 403, "message": "无权限操作"}), 403

    # 查找成员
    member = CourseGroupMember.query.filter_by(
        group_id=group_id,
        student_id=student_id
    ).first()

    if not member:
        return jsonify({"code": 404, "message": "成员不存在"}), 404

    db.session.delete(member)
    db.session.commit()

    return jsonify({
        "code": 200,
        "message": "removed"
    })


# ==================== 用户自助加入/退出 ====================

# 用户加入小组 POST /course-groups/{group_id}/join
@bp.route("/<int:group_id>/join", methods=["POST"])
@jwt_required()
def join_group(group_id):
    """
    当前登录学生加入指定小组
    """
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    if not user:
        return jsonify({"code": 404, "message": "用户不存在"}), 404

    # 1. 校验小组存在且状态为 active
    group = CourseGroup.query.get(group_id)
    if not group or group.status != 'active':
        return jsonify({"code": 404, "message": "小组不存在或不可加入"}), 404

    # 2. 校验已选修该课程
    enrollment = UserCourseModel.query.filter_by(
        user_id=user.id,
        course_id=group.course_id,
        status=UserCourseModel.STATUS_ACTIVE
    ).first()
    if not enrollment:
        return jsonify({"code": 422, "message": "未选修该课程，无法加入小组"}), 422

    # 3. 校验未加入该课程其他小组
    existing = CourseGroupMember.query.filter_by(
        student_id=user.id,
        course_id=group.course_id
    ).first()
    if existing:
        return jsonify({"code": 409, "message": "已加入该课程的其他小组"}), 409

    # 4. 校验小组未满员
    if len(group.members) >= group.student_limit:
        return jsonify({"code": 422, "message": "小组人数已满"}), 422

    # 添加成员
    member = CourseGroupMember(
        group_id=group.id,
        course_id=group.course_id,
        student_id=user.id
    )
    db.session.add(member)
    db.session.commit()

    return jsonify({
        "code": 200,
        "message": "joined",
        "data": {
            "group_id": group.id,
            "course_id": group.course_id,
            "student_id": user.id
        }
    })


# 用户退出小组 POST /course-groups/{group_id}/leave
@bp.route("/<int:group_id>/leave", methods=["POST"])
@jwt_required()
def leave_group(group_id):
    """
    当前登录学生退出指定小组
    """
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    if not user:
        return jsonify({"code": 404, "message": "用户不存在"}), 404

    # 校验在组内
    member = CourseGroupMember.query.filter_by(
        group_id=group_id,
        student_id=user.id
    ).first()

    if not member:
        return jsonify({"code": 404, "message": "未在该小组中"}), 404

    db.session.delete(member)
    db.session.commit()

    return jsonify({
        "code": 200,
        "message": "left"
    })


# ==================== 其他接口（兼容旧版） ====================

# 检查学生是否已加入课程小组（兼容旧版 /course-group/check）
@bp.route("/check", methods=["GET"])
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

    membership = CourseGroupMember.query.filter_by(
        student_id=user.id,
        course_id=course_id
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
            "term": group.term,
            "teacher_name": group.teacher.username if group.teacher else ""
        }
    })


# 获取小组学习进度（兼容旧版）
@bp.route("/<int:group_id>/progress", methods=["GET"])
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

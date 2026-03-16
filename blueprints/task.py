from flask import Blueprint, request, jsonify, send_file, current_app
import os
from flask_cors import cross_origin
from sqlalchemy import and_
from datetime import datetime

from exts import db

from models import UserModel, CourseModel, CourseGroup, Task, TaskAssignee, TaskSubmission, TaskSubmissionAttachment, CourseGroupMember, UserCourseModel
from flask_jwt_extended import get_jwt_identity, jwt_required
from . import check_permission

bp = Blueprint("task", __name__, url_prefix="/tasks")

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


# ==================== 任务 CRUD ====================

# 创建任务 POST /tasks
@bp.route("", methods=["POST"])
@jwt_required()
def create_task():
    """
    老师创建任务（草稿状态）
    请求体: {
        "group_id": 1,
        "title": "作业1",
        "requirement_text": "要求...",
        "deadline_at": "2026-04-01T23:59:00Z",  # UTC时间
        "allow_late": false,
        "max_attempts": 0,  # 0=不限制
        "is_scored": true,
        "score_min": 0,
        "score_max": 100
    }
    """
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    if not user:
        return jsonify({"code": 404, "message": "用户不存在"}), 404

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"code": 400, "message": "请求参数错误"}), 400

    group_id = data.get('group_id')
    title = data.get('title')

    if not group_id or not title:
        return jsonify({"code": 400, "message": "小组ID和标题不能为空"}), 400

    # 校验小组存在
    group = CourseGroup.query.get(group_id)
    if not group:
        return jsonify({"code": 404, "message": "小组不存在"}), 404

    # 校验权限：只有小组老师可以创建任务
    if group.teacher_id != user.id:
        return jsonify({"code": 403, "message": "无权限"}), 403

    # 解析截止时间
    deadline_at = None
    if data.get('deadline_at'):
        try:
            deadline_at = datetime.fromisoformat(data['deadline_at'].replace('Z', '+00:00'))
        except:
            return jsonify({"code": 400, "message": "截止时间格式错误"}), 400

    # 创建任务
    task = Task(
        group_id=group_id,
        course_id=group.course_id,
        term=group.term,
        teacher_id=user.id,
        title=title,
        requirement_text=data.get('requirement_text'),
        deadline_at=deadline_at,
        allow_late=data.get('allow_late', False),
        max_attempts=data.get('max_attempts', 0),
        is_scored=data.get('is_scored', False),
        score_min=data.get('score_min', 0),
        score_max=data.get('score_max', 100),
        status=Task.STATUS_DRAFT
    )
    db.session.add(task)
    db.session.commit()

    return jsonify({
        "code": 201,
        "message": "created",
        "data": {
            "id": task.id,
            "group_id": task.group_id,
            "title": task.title,
            "status": task.status
        }
    }), 201


# 任务列表 GET /tasks
@bp.route("", methods=["GET"])
@jwt_required()
def list_tasks():
    """
    任务列表
    参数: group_id, mine=teaching|learning, status
    """
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    if not user:
        return jsonify({"code": 404, "message": "用户不存在"}), 404

    group_id = request.args.get('group_id')
    mine = request.args.get('mine')  # teaching / learning
    status = request.args.get('status')

    query = Task.query

    # mine 过滤
    if mine == 'teaching':
        query = query.filter(Task.teacher_id == user.id)
    elif mine == 'learning':
        # 学生：只能看到自己所在小组的任务
        query = query.join(CourseGroupMember, and_(
            Task.group_id == CourseGroupMember.group_id,
            Task.course_id == CourseGroupMember.course_id
        )).filter(CourseGroupMember.student_id == user.id)

    if group_id:
        query = query.filter_by(group_id=group_id)
    if status:
        query = query.filter_by(status=status)

    tasks = query.order_by(Task.created_at.desc()).all()

    result = []
    for task in tasks:
        # 统计提交情况
        submitted_count = TaskAssignee.query.filter_by(task_id=task.id, status=TaskAssignee.STATUS_SUBMITTED).count()
        late_count = TaskAssignee.query.filter_by(task_id=task.id, status=TaskAssignee.STATUS_LATE).count()
        graded_count = TaskAssignee.query.filter_by(task_id=task.id, status=TaskAssignee.STATUS_GRADED).count()
        total_count = TaskAssignee.query.filter_by(task_id=task.id).count()

        task_data = {
            "id": task.id,
            "group_id": task.group_id,
            "title": task.title,
            "deadline_at": task.deadline_at.strftime('%Y-%m-%dT%H:%M:%SZ') if task.deadline_at else None,
            "status": task.status,
            "is_scored": task.is_scored,
            "allow_late": task.allow_late,
            "stats": {
                "total": total_count,
                "submitted": submitted_count,
                "late": late_count,
                "graded": graded_count
            }
        }

        # 如果是学生，获取该学生个人的提交状态
        if mine == 'learning':
            my_submission = TaskAssignee.query.filter_by(
                task_id=task.id,
                student_id=user.id
            ).first()
            task_data['my_status'] = my_submission.status if my_submission else 'not_started'

        result.append(task_data)

    return jsonify({
        "code": 200,
        "data": result,
        "total": len(result)
    })


# 任务详情 GET /tasks/{id}
@bp.route("/<int:task_id>", methods=["GET"])
@jwt_required()
def get_task_detail(task_id):
    """
    获取任务详情
    """
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    if not user:
        return jsonify({"code": 404, "message": "用户不存在"}), 404

    task = Task.query.get(task_id)
    if not task:
        return jsonify({"code": 404, "message": "任务不存在"}), 404

    # 权限检查
    is_teacher = (task.teacher_id == user.id)
    is_student = CourseGroupMember.query.filter_by(
        group_id=task.group_id,
        course_id=task.course_id,
        student_id=user.id
    ).first() is not None

    if not is_teacher and not is_student:
        return jsonify({"code": 403, "message": "无权限查看"}), 403

    # 获取派发对象统计
    assignees = TaskAssignee.query.filter_by(task_id=task_id).all()
    stats = {
        "total": len(assignees),
        "not_started": sum(1 for a in assignees if a.status == TaskAssignee.STATUS_NOT_STARTED),
        "submitted": sum(1 for a in assignees if a.status == TaskAssignee.STATUS_SUBMITTED),
        "late": sum(1 for a in assignees if a.status == TaskAssignee.STATUS_LATE),
        "graded": sum(1 for a in assignees if a.status == TaskAssignee.STATUS_GRADED),
        "missed": sum(1 for a in assignees if a.status == TaskAssignee.STATUS_MISSED)
    }

    return jsonify({
        "code": 200,
        "data": {
            "id": task.id,
            "group_id": task.group_id,
            "course_id": task.course_id,
            "term": task.term,
            "teacher_id": task.teacher_id,
            "title": task.title,
            "requirement_text": task.requirement_text,
            "deadline_at": task.deadline_at.strftime('%Y-%m-%dT%H:%M:%SZ') if task.deadline_at else None,
            "allow_late": task.allow_late,
            "max_attempts": task.max_attempts,
            "is_scored": task.is_scored,
            "score_min": task.score_min,
            "score_max": task.score_max,
            "status": task.status,
            "stats": stats,
            "created_at": task.created_at.strftime('%Y-%m-%d %H:%M:%S'),
            "is_teacher": is_teacher
        }
    })


# 更新任务 PUT /tasks/{id}
@bp.route("/<int:task_id>", methods=["PUT"])
@jwt_required()
def update_task(task_id):
    """
    老师编辑任务（只能是草稿状态）
    """
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    if not user:
        return jsonify({"code": 404, "message": "用户不存在"}), 404

    task = Task.query.get(task_id)
    if not task:
        return jsonify({"code": 404, "message": "任务不存在"}), 404

    # 权限检查
    if task.teacher_id != user.id:
        return jsonify({"code": 403, "message": "无权限"}), 403

    # 只能编辑草稿状态的任务
    if task.status != Task.STATUS_DRAFT:
        return jsonify({"code": 400, "message": "只能编辑草稿状态的任务"}), 400

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"code": 400, "message": "请求参数错误"}), 400

    if 'title' in data:
        task.title = data['title']
    if 'requirement_text' in data:
        task.requirement_text = data['requirement_text']
    if 'deadline_at' in data:
        try:
            task.deadline_at = datetime.fromisoformat(data['deadline_at'].replace('Z', '+00:00'))
        except:
            pass
    if 'allow_late' in data:
        task.allow_late = data['allow_late']
    if 'max_attempts' in data:
        task.max_attempts = data['max_attempts']
    if 'is_scored' in data:
        task.is_scored = data['is_scored']
    if 'score_min' in data:
        task.score_min = data['score_min']
    if 'score_max' in data:
        task.score_max = data['score_max']

    db.session.commit()

    return jsonify({
        "code": 200,
        "message": "updated",
        "data": {
            "id": task.id,
            "status": task.status
        }
    })


# 发布任务 POST /tasks/{id}/publish
@bp.route("/<int:task_id>/publish", methods=["POST"])
@jwt_required()
def publish_task(task_id):
    """
    发布任务并生成 assignee 快照
    """
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    if not user:
        return jsonify({"code": 404, "message": "用户不存在"}), 404

    task = Task.query.get(task_id)
    if not task:
        return jsonify({"code": 404, "message": "任务不存在"}), 404

    # 权限检查
    if task.teacher_id != user.id:
        return jsonify({"code": 403, "message": "无权限"}), 403

    # 只能发布草稿状态的任务
    if task.status != Task.STATUS_DRAFT:
        return jsonify({"code": 400, "message": "只能发布草稿状态的任务"}), 400

    # 获取当前小组成员
    members = CourseGroupMember.query.filter_by(
        group_id=task.group_id,
        course_id=task.course_id
    ).all()

    # 生成 assignee 快照
    for member in members:
        assignee = TaskAssignee(
            task_id=task.id,
            student_id=member.student_id,
            status=TaskAssignee.STATUS_NOT_STARTED
        )
        db.session.add(assignee)

    # 更新任务状态
    task.status = Task.STATUS_PUBLISHED
    db.session.commit()

    return jsonify({
        "code": 200,
        "message": "published",
        "data": {
            "id": task.id,
            "status": task.status,
            "assignee_count": len(members)
        }
    })


# 关闭任务 POST /tasks/{id}/close
@bp.route("/<int:task_id>/close", methods=["POST"])
@jwt_required()
def close_task(task_id):
    """
    关闭任务（截止后）
    """
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    if not user:
        return jsonify({"code": 404, "message": "用户不存在"}), 404

    task = Task.query.get(task_id)
    if not task:
        return jsonify({"code": 404, "message": "任务不存在"}), 404

    # 权限检查
    if task.teacher_id != user.id:
        return jsonify({"code": 403, "message": "无权限"}), 403

    # 只能关闭已发布的任务
    if task.status != Task.STATUS_PUBLISHED:
        return jsonify({"code": 400, "message": "只能关闭已发布的任务"}), 400

    task.status = Task.STATUS_CLOSED
    db.session.commit()

    return jsonify({
        "code": 200,
        "message": "closed",
        "data": {
            "id": task.id,
            "status": task.status
        }
    })


# 删除任务 DELETE /tasks/{id}
@bp.route("/<int:task_id>", methods=["DELETE"])
@jwt_required()
def delete_task(task_id):
    """
    删除任务
    """
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    if not user:
        return jsonify({"code": 404, "message": "用户不存在"}), 404

    task = Task.query.get(task_id)
    if not task:
        return jsonify({"code": 404, "message": "任务不存在"}), 404

    # 权限检查：只有创建任务的老师可以删除
    if task.teacher_id != user.id:
        return jsonify({"code": 403, "message": "无权限"}), 403

    # 删除任务相关的提交记录
    TaskAssignee.query.filter_by(task_id=task_id).delete()

    # 删除任务
    db.session.delete(task)
    db.session.commit()

    return jsonify({
        "code": 200,
        "message": "deleted"
    })


# ==================== 提交相关 ====================

# 学生提交 POST /tasks/{id}/submissions
@bp.route("/<int:task_id>/submissions", methods=["POST"])
@jwt_required()
def submit_task(task_id):
    """
    学生提交任务
    请求体: { "content_text": "提交内容" }
    """
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    if not user:
        return jsonify({"code": 404, "message": "用户不存在"}), 404

    task = Task.query.get(task_id)
    if not task:
        return jsonify({"code": 404, "message": "任务不存在"}), 404

    # 检查是否是小组学生
    is_member = CourseGroupMember.query.filter_by(
        group_id=task.group_id,
        course_id=task.course_id,
        student_id=user.id
    ).first() is None
    if is_member:
        return jsonify({"code": 403, "message": "不是小组成员"}), 403

    # 检查任务状态
    if task.status != Task.STATUS_PUBLISHED:
        return jsonify({"code": 400, "message": "任务未发布或已关闭"}), 400

    # 检查是否在 assignee 中
    assignee = TaskAssignee.query.filter_by(
        task_id=task_id,
        student_id=user.id
    ).first()
    if not assignee:
        return jsonify({"code": 403, "message": "不是任务派发对象"}), 403

    # 支持 JSON 和 FormData 两种格式
    content_text = ''
    if request.content_type and 'multipart/form-data' in request.content_type:
        # FormData 格式
        content_text = request.form.get('content_text', '')
    else:
        # JSON 格式
        data = request.get_json(silent=True) or {}
        content_text = data.get('content_text', '')

    # 检查提交次数限制
    if task.max_attempts > 0:
        attempt_count = TaskSubmission.query.filter_by(
            task_id=task_id,
            student_id=user.id
        ).count()
        if attempt_count >= task.max_attempts:
            return jsonify({"code": 400, "message": f"已达到最大提交次数({task.max_attempts})"}), 400

    # 计算尝试次数
    attempt_no = TaskSubmission.query.filter_by(
        task_id=task_id,
        student_id=user.id
    ).count() + 1

    # 判断是否逾期
    now = datetime.utcnow()
    effective_deadline = assignee.extension_deadline_at or task.deadline_at
    is_late = False
    if effective_deadline and now > effective_deadline:
        if not task.allow_late:
            return jsonify({"code": 400, "message": "已逾期且不允许逾期提交"}), 400
        is_late = True

    # 创建提交记录
    submission = TaskSubmission(
        task_id=task_id,
        student_id=user.id,
        attempt_no=attempt_no,
        content_text=content_text,
        submitted_at=now,
        is_late=is_late
    )
    db.session.add(submission)
    db.session.flush()  # 获取 submission.id

    # 处理附件上传
    if request.content_type and 'multipart/form-data' in request.content_type:
        files = request.files.getlist('attachments')
        if files:
            # 创建上传目录
            upload_dir = os.path.join('uploads', 'task_submissions', str(task_id), str(user.id))
            os.makedirs(upload_dir, exist_ok=True)

            for file in files:
                if file.filename:
                    # 生成唯一文件名
                    import uuid
                    ext = os.path.splitext(file.filename)[1]
                    storage_filename = f"{uuid.uuid4().hex}{ext}"
                    file_path = os.path.join(upload_dir, storage_filename)
                    file.save(file_path)

                    # 创建附件记录
                    attachment = TaskSubmissionAttachment(
                        submission_id=submission.id,
                        file_name=file.filename,
                        storage_key=os.path.join('task_submissions', str(task_id), str(user.id), storage_filename),
                        mime_type=file.content_type or 'application/octet-stream',
                        size_bytes=file.content_length or 0,
                        kind='image' if file.content_type and file.content_type.startswith('image/') else 'file'
                    )
                    db.session.add(attachment)

    # 更新 assignee 状态
    assignee.status = TaskAssignee.STATUS_LATE if is_late else TaskAssignee.STATUS_SUBMITTED

    db.session.commit()

    return jsonify({
        "code": 201,
        "message": "submitted",
        "data": {
            "id": submission.id,
            "attempt_no": submission.attempt_no,
            "is_late": submission.is_late,
            "submitted_at": submission.submitted_at.strftime('%Y-%m-%d %H:%M:%S')
        }
    }), 201


# 老师查看全组提交 GET /tasks/{id}/submissions
@bp.route("/<int:task_id>/submissions", methods=["GET"])
@jwt_required()
def list_submissions(task_id):
    """
    老师查看全组提交
    """
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    if not user:
        return jsonify({"code": 404, "message": "用户不存在"}), 404

    task = Task.query.get(task_id)
    if not task:
        return jsonify({"code": 404, "message": "任务不存在"}), 404

    # 权限检查
    if task.teacher_id != user.id:
        return jsonify({"code": 403, "message": "无权限"}), 403

    # 获取所有提交
    submissions = TaskSubmission.query.filter_by(task_id=task_id).order_by(
        TaskSubmission.student_id, TaskSubmission.attempt_no.desc()
    ).all()

    # 按学生分组，只取最后一次提交
    latest_by_student = {}
    for sub in submissions:
        if sub.student_id not in latest_by_student:
            latest_by_student[sub.student_id] = sub

    result = []
    for student_id, sub in latest_by_student.items():
        student = UserModel.query.get(student_id)
        # 获取附件信息
        attachments = []
        for att in sub.attachments:
            attachments.append({
                "id": att.id,
                "name": att.file_name,
                "size": att.size_bytes,
                "mime_type": att.mime_type,
                "storage_key": att.storage_key
            })
        result.append({
            "id": sub.id,
            "student_id": student_id,
            "student_name": student.username if student else "",
            "attempt_no": sub.attempt_no,
            "content_text": sub.content_text,
            "submitted_at": sub.submitted_at.strftime('%Y-%m-%d %H:%M:%S') if sub.submitted_at else None,
            "is_late": sub.is_late,
            "score": sub.score,
            "feedback": sub.feedback,
            "graded_at": sub.graded_at.strftime('%Y-%m-%d %H:%M:%S') if sub.graded_at else None,
            "attachments": attachments
        })

    return jsonify({
        "code": 200,
        "data": result,
        "total": len(result)
    })


# 学生查看自己的提交 GET /tasks/{id}/submissions/me
@bp.route("/<int:task_id>/submissions/me", methods=["GET"])
@jwt_required()
def get_my_submission(task_id):
    """
    学生查看自己的提交记录
    """
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    if not user:
        return jsonify({"code": 404, "message": "用户不存在"}), 404

    task = Task.query.get(task_id)
    if not task:
        return jsonify({"code": 404, "message": "任务不存在"}), 404

    # 获取当前学生的最新提交
    submission = TaskSubmission.query.filter_by(
        task_id=task_id,
        student_id=user.id
    ).order_by(TaskSubmission.attempt_no.desc()).first()

    if not submission:
        return jsonify({
            "code": 404,
            "message": "未找到提交记录",
            "data": None
        }), 404

    # 获取附件信息
    attachments = []
    for att in submission.attachments:
        attachments.append({
            "id": att.id,
            "name": att.file_name,
            "size": att.size_bytes,
            "mime_type": att.mime_type,
            "storage_key": att.storage_key
        })

    return jsonify({
        "code": 200,
        "data": {
            "id": submission.id,
            "student_id": submission.student_id,
            "attempt_no": submission.attempt_no,
            "content_text": submission.content_text,
            "submitted_at": submission.submitted_at.strftime('%Y-%m-%d %H:%M:%S') if submission.submitted_at else None,
            "is_late": submission.is_late,
            "score": submission.score,
            "feedback": submission.feedback,
            "graded_at": submission.graded_at.strftime('%Y-%m-%d %H:%M:%S') if submission.graded_at else None,
            "attachments": attachments
        }
    })


# 老师评分 POST /tasks/{id}/submissions/{submission_id}/grade
@bp.route("/<int:task_id>/submissions/<int:submission_id>/grade", methods=["POST"])
@jwt_required()
def grade_submission(task_id, submission_id):
    """
    老师评分反馈
    请求体: { "score": 90, "feedback": "很好" }
    """
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    if not user:
        return jsonify({"code": 404, "message": "用户不存在"}), 404

    task = Task.query.get(task_id)
    if not task:
        return jsonify({"code": 404, "message": "任务不存在"}), 404

    # 权限检查
    if task.teacher_id != user.id:
        return jsonify({"code": 403, "message": "无权限"}), 403

    submission = TaskSubmission.query.get(submission_id)
    if not submission or submission.task_id != task_id:
        return jsonify({"code": 404, "message": "提交不存在"}), 404

    data = request.get_json(silent=True) or {}

    # 验证分数
    score = data.get('score')
    if score is not None:
        if not task.is_scored:
            return jsonify({"code": 400, "message": "该任务不计入成绩"}), 400
        if score < task.score_min or score > task.score_max:
            return jsonify({"code": 400, "message": f"分数必须在{task.score_min}-{task.score_max}之间"}), 400
        submission.score = score

    submission.feedback = data.get('feedback')
    submission.graded_by = user.id
    submission.graded_at = datetime.utcnow()

    # 更新 assignee 状态
    assignee = TaskAssignee.query.filter_by(
        task_id=task_id,
        student_id=submission.student_id
    ).first()
    if assignee:
        assignee.status = TaskAssignee.STATUS_GRADED

    db.session.commit()

    return jsonify({
        "code": 200,
        "message": "graded",
        "data": {
            "submission_id": submission.id,
            "score": submission.score,
            "feedback": submission.feedback
        }
    })


# 下载提交附件 GET /tasks/{task_id}/submissions/{submission_id}/attachments/{attachment_id}
@bp.route("/<int:task_id>/submissions/<int:submission_id>/attachments/<int:attachment_id>", methods=["GET"])
@jwt_required()
def download_submission_attachment(task_id, submission_id, attachment_id):
    """
    下载提交附件
    """
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    if not user:
        return jsonify({"code": 404, "message": "用户不存在"}), 404

    task = Task.query.get(task_id)
    if not task:
        return jsonify({"code": 404, "message": "任务不存在"}), 404

    # 权限检查
    if task.teacher_id != user.id:
        return jsonify({"code": 403, "message": "无权限"}), 403

    submission = TaskSubmission.query.get(submission_id)
    if not submission or submission.task_id != task_id:
        return jsonify({"code": 404, "message": "提交不存在"}), 404

    attachment = TaskSubmissionAttachment.query.get(attachment_id)
    if not attachment or attachment.submission_id != submission_id:
        return jsonify({"code": 404, "message": "附件不存在"}), 404

    # 获取文件存储路径
    storage_key = attachment.storage_key

    # 尝试从不同位置读取文件
    file_path = None
    possible_paths = [
        os.path.join(current_app.config.get('UPLOAD_FOLDER', 'uploads'), storage_key),
        os.path.join('uploads', storage_key),
        storage_key
    ]

    for path in possible_paths:
        if os.path.exists(path):
            file_path = path
            break

    if not file_path or not os.path.exists(file_path):
        return jsonify({"code": 404, "message": "文件不存在"}), 404

    # 返回文件
    return send_file(
        file_path,
        as_attachment=True,
        download_name=attachment.file_name,
        mimetype=attachment.mime_type
    )

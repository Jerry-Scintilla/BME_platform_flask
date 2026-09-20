from collections import defaultdict

from flask import Blueprint, request, jsonify, send_file
from sqlalchemy.orm import joinedload

# 导入拓展
from exts import db, redis_client

# 导入数据库表
from models import (UserModel, CourseModel, LearningProgressModel, GroupModel,
                    LessonModel, UserCourseModel, CampLearningProgress)

# 导入表单验证
from .forms import LearningProgressForm

# 导入token验证模块
from flask_jwt_extended import (get_jwt_identity, jwt_required)

# 导入api文档模块
from flasgger import swag_from

from . import audit_log

bp = Blueprint("learningProgress", __name__, url_prefix="")


@bp.route("/learningProgress/update", methods=["POST"])
@jwt_required()
@swag_from('../apidocs/learningProgress/update.yaml')
@audit_log(operation="更新学习进度")
def update():
    User_Email = get_jwt_identity()
    user = UserModel.query.filter_by(email=User_Email).first()
    mode = 'admin' if user.is_admin() else 'user'
    if mode != 'admin':
        return jsonify({
            "code": 400,
            'message': "用户权限不够"
        }), 400

    data = request.get_json(silent=True)
    if not data:
        return jsonify({
            "code": 401,
            'message': "数据类型错误"
        }), 401

    # 添加调试信息
    # print(f"Received data: {data}")

    # 检查请求体的结构
    if 'Records' in data:
        records = data['Records']
    else:
        records = [data]

    validated_data = []
    errors = []

    for record in records:
        # 将字符串转换为整数
        try:
            course_id = int(record.get('Course_Id'))
            progress = int(record.get('Progress'))
            user_id = int(record.get('User_Id'))
        except ValueError as e:
            return jsonify({
                "code": 400,
                'message': f"数据类型转换错误: {str(e)}"
            }), 400

        # 创建并设置表单字段数据
        form = LearningProgressForm()

        # 设置表单字段数据
        form.User_Id.data = user_id
        form.Course_Id.data = course_id
        form.Progress.data = progress

        # 添加调试信息
        print(f"Processing record: Course_Id={course_id}, Progress={progress}, User_Id={user_id}")

        if form.validate():
            validated_data.append({
                'User_Id': form.User_Id.data,
                'Course_Id': form.Course_Id.data,
                'Progress': form.Progress.data
            })
        else:
            errors.append(form.errors)

    if errors:
        return jsonify({
            "code": 400,
            'message': "数据验证失败",
            'errors': errors
        }), 400

    message = "加入"

    for item in validated_data:
        # 检查User_Id的存在性
        user = UserModel.query.get(item['User_Id'])
        if not user:
            return jsonify({
                "code": 404,
                'message': f"用户 ID {item['User_Id']} 不存在"
            }), 404
        # 检查Course_Id的存在性
        course = CourseModel.query.get(item['Course_Id'])
        if not course:
            return jsonify({
                "code": 404,
                'message': f"课程 ID {item['Course_Id']} 不存在"
            }), 404
        # 检查到底是更新记录还是插入记录
        existing_progress = LearningProgressModel.query.filter_by(
            user_id=item['User_Id'],
            course_id=item['Course_Id']
        ).first()

        if existing_progress:
            # 更新现有的记录
            existing_progress.progress = item['Progress']
            message = "更新"
        else:
            # 插入新的记录
            new_progress = LearningProgressModel(
                user_id=item['User_Id'],
                course_id=item['Course_Id'],
                progress=item['Progress']
            )
            db.session.add(new_progress)

    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({
            "code": 500,
            'message': f"数据库保存失败: {str(e)}"
        }), 500

    return jsonify({
        "code": 200,
        'message': f"学习进度{str(message)}成功"
    }), 200





@bp.route("/learningProgress/list", methods=["GET"])
@jwt_required()
@swag_from('../apidocs/learningProgress/list.yaml')
def learningprogress_list():
    User_Email = get_jwt_identity()
    user = UserModel.query.filter_by(email=User_Email).first()
    mode = 'admin' if user.is_admin() else 'user'
    if mode != 'admin':
        return jsonify({
            "code": 400,
            'message': "用户权限不够"
        }), 400

    # 查询所有学习进度记录，并加入用户信息
    all_records = LearningProgressModel.query.options(db.joinedload(LearningProgressModel.user), joinedload(LearningProgressModel.course)).all()

    # 使用defaultdict来根据user_id对记录进行分组
    grouped_records = defaultdict(list)
    for record in all_records:
        # 获取章节信息
        chapter_num, section_num, chapter_name, section_name = record.get_chapter_info()
        
        grouped_records[record.user_id].append({
            'course_id': record.course_id,
            'progress': record.progress,
            'course_name': record.course.title,
            'chapter_num': chapter_num,
            'section_num': section_num,
            'chapter_name': chapter_name,
            'section_name': section_name
        })

    # 构建最终结果
    result = []
    for user_id, records in grouped_records.items():
        # 获取用户信息
        user = next((record.user for record in all_records if record.user_id == user_id), None)
        username = user.username if user else '未知用户'

        result.append({
            'user_id': user_id,
            'username': username,
            'records': records
        })

    return jsonify({
        "code": 200,
        'message': "成功获取学习进度列表",
        'data': result
    }), 200


@bp.route("/learningProgress/student", methods=["GET"])
@jwt_required()
@swag_from('../apidocs/learningProgress/student.yaml')
def student():
    User_Email = get_jwt_identity()
    user = UserModel.query.filter_by(email=User_Email).first()
    if not user:
        return jsonify({
            "code": 400,
            'message': "该学生不存在"
        })

    progress = LearningProgressModel.query.filter_by(user_id=user.id).options(joinedload(LearningProgressModel.user), joinedload(LearningProgressModel.course)
).all()

    records = []
    for record in progress:
        # 获取章节信息
        chapter_num, section_num, chapter_name, section_name = record.get_chapter_info()
        
        records.append({
            'course_id': record.course_id,
            'progress': record.progress,
            'course_name': record.course.title,
            'chapter_num': chapter_num,
            'section_num': section_num,
            'chapter_name': chapter_name,
            'section_name': section_name
        })

    result = {
        'user_id': user.id,
        'username': user.username,
        'records': records
    }

    return jsonify({
        "code": 200,
        'message': "成功获取学生学习进度",
        'data': result
    }), 200


@bp.route("/learningProgress/group", methods=["GET"])
@jwt_required()
@swag_from('../apidocs/learningProgress/group.yaml')
def group():
    User_Email = get_jwt_identity()
    user = UserModel.query.filter_by(email=User_Email).first()
    #增加接口安全性
    if not user:
        return jsonify({
            "code": 400,
            'message': "请求用户不存在"
        }), 400

    group_id = request.args.get('Group_Id')

    if not group_id:
        return jsonify({
            "code": 401,
            'message': "Group_Id不能为空"
        }), 401

    # 查询对应 Group_Id 的小组
    group = GroupModel.query.filter_by(group_id=group_id).first()

    if not group:
        return jsonify({
            "code": 402,
            'message': "小组不存在"
        }), 402

    students = GroupModel.query.filter_by(group_id=group_id).all()

    student_ids = [student.student_id for student in students]

    if not user.is_admin():
        if user.id not in student_ids:
            return jsonify({
                "code": 403,
                'message': "本用户不在该小组中"
            }), 403

    result = []

    for student_id in student_ids:
        progress = LearningProgressModel.query.filter_by(user_id=student_id).options(joinedload(LearningProgressModel.course)).all()

        records = []
        for record in progress:
            # 获取章节信息
            chapter_num, section_num, chapter_name, section_name = record.get_chapter_info()
            
            records.append({
                'course_id': record.course_id,
                'progress': record.progress,
                'course_name': record.course.title,
                'course_chapters': record.course.chapters,
                'chapter_num': chapter_num,
                'section_num': section_num,
                'chapter_name': chapter_name,
                'section_name': section_name
            })

        user = UserModel.query.get(student_id)

        result.append({
            'user_id': student_id,
            'username': user.username,
            'records': records,
        })

    return jsonify({
        "code": 200,
        'message':'获取学生小组学习进度成功',
        'data': {'result':result,
                 'group_name':group.name
                 }
    }), 200


@bp.route("/learningProgress/delete", methods=["POST"])
@jwt_required()
@swag_from('../apidocs/learningProgress/delete.yaml')
@audit_log(operation="删除学习进度")
def delete():
    User_Email = get_jwt_identity()
    user = UserModel.query.filter_by(email=User_Email).first()
    mode = 'admin' if user.is_admin() else 'user'
    if mode != 'admin':
        return jsonify({
            "code": 400,
            'message': "用户权限不够"
        }), 400

    delete_progress = request.get_json(silent=True)

    progress_record = LearningProgressModel.query.filter_by(
        user_id=delete_progress['User_Id'],
        course_id=delete_progress['Course_Id']
    ).first()

    if progress_record:
        # 删除找到的记录
        db.session.delete(progress_record)
        # 提交事务
        db.session.commit()
        return jsonify({
            "code": 200,
            'message': "学习记录删除成功"
        }), 200
    else:
        return jsonify({
            "code": 401,
            'message': "未找到学习记录"
        }), 401


@bp.route("/learningProgress/group_through_courseid", methods=["GET"])
@jwt_required()
@swag_from('../apidocs/learningProgress/group_through_courseid.yaml')  
def group_through_courseid():
    # 获取当前用户
    User_Email = get_jwt_identity()
    user = UserModel.query.filter_by(email=User_Email).first()
    if not user:
        return jsonify({
            "code": 400,
            'message': "请求用户不存在"
        }), 400

    # 获取课程ID参数
    course_id = request.args.get('Course_Id')
    if not course_id:
        return jsonify({
            "code": 401,
            'message': "Course_Id不能为空"
        }), 401
    
    # 查询课程是否存在
    course = CourseModel.query.get(course_id)
    if not course:
        return jsonify({
            "code": 404,
            'message': "课程不存在"
        }), 404

    # 查找用户在该课程下所属的小组
    user_group = GroupModel.query.filter_by(
        student_id=user.id,
        course_id=course_id
    ).first()
    
    if not user_group:
        return jsonify({
            "code": 402,
            'message': "用户在该课程下没有加入小组"
        }), 402
    
    # 获取同组成员
    group_id = user_group.group_id
    group_members = GroupModel.query.filter_by(
        group_id=group_id,
        course_id=course_id
    ).all()
    
    # 如果没有找到同组成员，可能是数据问题
    if not group_members:
        return jsonify({
            "code": 405,
            'message': "无法找到小组成员"
        }), 405
    
    # 获取小组名称
    group_name = user_group.name

    teacher = UserModel.query.get(user_group.teacher_id)
    
    # 获取所有组员的学习进度
    result = []
    for member in group_members:
        # 获取该成员的学习进度
        progress = LearningProgressModel.query.filter_by(
            user_id=member.student_id,
            course_id=course_id
        ).options(joinedload(LearningProgressModel.course)).first()
        
        # 获取成员信息
        student = UserModel.query.get(member.student_id)
        
        # 创建记录
        progress_data = {
            'course_id': int(course_id),
            'progress': progress.progress if progress else 0,
            'course_name': course.title,
            'course_chapters': course.chapters
        }
        
        # 如果有进度记录，添加章节信息
        if progress:
            chapter_num, section_num, chapter_name, section_name = progress.get_chapter_info()
            progress_data.update({
                'chapter_num': chapter_num,
                'section_num': section_num,
                'chapter_name': chapter_name,
                'section_name': section_name
            })
        else:
            progress_data.update({
                'chapter_num': None,
                'section_num': None,
                'chapter_name': None,
                'section_name': None
            })
        
        result.append({
            'user_id': student.id,
            'username': student.username,
            'records': [progress_data]  # 由于是针对单个课程，所以只有一条记录
        })
    
    return jsonify({
        "code": 200,
        'message': '获取同组学生学习进度成功',
        'data': {
            'result': result,
            'group_name': group_name,
            'teacher_id': teacher.id,
            'teacher_name': teacher.username
        }
    }), 200


# ==================== 课时进度管理 API ====================

def _camp_scope(user_id, course_id, prefer_sid=None):
    """营期快照分流判定（09-14；2026-09-20 B2 改 assignment 口径，migrate_47）：
    camp_course_assignment active 行 → 非 archived 营（同课跨营多活营时最新分配优先；
    prefer_sid=前端营内入口带参显式指定）。仅 (user, course) 完全无分配行时回退旧
    user_course 营戳（回填漏网兜底）。无有效营 → None（走全局表）。"""
    from .camp_course_assign import active_scope_camp
    return active_scope_camp(user_id, course_id, prefer_sid=prefer_sid)


@bp.route("/learningProgress/lesson/update", methods=["POST"])
@jwt_required()
@swag_from('../apidocs/learningProgress/lesson_update.yaml')
def update_lesson_progress():
    """
    更新单个课时学习进度
    请求参数:
    {
        "Course_Id": 1,
        "Lesson_Id": 5,
        "Status": "learning" / "completed",
        "Duration": 15,  // 学习时长（分钟）
        "Detail": {}     // JSON格式详情
    }
    """
    from datetime import datetime

    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    if not user:
        return jsonify({"code": 404, "message": "用户不存在"}), 404

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"code": 400, "message": "请求参数错误"}), 400

    course_id = data.get('Course_Id')
    lesson_id = data.get('Lesson_Id')
    status = data.get('Status', 'learning')
    duration = data.get('Duration', 0)
    detail = data.get('Detail', {})

    # 验证课程存在
    course = CourseModel.query.get(course_id)
    if not course:
        return jsonify({"code": 404, "message": "课程不存在"}), 404

    # 验证课时存在
    lesson = LessonModel.query.get(lesson_id)
    if not lesson:
        return jsonify({"code": 404, "message": "课时不存在"}), 404

    # 验证状态
    valid_status = ['not_started', 'learning', 'completed']
    if status not in valid_status:
        return jsonify({"code": 400, "message": f"状态必须为: {', '.join(valid_status)}"}), 400

    now = datetime.now()

    # 营期快照分流（09-14；B2 起 assignment 口径 + 显式 sid）：body 带 camp_session_id
    # （前端从营内入口进来）优先按该营分配行分流——同课跨营多活营时打点落营准确；
    # 无该营分配行则回落最新分配推导。非 archived 营 → 写快照表（营期维度从零），不碰全局
    try:
        prefer_sid = int(data.get('camp_session_id'))
    except (TypeError, ValueError):
        prefer_sid = None                   # 非法值静默忽略，回落推导口径
    camp = _camp_scope(user.id, course_id, prefer_sid=prefer_sid)
    if camp is not None:
        snap = CampLearningProgress.query.filter_by(
            camp_session_id=camp.id, user_id=user.id, lesson_id=lesson_id).first()
        if snap:
            snap.status = status
            snap.duration = duration or snap.duration
            snap.detail = detail or snap.detail
            if status == 'completed' and not snap.completed_time:
                snap.completed_time = now
            if status == 'learning' and not snap.start_time:
                snap.start_time = now
        else:
            snap = CampLearningProgress(
                camp_session_id=camp.id, user_id=user.id, course_id=course_id,
                lesson_id=lesson_id, status=status,
                duration=duration, detail=detail,
                start_time=now if status != 'not_started' else None,
                completed_time=now if status == 'completed' else None)
            db.session.add(snap)
        db.session.commit()
        return jsonify({
            "code": 200,
            "message": "课时进度更新成功（营期快照）",
            "scope": "camp", "camp_session_id": camp.id,
            "progress": snap.to_dict(),
        })

    # 查找是否已有记录
    progress = LearningProgressModel.query.filter_by(
        user_id=user.id,
        course_id=course_id,
        lesson_id=lesson_id
    ).first()

    if progress:
        # 更新现有记录
        progress.status = status
        progress.duration = duration or progress.duration
        progress.detail = detail or progress.detail
        if status == 'completed' and not progress.completed_time:
            progress.completed_time = now
        if status == 'learning' and not progress.start_time:
            progress.start_time = now
    else:
        # 创建新记录
        progress = LearningProgressModel(
            user_id=user.id,
            course_id=course_id,
            lesson_id=lesson_id,
            status=status,
            duration=duration,
            detail=detail,
            start_time=now if status != 'not_started' else None,
            completed_time=now if status == 'completed' else None
        )
        db.session.add(progress)

    db.session.commit()

    return jsonify({
        "code": 200,
        "message": "课时进度更新成功",
        "scope": "global",
        "progress": progress.to_dict()
    })


@bp.route("/learningProgress/lesson/list", methods=["GET"])
@jwt_required()
@swag_from('../apidocs/learningProgress/lesson_list.yaml')
def list_lesson_progress():
    """
    获取课程下所有课时的学习进度
    参数: Course_Id
    """
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    if not user:
        return jsonify({"code": 404, "message": "用户不存在"}), 404

    course_id = request.args.get('Course_Id')
    if not course_id:
        return jsonify({"code": 400, "message": "缺少课程ID"}), 400

    # 验证课程存在
    course = CourseModel.query.get(course_id)
    if not course:
        return jsonify({"code": 404, "message": "课程不存在"}), 404

    # 获取该课程所有课时
    lessons = LessonModel.query.filter_by(course_id=course_id).order_by(LessonModel.order).all()

    # 获取用户所有课时进度（09-14：带 camp_session_id 参数 → 查营期快照表，营内口径从零；
    # 查的永远是自己名下的行，sid 只决定口径，传错最多拿到自己空集，无需额外鉴权）
    camp_sid = request.args.get('camp_session_id')
    if camp_sid:
        progress_list = CampLearningProgress.query.filter_by(
            camp_session_id=camp_sid,
            user_id=user.id,
            course_id=course_id
        ).all()
    else:
        progress_list = LearningProgressModel.query.filter_by(
            user_id=user.id,
            course_id=course_id
        ).all()

    # 构建进度映射
    progress_map = {p.lesson_id: p for p in progress_list}

    # 组装结果
    result = []
    for lesson in lessons:
        progress = progress_map.get(lesson.id)
        if progress:
            result.append({
                'lesson_id': lesson.id,
                'lesson_title': lesson.title,
                'lesson_type': lesson.type,
                'lesson_order': lesson.order,
                'status': progress.status,
                'duration': progress.duration,
                'detail': progress.detail,
                'start_time': progress.start_time.strftime('%Y-%m-%d %H:%M:%S') if progress.start_time else None,
                'completed_time': progress.completed_time.strftime('%Y-%m-%d %H:%M:%S') if progress.completed_time else None
            })
        else:
            result.append({
                'lesson_id': lesson.id,
                'lesson_title': lesson.title,
                'lesson_type': lesson.type,
                'lesson_order': lesson.order,
                'status': 'not_started',
                'duration': 0,
                'detail': None,
                'start_time': None,
                'completed_time': None
            })

    # 统计
    completed_count = sum(1 for r in result if r['status'] == 'completed')
    total_count = len(result)

    return jsonify({
        "code": 200,
        "message": "查询成功",
        "data": result,
        "summary": {
            "total": total_count,
            "completed": completed_count,
            "learning": total_count - completed_count,
            "progress_percent": round(completed_count / total_count * 100, 2) if total_count > 0 else 0
        }
    })


@bp.route("/learningProgress/lesson/detail", methods=["GET"])
@jwt_required()
@swag_from('../apidocs/learningProgress/lesson_detail.yaml')
def get_lesson_progress():
    """
    获取单个课时的学习进度
    参数: Course_Id, Lesson_Id
    """
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    if not user:
        return jsonify({"code": 404, "message": "用户不存在"}), 404

    course_id = request.args.get('Course_Id')
    lesson_id = request.args.get('Lesson_Id')

    if not course_id or not lesson_id:
        return jsonify({"code": 400, "message": "缺少参数"}), 400

    # 验证课时存在
    lesson = LessonModel.query.get(lesson_id)
    if not lesson:
        return jsonify({"code": 404, "message": "课时不存在"}), 404

    # 获取进度
    progress = LearningProgressModel.query.filter_by(
        user_id=user.id,
        course_id=course_id,
        lesson_id=lesson_id
    ).first()

    if progress:
        return jsonify({
            "code": 200,
            "message": "查询成功",
            "data": progress.to_dict()
        })
    else:
        return jsonify({
            "code": 200,
            "message": "查询成功",
            "data": {
                'lesson_id': lesson_id,
                'status': 'not_started',
                'duration': 0,
                'detail': None
            }
        })


# ==================== 用户选课 API ====================

# 「自主加入学习」已移除（阶段 1，A14）：课程独立但学习进度只走营期，
# 唯一入课途径 = 营期选课 /camp/selection（带 camp_session_id 戳）。
# 历史无营戳的 user_course 行保留只读，进度照常展示。


@bp.route("/userCourse/drop", methods=["POST"])
@jwt_required()
@swag_from('../apidocs/userCourse/drop.yaml')
def drop_course():
    """
    用户退课
    请求参数:
    {
        "Course_Id": 1
    }
    """
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    if not user:
        return jsonify({"code": 404, "message": "用户不存在"}), 404

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"code": 400, "message": "请求参数错误"}), 400

    course_id = data.get('Course_Id')
    if not course_id:
        return jsonify({"code": 400, "message": "课程ID不能为空"}), 400

    # 检查选课记录
    user_course = UserCourseModel.query.filter_by(
        user_id=user.id,
        course_id=course_id
    ).first()

    if not user_course:
        return jsonify({"code": 404, "message": "您还未选修该课程"}), 404

    if user_course.status == UserCourseModel.STATUS_DROPPED:
        return jsonify({"code": 400, "message": "您已退选该课程"}), 400

    # 更新状态为已退课
    user_course.status = UserCourseModel.STATUS_DROPPED
    db.session.commit()

    return jsonify({
        "code": 200,
        "message": "退课成功"
    })


@bp.route("/userCourse/list", methods=["GET"])
@jwt_required()
@swag_from('../apidocs/userCourse/list.yaml')
def list_user_courses():
    """
    获取当前用户的选课列表
    """
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    if not user:
        return jsonify({"code": 404, "message": "用户不存在"}), 404

    # 获取所有选课记录
    user_courses = UserCourseModel.query.filter_by(user_id=user.id).all()

    result = []
    for uc in user_courses:
        course = CourseModel.query.get(uc.course_id)
        if course:
            result.append({
                'id': uc.id,
                'course_id': uc.course_id,
                'course_title': course.title,
                'course_cover': course.cover,
                'enroll_time': uc.enroll_time.strftime('%Y-%m-%d %H:%M:%S') if uc.enroll_time else None,
                'status': uc.status
            })

    return jsonify({
        "code": 200,
        "message": "获取选课列表成功",
        "data": result
    })


@bp.route("/userCourse/check", methods=["GET"])
@jwt_required()
@swag_from('../apidocs/userCourse/check.yaml')
def check_course():
    """
    检查当前用户是否已选某课程
    参数: Course_Id
    """
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    if not user:
        return jsonify({"code": 404, "message": "用户不存在"}), 404

    course_id = request.args.get('Course_Id')
    if not course_id:
        return jsonify({"code": 400, "message": "课程ID不能为空"}), 400

    # 检查选课记录
    user_course = UserCourseModel.query.filter_by(
        user_id=user.id,
        course_id=course_id
    ).first()

    # 营期戳透出（09-14；B2 起 assignment 口径）：当前有效分配营（非 archived）才返回
    # ——结营后全局才是有效口径；前端用它做进度口径分流（effectiveSid），保证从任意
    # 路径进详情页口径一致。同课跨营多活营时返回最新分配营（与打点分流一致）。
    camp_sid = None
    scope = _camp_scope(user.id, course_id)
    if scope is not None:
        camp_sid = scope.id

    if user_course and user_course.status == UserCourseModel.STATUS_ACTIVE:
        return jsonify({
            "code": 200,
            "message": "已选课",
            "data": {
                "enrolled": True,
                "status": user_course.status,
                "enroll_time": user_course.enroll_time.strftime('%Y-%m-%d %H:%M:%S') if user_course.enroll_time else None,
                "camp_session_id": camp_sid
            }
        })
    else:
        return jsonify({
            "code": 200,
            "message": "未选课",
            "data": {
                "enrolled": False,
                "status": user_course.status if user_course else None,
                "camp_session_id": camp_sid
            }
        })


@bp.route("/userCourse/students", methods=["GET"])
@jwt_required()
@swag_from('../apidocs/userCourse/students.yaml')
def list_course_students():
    """
    获取某课程的所有学生（管理员/教师）
    参数: Course_Id
    """
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    if not user:
        return jsonify({"code": 404, "message": "用户不存在"}), 404

    # 只有管理员可以查看
    if not user.is_admin():
        return jsonify({"code": 403, "message": "权限不足"}), 403

    course_id = request.args.get('Course_Id')
    if not course_id:
        return jsonify({"code": 400, "message": "课程ID不能为空"}), 400

    # 验证课程存在
    course = CourseModel.query.get(course_id)
    if not course:
        return jsonify({"code": 404, "message": "课程不存在"}), 404

    # 获取所有选课学生
    user_courses = UserCourseModel.query.filter_by(
        course_id=course_id,
        status=UserCourseModel.STATUS_ACTIVE
    ).all()

    result = []
    for uc in user_courses:
        student = UserModel.query.get(uc.user_id)
        if student:
            result.append({
                'user_id': student.id,
                'username': student.username,
                'email': student.email,
                'enroll_time': uc.enroll_time.strftime('%Y-%m-%d %H:%M:%S') if uc.enroll_time else None
            })

    return jsonify({
        "code": 200,
        "message": "获取学生列表成功",
        "data": {
            "course_id": course_id,
            "course_title": course.title,
            "students": result,
            "total": len(result)
        }
    })
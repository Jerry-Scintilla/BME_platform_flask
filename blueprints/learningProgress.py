from collections import defaultdict

from flask import Blueprint, request, jsonify, send_file
from sqlalchemy.orm import joinedload

# 导入拓展
from exts import db, redis_client

# 导入数据库表
from models import UserModel, CourseModel, LearningProgressModel, GroupModel

# 导入表单验证
from .forms import LearningProgressForm

# 导入token验证模块
from flask_jwt_extended import (get_jwt_identity, jwt_required)

# 导入api文档模块
from flasgger import swag_from

bp = Blueprint("learningProgress", __name__, url_prefix="")


@bp.route("/learningProgress/update", methods=["POST"])
@jwt_required()
@swag_from('../apidocs/learningProgress/update.yaml')
def update():
    User_Email = get_jwt_identity()
    user = UserModel.query.filter_by(email=User_Email).first()
    mode = user.user_mode
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
    mode = user.user_mode
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
        grouped_records[record.user_id].append({
            'course_id': record.course_id,
            'progress': record.progress,
            'course_name': record.course.title
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
        records.append({
            'course_id': record.course_id,
            'progress': record.progress,
            'course_name': record.course.title
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

    if user.user_mode != 'admin':
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
            records.append({
                'course_id': record.course_id,
                'progress': record.progress,
                'course_name': record.course.title,
                'course_chapters':record.course.chapters
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
def delete():
    User_Email = get_jwt_identity()
    user = UserModel.query.filter_by(email=User_Email).first()
    mode = user.user_mode
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
            'group_name': group_name
        }
    }), 200
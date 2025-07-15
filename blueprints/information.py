import os
import datetime
import base64 # Added for error image handling

from flask import Blueprint, request, jsonify

# 导入拓展
from exts import db, redis_client

# 导入数据库表
from models import UserModel, GroupModel, InformationModel

# 导入token验证模块
from flask_jwt_extended import (get_jwt_identity, jwt_required)

# 导入api文档模块
from flasgger import swag_from

# 导入表单验证
from blueprints.forms import LeaveForm, TaskForm, NoticeForm

bp = Blueprint("information", __name__, url_prefix="")

# 辅助函数：创建提醒信息
def create_reminder(title, content, related_info_id, student_id, group_id, source_type=0):
    """
    创建提醒信息
    
    参数:
    - title: 提醒标题
    - content: 提醒内容
    - related_info_id: 关联的原始信息ID
    - student_id: 接收提醒的用户ID
    - group_id: 所属小组ID
    - source_type: 原始信息类型 (1:请假 2:任务 3:通知 4:报错 5:作业)
    
    返回:
    - 创建的提醒信息对象
    """
    reminder = InformationModel(
        group_id=group_id,
        type=0,  # 0代表提醒信息
        title=title,
        content=content,
        range=str(related_info_id),  # 存储关联的信息ID
        student_id=student_id,
        priority=source_type  # 使用priority字段存储原始信息类型
    )
    
    db.session.add(reminder)
    db.session.commit()
    
    return reminder

# 辅助函数：处理日期时间格式，如果只有日期部分，则设置时间为23:59:59
def process_datetime(dt):
    """
    处理日期时间格式，如果只有日期部分（时间部分为00:00:00或未提供），则设置时间为23:59:59
    支持datetime对象或字符串格式
    注意：表单类已经处理了字符串格式的日期时间，此函数主要用于处理数据库中已存在的日期时间
    """
    if not dt:
        return dt
    
    # 如果是datetime对象，检查是否只有日期部分
    if hasattr(dt, 'hour') and dt.hour == 0 and dt.minute == 0 and dt.second == 0:
        # 只有日期部分，设置时间为23:59:59
        return datetime.datetime(dt.year, dt.month, dt.day, 23, 59, 59)
    
    return dt

# 辅助函数：删除与指定信息关联的所有提醒
def delete_related_reminders(info_id):
    """
    删除与指定信息ID关联的所有提醒
    
    参数:
    - info_id: 原始信息的ID
    
    返回:
    - 删除的提醒数量
    """
    # 查找所有关联该信息的提醒
    reminders = InformationModel.query.filter_by(
        type=0,  # 提醒信息
        range=str(info_id)  # 关联的信息ID
    ).all()
    
    # 记录删除数量
    deleted_count = len(reminders)
    
    # 批量删除
    for reminder in reminders:
        db.session.delete(reminder)
    
    # 提交到数据库
    if deleted_count > 0:
        db.session.commit()
    
    return deleted_count

@bp.route("/information/leave/add", methods=["POST"])
@jwt_required()
@swag_from('../apidocs/information/leave/add.yaml')
def leave_add():
    """
    添加请假信息
    """
    form = LeaveForm()
    if form.validate():
        # 获取表单数据
        group_id = form.Group_Id.data
        title = form.Title.data
        content = form.Content.data
        start_time = process_datetime(form.Start_Time.data)  # 处理日期时间格式
        end_time = process_datetime(form.End_Time.data)      # 处理日期时间格式
        
        # 获取当前用户
        user_email = get_jwt_identity()
        user = UserModel.query.filter_by(email=user_email).first()
        if not user:
            return jsonify({
                "code": 404,
                "message": "用户不存在"
            }),404
        
        # 检查组是否存在
        group = GroupModel.query.filter_by(group_id=group_id).first()
        if not group:
            return jsonify({
                "code": 400,
                "message": "小组不存在"
            }),400
        
        # 创建请假信息
        information = InformationModel(
            group_id=group_id,
            type=1,  # 1代表请假信息
            title=title,
            content=content,
            start_time=start_time,
            end_time=end_time,
            student_id=user.id,  # 记录请假学生ID
            status=0  # 0表示未批准
        )
        
        db.session.add(information)
        db.session.commit()
        
        # 创建提醒给组长（如果有组长）
        if group.teacher_id:
            reminder_title = f"新的请假申请: {title}"
            reminder_content = f"{user.username}提交了请假申请，起止时间: {start_time.strftime('%Y-%m-%d %H:%M:%S') if isinstance(start_time, datetime.datetime) else start_time if start_time else '未设置'} - {end_time.strftime('%Y-%m-%d %H:%M:%S') if isinstance(end_time, datetime.datetime) else end_time if end_time else '未设置'}"
            create_reminder(
                title=reminder_title,
                content=reminder_content,
                related_info_id=information.id,
                student_id=group.teacher_id,
                group_id=group_id,
                source_type=1  # 1代表请假信息
            )
        
        return jsonify({
            "code": 200,
            "message": "请假申请提交成功",
            "data": {
                "id": information.id
            }
        }),200
    else:
        return jsonify({
            "code": 400,
            "message": form.errors
        }),400

@bp.route("/information/leave/delete", methods=["POST"])
@jwt_required()
@swag_from('../apidocs/information/leave/delete.yaml')
def leave_delete():
    """
    删除请假信息
    """
    # 获取请求数据
    data = request.get_json()
    leave_id = data.get("id")
    
    if not leave_id:
        return jsonify({
            "code": 400,
            "message": "请假ID不能为空" 
        }),400
    
    # 获取当前用户
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    if not user:
        return jsonify({
            "code": 404,
            "message": "用户不存在"
        }),404
    
    # 查找请假信息
    leave = InformationModel.query.filter_by(id=leave_id, type=1).first()
    
    if not leave:
        return jsonify({
            "code": 404,
            "message": "请假信息不存在"
        }),404
    
    # 验证删除权限：请假人或组长都可删除
    is_requester = (leave.student_id == user.id)
    is_group_leader = False
    
    # 查询小组信息，检查用户是否为小组组长
    group = GroupModel.query.filter_by(group_id=leave.group_id).first()
    if group and group.teacher_id == user.id:
        is_group_leader = True
    
    # 判断是否有删除权限
    if not (is_requester or is_group_leader):
        return jsonify({
            "code": 403,
            "message": "无权删除此请假信息，仅请假人或组长可删除"
        }),403
    
    # 删除关联的提醒信息
    reminders_deleted = delete_related_reminders(leave_id)
    
    # 删除请假信息
    db.session.delete(leave)
    db.session.commit()
    
    return jsonify({
        "code": 200,
        "message": "请假信息删除成功",
        "data": {
            "reminders_deleted": reminders_deleted
        }
    }),200

@bp.route("/information/leave/query", methods=["GET"])
@jwt_required()
@swag_from('../apidocs/information/leave/query.yaml')
def leave_query():
    """
    查询请假信息 - 支持三种查询方式：
    1. 管理员通过小组ID查询特定小组的请假信息（需提供group_id）
    2. 学生查询自己的请假信息（无需提供ID）
    3. 教师查询所负责小组的请假信息（无需提供ID）
    """
    # 获取查询参数
    group_id = request.args.get("group_id", type=int)
    status = request.args.get("status", type=int)
    
    # 获取当前用户
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    if not user:
        return jsonify({
            "code": 404,
            "message": "用户不存在"
        }),404
    
    # 构建基础查询条件
    query = InformationModel.query.filter_by(type=1)  # 类型1为请假信息
    
    # 如果通过小组ID查询，检查用户是否为管理员
    if group_id:
        if user.user_mode != 'admin':
            return jsonify({
                "code": 403,
                "message": "权限不足，只有管理员可以通过小组ID查询"
            }),403
        # 管理员可以查询特定小组
        query = query.filter_by(group_id=group_id)
    else:
        # 先尝试作为学生查询
        student_groups = GroupModel.query.filter_by(student_id=user.id).all()
        student_group_ids = [group.group_id for group in student_groups]

        # 再查询作为教师负责的小组
        teacher_groups = GroupModel.query.filter_by(teacher_id=user.id).all()
        teacher_group_ids = [group.group_id for group in teacher_groups]
        
        # 合并学生和教师的小组ID
        all_group_ids = list(set(student_group_ids + teacher_group_ids))
        
        if all_group_ids:
            # 如果用户有相关小组，查询这些小组的请假信息
            query = query.filter(InformationModel.group_id.in_(all_group_ids))
        else:
            # 既不是学生也不是教师，返回空列表
            return jsonify({
                "code": 200,
                "message": "查询成功，但无相关请假信息",
                "data": []
            }),200
    
    # 根据状态筛选
    if status is not None:  # 0和1都是有效值
        query = query.filter_by(status=status)

    if group_id is not None:
        query = query.filter_by(group_id=group_id)
    
    # 执行查询并按创建时间倒序排序
    leaves = query.order_by(InformationModel.create_time.desc()).all()
    
    # 构建返回数据，按批准状态分组
    approved_leaves = []
    pending_leaves = []
    
    for leave in leaves:
        # 获取请假学生信息
        student = UserModel.query.filter_by(id=leave.student_id).first()
        student_name = student.username if student else "未知用户"
        
        # 获取所属小组信息
        group = GroupModel.query.filter_by(group_id=leave.group_id).first()
        group_name = group.name if group else "未知小组"
        
        leave_data = {
            "id": leave.id,
            "group_id": leave.group_id,
            "group_name": group_name,
            "title": leave.title,
            "content": leave.content,
            "student_id": leave.student_id,
            "student_name": student_name,
            "start_time": leave.start_time.strftime("%Y-%m-%d %H:%M:%S") if leave.start_time else None,
            "end_time": leave.end_time.strftime("%Y-%m-%d %H:%M:%S") if leave.end_time else None,
            "status": leave.status,
            "create_time": leave.create_time.strftime("%Y-%m-%d %H:%M:%S")
        }
        
        # 根据status分组
        if leave.status == 1:  # 已批准
            approved_leaves.append(leave_data)
            print(f"添加到已批准请假列表，当前数量: {len(leave_approved)}")
        else:  # 未批准
            pending_leaves.append(leave_data)
            print(f"添加到未批准请假列表，当前数量: {len(leave_pending)}")
                    
    return jsonify({
        "code": 200,
        "message": "查询成功",
        "data": {
            "approved": approved_leaves,
            "pending": pending_leaves
        }
    }),200

@bp.route("/information/leave/approve", methods=["POST"])
@jwt_required()
@swag_from('../apidocs/information/leave/approve.yaml')
def leave_approve():
    """
    批准请假申请
    """
    # 获取请求数据
    data = request.get_json()
    leave_id = data.get("id")
    status = data.get("status")
    
    if not leave_id:
        return jsonify({
            "code": 400,
            "message": "请假ID不能为空" 
        }),400
    
    # 获取当前用户
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    if not user:
        return jsonify({
            "code": 404,
            "message": "用户不存在"
        }),404
    
    # 查找请假信息
    leave = InformationModel.query.filter_by(id=leave_id, type=1).first()
    
    if not leave:
        return jsonify({
            "code": 404,
            "message": "请假信息不存在"
        }),404
    
    # 验证批准权限：只有组长可以批准请假
    group = GroupModel.query.filter_by(group_id=leave.group_id).first()
    if not group:
        return jsonify({
            "code": 404,
            "message": "小组不存在"
        }),404
    
    # 检查是否为组长或管理员
    is_group_leader = (group.teacher_id == user.id)
    is_admin = (user.user_mode == 'admin')
    
    if not (is_group_leader or is_admin):
        return jsonify({
            "code": 403,
            "message": "无权批准请假，仅组长或管理员可批准"
        }),403
    
    # 更新请假状态为已批准
    previous_status = leave.status  # 记录之前的状态
    leave.status = status
    
    # 如果请假开始/结束时间只有日期部分，处理为23:59:59
    if leave.start_time:
        leave.start_time = process_datetime(leave.start_time)
    if leave.end_time:
        leave.end_time = process_datetime(leave.end_time)
    
    db.session.commit()
    
    # 创建提醒给请假的学生
    student = UserModel.query.filter_by(id=leave.student_id).first()
    if student:
        status_text = "已批准" if status == 1 else "已拒绝"
        reminder_title = f"请假申请{status_text}: {leave.title}"
        reminder_content = f"您的请假申请\"{leave.title}\"已被{user.username}{status_text}"
        
        create_reminder(
            title=reminder_title,
            content=reminder_content,
            related_info_id=leave_id,
            student_id=student.id,
            group_id=leave.group_id,
            source_type=1  # 1代表请假信息
        )
    
    return jsonify({
        "code": 200,
        "status": status,
        "message": "请假状态已修改"
    }),200

@bp.route("/information/task/add", methods=["POST"])
@jwt_required()
@swag_from('../apidocs/information/task/add.yaml')
def task_add():
    """
    添加或修改任务信息
    """
    form = TaskForm()
    if form.validate():
        # 获取表单数据
        task_id = form.Id.data
        group_id = form.Group_Id.data
        title = form.Title.data
        content = form.Content.data
        end_time = form.End_Time.data  # 表单类已处理日期时间格式
        priority = form.Priority.data or 5  # 默认为低等优先级
        
        # 获取当前用户
        user_email = get_jwt_identity()
        user = UserModel.query.filter_by(email=user_email).first()
        if not user:
            return jsonify({
                "code": 404,
                "message": "用户不存在"
            }),404
        
        # 检查组是否存在
        group = GroupModel.query.filter_by(group_id=group_id).first()
        if not group:
            return jsonify({
                "code": 400,
                "message": "小组不存在"
            }),400
        
        # 检查用户是否为组长（只有组长可以创建任务）
        if group.teacher_id != user.id and user.user_mode != 'admin':
            return jsonify({
                "code": 403,
                "message": "只有组长或管理员可以创建任务"
            }),403
        
        # 判断是添加还是修改
        if task_id:
            # 修改现有任务
            task = InformationModel.query.filter_by(id=task_id, type=2).first()
            if not task:
                return jsonify({
                    "code": 404,
                    "message": "任务信息不存在"
                }),404
            
            # 检查任务所属组是否与表单中的组匹配
            if task.group_id != group_id:
                return jsonify({
                    "code": 400,
                    "message": "任务所属小组与提供的小组ID不匹配"
                }),400
            
            # 更新任务信息
            task.title = title
            task.content = content
            task.end_time = end_time
            task.priority = priority
            
            db.session.commit()
            
            # 创建任务更新提醒，通知所有组内成员
            priority_text = ["", "紧急", "高优先级", "中优先级", "低优先级", "普通"][priority] if 1 <= priority <= 5 else "普通"
            
            # 查询小组内的所有学生
            students = GroupModel.query.filter_by(group_id=group_id).all()
            student_ids = set([s.student_id for s in students if s.student_id])
            
            for student_id in student_ids:
                reminder_title = f"任务已更新: {title}"
                reminder_content = f"组长{user.username}已更新了一个{priority_text}任务: {title}"
                if end_time:
                    # 检查end_time是否为字符串类型，若是则直接使用，否则调用strftime
                    if isinstance(end_time, str):
                        reminder_content += f", 截止时间: {end_time}"
                    else:
                        reminder_content += f", 截止时间: {end_time.strftime('%Y-%m-%d %H:%M:%S')}"
                
                create_reminder(
                    title=reminder_title,
                    content=reminder_content,
                    related_info_id=task_id,
                    student_id=student_id,
                    group_id=group_id,
                    source_type=2  # 2代表任务信息
                )
            
            return jsonify({
                "code": 200,
                "message": "任务修改成功",
                "data": {
                    "id": task.id
                }
            }),200
        else:
            # 创建新任务
            information = InformationModel(
                group_id=group_id,
                type=2,  # 2代表任务信息
                title=title,
                content=content,
                end_time=end_time,
                priority=priority,
                student_id=0  # 设置默认值为0，避免数据库错误
                # 不再记录student_id
            )
            
            db.session.add(information)
            db.session.commit()
            
            # 创建新任务提醒，通知所有组内成员
            priority_text = ["", "紧急", "高", "中", "低", "不重要"][priority] if 1 <= priority <= 5 else "鬼都不管"
            
            # 查询小组内的所有学生
            students = GroupModel.query.filter_by(group_id=group_id).all()
            student_ids = set([s.student_id for s in students if s.student_id])
            
            for student_id in student_ids:
                reminder_title = f"新任务: {title}"
                reminder_content = f"组长{user.username}发布了一个{priority_text}任务: {title}"
                if end_time:
                    # 检查end_time是否为字符串类型，若是则直接使用，否则调用strftime
                    if isinstance(end_time, str):
                        reminder_content += f", 截止时间: {end_time}"
                    else:
                        reminder_content += f", 截止时间: {end_time.strftime('%Y-%m-%d %H:%M:%S')}"
                
                create_reminder(
                    title=reminder_title,
                    content=reminder_content,
                    related_info_id=information.id,
                    student_id=student_id,
                    group_id=group_id,
                    source_type=2  # 2代表任务信息
                )
            
            return jsonify({
                "code": 200,
                "message": "任务创建成功",
                "data": {
                    "id": information.id
                }
            }),200
    else:
        return jsonify({
            "code": 400,
            "message": form.errors
        }),400

@bp.route("/information/task/delete", methods=["POST"])
@jwt_required()
@swag_from('../apidocs/information/task/delete.yaml')
def task_delete():
    """
    删除任务信息
    """
    # 获取请求数据
    data = request.get_json()
    task_id = data.get("id")
    
    if not task_id:
        return jsonify({
            "code": 400,
            "message": "任务ID不能为空" 
        }),400
    
    # 获取当前用户
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    if not user:
        return jsonify({
            "code": 404,
            "message": "用户不存在"
        }),404
    
    # 查找任务信息
    task = InformationModel.query.filter_by(id=task_id, type=2).first()
    
    if not task:
        return jsonify({
            "code": 404,
            "message": "任务信息不存在"
        }),404
    
    # 验证删除权限：只有组长和管理员可删除任务
    is_admin = (user.user_mode == 'admin')
    is_group_leader = False
    
    # 查询小组信息，检查用户是否为小组组长
    group = GroupModel.query.filter_by(group_id=task.group_id).first()
    if group and group.teacher_id == user.id:
        is_group_leader = True
    
    # 判断是否有删除权限
    if not (is_group_leader or is_admin):
        return jsonify({
            "code": 403,
            "message": "无权删除此任务，仅组长或管理员可删除"
        }),403
    
    # 删除关联的提醒信息
    reminders_deleted = delete_related_reminders(task_id)
    
    # 删除任务信息
    db.session.delete(task)
    db.session.commit()
    
    return jsonify({
        "code": 200,
        "message": "任务删除成功",
        "data": {
            "reminders_deleted": reminders_deleted
        }
    }),200

@bp.route("/information/task/query", methods=["GET"])
@jwt_required()
@swag_from('../apidocs/information/task/query.yaml')
def task_query():
    """
    查询任务信息 - 支持查询方式：
    1. 管理员通过小组ID查询特定小组的任务信息（需提供group_id）
    2. 普通用户查询所属小组的任务信息
    """
    # 获取查询参数
    group_id = request.args.get("group_id", type=int)
    priority = request.args.get("priority", type=int)
    
    # 获取当前用户
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    if not user:
        return jsonify({
            "code": 404,
            "message": "用户不存在"
        }),404
    
    # 构建基础查询条件
    query = InformationModel.query.filter_by(type=2)  # 类型2为任务信息
    
    # 如果通过小组ID查询，检查用户是否为管理员
    if group_id:
        query = query.filter_by(group_id=group_id)
    else:
        # 查询用户所在的小组
        user_groups = GroupModel.query.filter((GroupModel.teacher_id == user.id) | (GroupModel.student_id == user.id)).all()
        group_ids = [group.group_id for group in user_groups]
        
        if not group_ids:
            # 用户不属于任何小组
            return jsonify({
                "code": 200,
                "message": "查询成功，但无相关任务信息",
                "data": {
                    "urgent_priority": [],
                    "high_priority": [],
                    "medium_priority": [],
                    "low_priority": [],
                    "unimportant_priority": []
                }
            }),200
        
        # 查询这些小组的任务
        query = query.filter(InformationModel.group_id.in_(group_ids))
    
    # 根据优先级筛选
    if priority is not None and 1 <= priority <= 5:
        query = query.filter_by(priority=priority)
    
    # 执行查询并按创建时间倒序排序
    tasks = query.order_by(InformationModel.create_time.desc()).all()
    
    # 构建返回数据，按优先级分组
    urgent_priority = [] # 优先级1
    high_priority = []   # 优先级2
    medium_priority = [] # 优先级3
    low_priority = []    # 优先级4
    unimportant_priority = [] # 优先级5
    
    for task in tasks:
        # 获取所属小组信息
        group = GroupModel.query.filter_by(group_id=task.group_id).first()
        group_name = group.name if group else "未知小组"
        
        # 获取小组成员信息
        submitted_students = []
        not_submitted_students = []
        
        if group:
            # 获取小组所有学生
            students_in_group = GroupModel.query.filter_by(group_id=task.group_id).all()
            student_ids = [student.student_id for student in students_in_group if student.student_id]
            
            # 获取已提交作业的学生ID列表
            submitted_student_ids = []
            if task.students_id is not None:
                # 处理task.students_id，确保正确解析
                if isinstance(task.students_id, str) and task.students_id.strip():
                    # 解析逗号分隔的字符串
                    submitted_student_ids = [int(sid.strip()) for sid in task.students_id.split(",") if sid.strip().isdigit()]
            
            # 构建已提交和未提交的学生列表
            for student_id in student_ids:
                student = UserModel.query.filter_by(id=student_id).first()
                if not student:
                    continue
                
                student_info = {
                    "id": student.id,
                    "name": student.username
                }
                
                if student.id in submitted_student_ids:
                    submitted_students.append(student_info)
                else:
                    not_submitted_students.append(student_info)
        
        task_data = {
            "id": task.id,
            "group_id": task.group_id,
            "group_name": group_name,
            "title": task.title,
            "content": task.content,
            "end_time": task.end_time.strftime("%Y-%m-%d %H:%M:%S") if task.end_time else None,
            "priority": task.priority,
            "create_time": task.create_time.strftime("%Y-%m-%d %H:%M:%S"),
            "submitted_students": submitted_students,
            "not_submitted_students": not_submitted_students
        }
        
        # 确保优先级是整数类型
        try:
            priority_value = int(task.priority) if task.priority is not None else 3
        except (ValueError, TypeError):
            priority_value = 3  # 默认为中等优先级
        
        # 根据优先级分组
        if priority_value == 1:
            urgent_priority.append(task_data)
        elif priority_value == 2:
            high_priority.append(task_data)
        elif priority_value == 3:
            medium_priority.append(task_data)
        elif priority_value == 4:
            low_priority.append(task_data)
        elif priority_value == 5:
            unimportant_priority.append(task_data)
    
    return jsonify({
        "code": 200,
        "message": "查询成功",
        "data": {
            "urgent_priority": urgent_priority,
            "high_priority": high_priority,
            "medium_priority": medium_priority,
            "low_priority": low_priority,
            "unimportant_priority": unimportant_priority
        }
    }),200

@bp.route("/information/notice/add", methods=["POST"])
@jwt_required()
@swag_from('../apidocs/information/notice/add.yaml')
def notice_add():
    """
    添加或修改通知信息
    """
    form = NoticeForm()
    if form.validate():
        # 获取表单数据
        notice_id = form.Id.data
        group_id = form.Group_Id.data
        title = form.Title.data
        content = form.Content.data
        range_str = form.Range.data or "0"  # 默认为全组通知
        
        # 获取当前用户
        user_email = get_jwt_identity()
        user = UserModel.query.filter_by(email=user_email).first()
        if not user:
            return jsonify({
                "code": 404,
                "message": "用户不存在"
            }),404
        
        # 检查组是否存在
        group = GroupModel.query.filter_by(group_id=group_id).first()
        if not group:
            return jsonify({
                "code": 400,
                "message": "小组不存在"
            }),400
        
        # 检查用户是否为组长（只有组长可以发布通知）
        if group.teacher_id != user.id and user.user_mode != 'admin':
            return jsonify({
                "code": 403,
                "message": "只有组长或管理员可以发布通知"
            }),403
        
        # 判断是添加还是修改
        if notice_id:
            # 修改现有通知
            notice = InformationModel.query.filter_by(id=notice_id, type=3).first()
            if not notice:
                return jsonify({
                    "code": 404,
                    "message": "通知信息不存在"
                }),404
            
            # 检查通知所属组是否与表单中的组匹配
            if notice.group_id != group_id:
                return jsonify({
                    "code": 400,
                    "message": "通知所属小组与提供的小组ID不匹配"
                }),400
            
            # 更新通知信息
            notice.title = title
            notice.content = content
            notice.range = range_str
            
            db.session.commit()
            
            # 创建通知更新提醒
            # 确定通知的接收者
            if range_str == "0":
                # 全组通知，查询小组内的所有学生
                students = GroupModel.query.filter_by(group_id=group_id).all()
                recipient_ids = set([s.student_id for s in students if s.student_id])
            else:
                # 指定用户通知
                recipient_ids = set([int(uid.strip()) for uid in range_str.split(",") if uid.strip().isdigit()])
            
            # 为每个接收者创建提醒
            for recipient_id in recipient_ids:
                if recipient_id:  # 确保接收者ID不为空
                    reminder_title = f"通知已更新: {title}"
                    reminder_content = f"{user.username}已更新了一条通知: {title}"
                    
                    create_reminder(
                        title=reminder_title,
                        content=reminder_content,
                        related_info_id=notice_id,
                        student_id=recipient_id,
                        group_id=group_id,
                        source_type=3  # 3代表通知信息
                    )
            
            return jsonify({
                "code": 200,
                "message": "通知修改成功",
                "data": {
                    "id": notice.id
                }
            }),200
        else:
            # 创建新通知
            information = InformationModel(
                group_id=group_id,
                type=3,  # 3代表通知信息
                title=title,
                content=content,
                range=range_str,
                student_id=user.id  # 设置创建者ID，避免student_id为null
            )
            
            db.session.add(information)
            db.session.commit()
            
            # 创建新通知提醒
            # 确定通知的接收者
            if range_str == "0":
                # 全组通知，查询小组内的所有学生
                students = GroupModel.query.filter_by(group_id=group_id).all()
                recipient_ids = set([s.student_id for s in students if s.student_id])
            else:
                # 指定用户通知
                recipient_ids = set([int(uid.strip()) for uid in range_str.split(",") if uid.strip().isdigit()])
            
            # 为每个接收者创建提醒
            for recipient_id in recipient_ids:
                if recipient_id:  # 确保接收者ID不为空
                    reminder_title = f"新通知: {title}"
                    reminder_content = f"{user.username}发布了一条新通知: {title}"
                    
                    create_reminder(
                        title=reminder_title,
                        content=reminder_content,
                        related_info_id=information.id,
                        student_id=recipient_id,
                        group_id=group_id,
                        source_type=3  # 3代表通知信息
                    )
            
            return jsonify({
                "code": 200,
                "message": "通知发布成功",
                "data": {
                    "id": information.id
                }
            }),200
    else:
        return jsonify({
            "code": 400,
            "message": form.errors
        }),400

@bp.route("/information/notice/delete", methods=["POST"])
@jwt_required()
@swag_from('../apidocs/information/notice/delete.yaml')
def notice_delete():
    """
    删除通知信息
    """
    # 获取请求数据
    data = request.get_json()
    notice_id = data.get("id")
    
    if not notice_id:
        return jsonify({
            "code": 400,
            "message": "通知ID不能为空" 
        }),400
    
    # 获取当前用户
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    if not user:
        return jsonify({
            "code": 404,
            "message": "用户不存在"
        }),404
    
    # 查找通知信息
    notice = InformationModel.query.filter_by(id=notice_id, type=3).first()
    
    if not notice:
        return jsonify({
            "code": 404,
            "message": "通知信息不存在"
        }),404
    
    # 验证删除权限：只有组长或管理员可以删除通知
    is_admin = (user.user_mode == 'admin')
    is_group_leader = False
    
    # 查询小组信息，检查用户是否为小组组长
    group = GroupModel.query.filter_by(group_id=notice.group_id).first()
    if group and group.teacher_id == user.id:
        is_group_leader = True
    
    # 判断是否有删除权限
    if not (is_group_leader or is_admin):
        return jsonify({
            "code": 403,
            "message": "无权删除此通知，仅组长或管理员可删除"
        }),403
    
    # 删除关联的提醒信息
    reminders_deleted = delete_related_reminders(notice_id)
    
    # 删除通知信息
    db.session.delete(notice)
    db.session.commit()
    
    return jsonify({
        "code": 200,
        "message": "通知删除成功",
        "data": {
            "reminders_deleted": reminders_deleted
        }
    }),200

@bp.route("/information/notice/query", methods=["GET"])
@jwt_required()
@swag_from('../apidocs/information/notice/query.yaml')
def notice_query():
    """
    查询通知信息 - 支持查询方式：
    1. 管理员通过小组ID查询特定小组的通知信息（需提供group_id）
    2. 普通用户查询所属小组的通知信息
    """
    # 获取查询参数
    group_id = request.args.get("group_id", type=int)
    
    # 获取当前用户
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    if not user:
        return jsonify({
            "code": 404,
            "message": "用户不存在"
        }),404
    
    # 构建基础查询条件
    query = InformationModel.query.filter_by(type=3)  # 类型3为通知信息
    
    # 如果通过小组ID查询，检查用户是否为管理员
    if group_id:
        query = query.filter_by(group_id=group_id)
    else:
        # 查询用户所在的小组
        user_groups = GroupModel.query.filter((GroupModel.teacher_id == user.id) | (GroupModel.student_id == user.id)).all()
        group_ids = [group.group_id for group in user_groups]
        
        if not group_ids:
            # 用户不属于任何小组
            return jsonify({
                "code": 200,
                "message": "查询成功，但无相关通知信息",
                "data": []
            }),200
        
        # 查询这些小组的通知
        query = query.filter(InformationModel.group_id.in_(group_ids))
    
    # 执行查询并按创建时间倒序排序
    notices = query.order_by(InformationModel.create_time.desc()).all()
    
    # 构建返回数据
    result = []
    for notice in notices:
        # 检查通知范围是否包含当前用户
        # 如果range为0，表示全组通知；否则检查用户ID是否在range中
        if notice.range != "0":
            # 检查学生是否在通知范围内
            if user.user_mode != 'admin' and str(user.id) not in notice.range.split(","):
                # 该通知不是发给当前用户的
                continue
        
        # 获取所属小组信息
        group = GroupModel.query.filter_by(group_id=notice.group_id).first()
        group_name = group.name if group else "未知小组"
        
        # 处理通知范围
        recipients = []
        if notice.range == "0":
            recipients_desc = "全组成员"
        else:
            # 获取每个接收者的信息
            recipient_ids = notice.range.split(",")
            for recipient_id in recipient_ids:
                try:
                    recipient = UserModel.query.filter_by(id=int(recipient_id)).first()
                    if recipient:
                        recipients.append(recipient.username)
                except:
                    pass
            recipients_desc = "、".join(recipients) if recipients else "未指定"
        
        notice_data = {
            "id": notice.id,
            "group_id": notice.group_id,
            "group_name": group_name,
            "title": notice.title,
            "content": notice.content,
            "recipients": recipients_desc,
            "create_time": notice.create_time.strftime("%Y-%m-%d %H:%M:%S")
        }
        
        result.append(notice_data)
    
    return jsonify({
        "code": 200,
        "message": "查询成功",
        "data": result
    }),200

@bp.route("/information/query/all", methods=["GET"])
@jwt_required()
@swag_from('../apidocs/information/query/all.yaml')
def information_query_all():
    """
    集成式查询API - 查询用户所在小组的所有信息
    支持多种可选参数进行精确搜索
    """
    # 获取查询参数（全部可选）
    group_id = request.args.get("group_id", type=int)
    info_type = request.args.get("type", type=int)  # 1:请假 2:任务 3:通知
    status = request.args.get("status", type=int)  # 0:未批准 1:已批准（仅对请假信息有效）
    end_time_str = request.args.get("end_time")  # 截止时间，格式为YYYY-MM-DD
    create_time_str = request.args.get("create_time")  # 创建时间，格式为YYYY-MM-DD
    
    # 打印所有请求参数，检查是否有隐藏参数
    print(f"请求参数: {dict(request.args)}")
    
    # 处理日期参数
    end_time = None
    create_time = None
    
    if end_time_str:
        try:
            end_time = datetime.datetime.strptime(end_time_str, "%Y-%m-%d")
            # 设置为当天23:59:59
            end_time = datetime.datetime(end_time.year, end_time.month, end_time.day, 23, 59, 59)
        except ValueError:
            return jsonify({
                "code": 400,
                "message": "截止时间格式错误，应为YYYY-MM-DD"
            }), 400
    
    if create_time_str:
        try:
            create_time = datetime.datetime.strptime(create_time_str, "%Y-%m-%d")
            # 设置为当天23:59:59
            create_time = datetime.datetime(create_time.year, create_time.month, create_time.day, 23, 59, 59)
        except ValueError:
            return jsonify({
                "code": 400,
                "message": "创建时间格式错误，应为YYYY-MM-DD"
            }), 400
    
    # 获取当前用户
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    if not user:
        return jsonify({
            "code": 404,
            "message": "用户不存在"
        }), 404
    
    # 查询用户所在的小组
    if group_id:
        # 如果指定了小组ID，检查用户是否属于该小组
        group = GroupModel.query.filter_by(group_id=group_id).first()
        if not group:
            return jsonify({
                "code": 404,
                "message": "小组不存在"
            }), 404
        
        # 检查用户是否为管理员或属于该小组
        if user.user_mode != 'admin' and group.teacher_id != user.id and group.student_id != user.id:
            return jsonify({
                "code": 403,
                "message": "权限不足，您不是该小组成员"
            }), 403
        
        group_ids = [group_id]
    else:
        # 查询用户所在的所有小组
        if user.user_mode == 'admin':
            # 管理员可以查看所有小组
            groups = GroupModel.query.all()
        else:
            # 查询用户作为教师（组长）的小组
            teacher_groups = GroupModel.query.filter_by(teacher_id=user.id).all()
            # 查询用户作为学生的小组
            student_groups = GroupModel.query.filter_by(student_id=user.id).all()
            # 合并两个查询结果
            groups = teacher_groups + student_groups
        
        # 去重，避免重复的小组ID
        unique_groups = {}
        for group in groups:
            unique_groups[group.group_id] = group
        
        group_ids = list(unique_groups.keys())
        
        # 调试信息：打印用户ID和查询到的小组IDs
        print(f"用户ID: {user.id}, 用户模式: {user.user_mode}, 查询到的小组IDs: {group_ids}")
        
        if not group_ids:
            # 用户不属于任何小组
            return jsonify({
                "code": 200,
                "message": "查询成功，但无相关信息",
                "data": {
                    "leave": {
                        "approved": [],
                        "pending": []
                    },
                    "task": [],
                    "notice": []
                }
            }), 200
    
    # 构建基础查询
    query = InformationModel.query.filter(InformationModel.group_id.in_(group_ids))
    
    # 根据类型筛选
    if info_type:
        if info_type not in [1, 2, 3]:
            return jsonify({
                "code": 400,
                "message": "信息类型错误，应为1(请假)、2(任务)或3(通知)"
            }), 400
        query = query.filter_by(type=info_type)
    
    # 根据截止时间筛选
    if end_time:
        query = query.filter(InformationModel.end_time <= end_time)
    
    # 根据创建时间筛选
    if create_time:
        # 创建时间为当天或之前
        next_day = create_time + datetime.timedelta(days=1)
        query = query.filter(InformationModel.create_time < next_day)
    
    # 执行查询并按创建时间倒序排序
    infos = query.order_by(InformationModel.create_time.desc()).all()
    
    # 调试信息：打印查询到的信息数量
    print(f"查询到的信息数量: {len(infos)}")
    
    # 详细打印每个信息对象的属性
    for info in infos:
        print("\n===== 信息对象详情 =====")
        print(f"ID: {info.id}")
        print(f"类型: {info.type}")
        print(f"小组ID: {info.group_id}")
        print(f"标题: {info.title}")
        print(f"内容: {info.content}")
        print(f"创建时间: {info.create_time}")
        
        # 打印可能存在的属性
        for attr in ['student_id', 'start_time', 'end_time', 'status', 'priority', 'range']:
            if hasattr(info, attr):
                print(f"{attr}: {getattr(info, attr)}")
        print("=======================\n")
    
    # 按类型分类结果
    leave_approved = []
    leave_pending = []
    tasks = []
    notices = []
    
    for info in infos:
        try:
            # 调试信息：打印每条信息的详细信息
            print(f"处理信息ID: {info.id}, 类型: {info.type}, 小组ID: {info.group_id}, 标题: {info.title}")
            
            # 获取所属小组信息
            group = GroupModel.query.filter_by(group_id=info.group_id).first()
            group_name = group.name if group else "未知小组"
            

            # 根据类型处理不同信息
            if info.type == "1":  # 请假信息
                print(f"处理请假信息")
                
                # 如果指定了状态，筛选请假信息
                if status is not None and info.status != status:
                    print(f"筛选状态: {status}, 信息状态: {info.status}")
                    print(f"状态不匹配，跳过")
                    continue
                
                leave_data = {
                    "id": info.id,
                    "group_id": info.group_id,
                    "group_name": group_name,
                    "title": info.title,
                    "content": info.content,
                    "student_id": info.student_id,
                    "start_time": info.start_time.strftime("%Y-%m-%d %H:%M:%S") if info.start_time else None,
                    "end_time": info.end_time.strftime("%Y-%m-%d %H:%M:%S") if info.end_time else None,
                    "status": info.status,
                    "create_time": info.create_time.strftime("%Y-%m-%d %H:%M:%S")
                }
                
                # 获取学生信息
                student = UserModel.query.filter_by(id=info.student_id).first()
                leave_data["student_name"] = student.username if student else "未知用户"
                
                # 根据批准状态分类
                if info.status == 1:
                    leave_approved.append(leave_data)
                    print(f"添加到已批准请假列表，当前数量: {len(leave_approved)}")
                else:
                    leave_pending.append(leave_data)
                    print(f"添加到未批准请假列表，当前数量: {len(leave_pending)}")
                    
            elif info.type == "2":  # 任务信息
                print(f"处理任务信息")
                
                # 确保优先级是整数类型
                try:
                    priority_value = int(info.priority) if info.priority is not None else 3
                except (ValueError, TypeError):
                    priority_value = 3  # 默认为中等优先级
                    
                task_data = {
                    "id": info.id,
                    "group_id": info.group_id,
                    "group_name": group_name,
                    "title": info.title,
                    "content": info.content,
                    "end_time": info.end_time.strftime("%Y-%m-%d %H:%M:%S") if info.end_time else None,
                    "priority": priority_value,
                    "create_time": info.create_time.strftime("%Y-%m-%d %H:%M:%S")
                }
                
                tasks.append(task_data)
                print(f"添加到任务列表，当前数量: {len(tasks)}")
                
            elif info.type == "3":  # 通知信息
                print(f"处理通知信息")
                
                # 检查通知范围
                if info.range != "0" and user.user_mode != 'admin':
                    if str(user.id) not in info.range.split(","):
                        # 该通知不是发给当前用户的
                        print(f"通知不是发给当前用户的，跳过")
                        continue
                
                # 处理通知范围
                recipients = []
                recipients_desc = "全组成员"  # 默认值
                
                if info.range == "0":
                    recipients_desc = "全组成员"
                else:
                    # 获取每个接收者的信息
                    recipient_ids = info.range.split(",")
                    for recipient_id in recipient_ids:
                        try:
                            recipient = UserModel.query.filter_by(id=int(recipient_id)).first()
                            if recipient:
                                recipients.append(recipient.username)
                        except:
                            pass
                    recipients_desc = "、".join(recipients) if recipients else "未指定"
                
                notice_data = {
                    "id": info.id,
                    "group_id": info.group_id,
                    "group_name": group_name,
                    "title": info.title,
                    "content": info.content,
                    "recipients": recipients_desc,
                    "create_time": info.create_time.strftime("%Y-%m-%d %H:%M:%S")
                }
                
                notices.append(notice_data)
                print(f"添加到通知列表，当前数量: {len(notices)}")
        except Exception as e:
            # 捕获处理单条信息时的异常，避免影响其他信息的处理
            print(f"处理信息ID: {info.id} 时出错: {str(e)}")
            import traceback
            traceback.print_exc()
    
    # 调试信息：打印分类后的信息数量
    print(f"请假信息(已批准): {len(leave_approved)}, 请假信息(未批准): {len(leave_pending)}, 任务信息: {len(tasks)}, 通知信息: {len(notices)}")
    
    # 构建返回数据
    return jsonify({
        "code": 200,
        "message": "查询成功",
        "data": {
            "leave": {
                "approved": leave_approved,
                "pending": leave_pending
            },
            "task": tasks,
            "notice": notices
        }
    }), 200

@bp.route("/information/error/add", methods=["POST"])
@jwt_required()
@swag_from('../apidocs/information/error/add.yaml')
def error_add():
    """
    添加报错信息
    """
    # 获取当前用户
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    if not user:
        return jsonify({
            "code": 404,
            "message": "用户不存在"
        }), 404
    
    # 获取表单数据
    title = request.form.get('title')
    content = request.form.get('content')
    
    if not title:
        return jsonify({
            "code": 400,
            "message": "报错标题不能为空"
        }), 400
    
    # 创建报错信息
    error_info = InformationModel(
        group_id=0,  # 报错信息不属于任何小组
        type=4,  # 4代表报错信息
        title=title,
        content=content,
        student_id=user.id,  # 记录报错用户ID
        resource=None  # 初始时没有图片资源
    )
    
    db.session.add(error_info)
    db.session.commit()
    
    # 处理图片上传
    if 'image' in request.files:
        file = request.files['image']
        if file and file.filename:
            # 确保目录存在
            os.makedirs('./data/error_images', exist_ok=True)
            
            # 保存图片
            filename = f"{error_info.id}.{file.filename.rsplit('.', 1)[1].lower()}"
            file_path = os.path.join('./data/error_images', filename)
            file.save(file_path)
            
            # 更新数据库中的资源路径
            error_info.resource = filename
            db.session.commit()
            
            # 将图片缓存到Redis
            try:
                with open(file_path, 'rb') as image_file:
                    image_stream = image_file.read()
                    image_base64 = base64.b64encode(image_stream).decode()
                    
                    # 缓存到Redis（7天过期）
                    cache_key = f"error_image:base64:{error_info.id}"
                    redis_client.setex(cache_key, 60*60*24*7, image_base64)
            except Exception as e:
                print(f"图片缓存失败: {str(e)}")
    
    return jsonify({
        "code": 200,
        "message": "报错信息提交成功",
        "data": {
            "id": error_info.id
        }
    })

@bp.route("/information/error/query", methods=["GET"])
@jwt_required()
@swag_from('../apidocs/information/error/query.yaml')
def error_query():
    """
    查询用户自己的报错信息
    """
    # 获取当前用户
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    if not user:
        return jsonify({
            "code": 404,
            "message": "用户不存在"
        }), 404
    
    # 查询用户的报错信息
    errors = InformationModel.query.filter_by(
        type=4,  # 4代表报错信息
        student_id=user.id  # 只能查看自己的报错
    ).order_by(InformationModel.create_time.desc()).all()
    
    result = []
    for error in errors:
        error_data = {
            "id": error.id,
            "title": error.title,
            "content": error.content,
            "create_time": error.create_time.strftime("%Y-%m-%d %H:%M:%S"),
            "has_image": bool(error.resource)
        }
        
        # 如果有图片资源，获取图片的Base64编码
        if error.resource:
            # 定义Redis缓存键
            cache_key = f"error_image:base64:{error.id}"
            
            # 尝试从Redis缓存中获取Base64编码
            cached_base64 = redis_client.get(cache_key)
            
            if cached_base64:
                error_data["image"] = cached_base64.decode('utf-8')
            else:
                # 如果缓存未命中，从文件系统读取
                image_path = os.path.join('./data/error_images', error.resource)
                if os.path.exists(image_path):
                    try:
                        with open(image_path, 'rb') as image_file:
                            image_stream = image_file.read()
                            image_base64 = base64.b64encode(image_stream).decode()
                            
                            # 缓存到Redis
                            redis_client.setex(cache_key, 60*60*24*7, image_base64)
                            
                            error_data["image"] = image_base64
                    except Exception as e:
                        print(f"读取图片失败: {str(e)}")
        
        result.append(error_data)
    
    return jsonify({
        "code": 200,
        "message": "查询成功",
        "data": result
    })

@bp.route("/information/error/delete", methods=["POST"])
@jwt_required()
@swag_from('../apidocs/information/error/delete.yaml')
def error_delete():
    """
    删除报错信息
    """
    # 获取请求数据
    data = request.get_json()
    error_id = data.get("id")
    
    if not error_id:
        return jsonify({
            "code": 400,
            "message": "报错ID不能为空"
        }), 400
    
    # 获取当前用户
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    if not user:
        return jsonify({
            "code": 404,
            "message": "用户不存在"
        }), 404
    
    # 查找报错信息
    error = InformationModel.query.filter_by(id=error_id, type=4).first()
    
    if not error:
        return jsonify({
            "code": 404,
            "message": "报错信息不存在"
        }), 404
    
    # 验证删除权限：只有报错人自己可以删除
    if error.student_id != user.id:
        return jsonify({
            "code": 403,
            "message": "无权删除此报错信息，仅报错人本人可删除"
        }), 403
    
    # 如果有图片资源，删除相关文件和缓存
    if error.resource:
        # 删除图片文件
        image_path = os.path.join('./data/error_images', error.resource)
        if os.path.exists(image_path):
            try:
                os.remove(image_path)
            except Exception as e:
                print(f"删除图片文件失败: {str(e)}")
        
        # 删除Redis缓存
        cache_key = f"error_image:base64:{error.id}"
        redis_client.delete(cache_key)
    
    # 删除报错信息
    db.session.delete(error)
    db.session.commit()
    
    return jsonify({
        "code": 200,
        "message": "报错信息删除成功"
    })

@bp.route("/information/homework/add", methods=["POST"])
@jwt_required()
@swag_from('../apidocs/information/homework/add.yaml')
def homework_add():
    """
    添加作业信息
    """
    # 获取当前用户
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    if not user:
        return jsonify({
            "code": 404,
            "message": "用户不存在"
        }), 404
    
    # 获取表单数据
    task_id = request.form.get('task_id')  # 对应的任务ID
    title = request.form.get('title')  # 作业标题
    content = request.form.get('content')  # 作业内容/说明
    
    if not task_id:
        return jsonify({
            "code": 400,
            "message": "任务ID不能为空"
        }), 400
        
    if not title:
        return jsonify({
            "code": 400,
            "message": "作业标题不能为空"
        }), 400
    
    # 检查任务是否存在
    task = InformationModel.query.filter_by(id=task_id, type=2).first()
    if not task:
        return jsonify({
            "code": 404,
            "message": "对应的任务信息不存在"
        }), 404
    
    # 检查用户是否已经提交过该任务的作业
    existing_homework = InformationModel.query.filter_by(
        type=5,  # 作业信息
        student_id=user.id,
        range=task_id
    ).first()
    
    if existing_homework:
        return jsonify({
            "code": 409,
            "message": "您已提交过该任务的作业，请使用修改功能"
        }), 409
    
    # 检查是否有文件上传（非必需）
    files = []
    saved_files = []
    
    if 'files' in request.files:
        files = request.files.getlist('files')
        if files and files[0].filename != '':
            # 检查文件大小总和
            total_size = 0
            for file in files:
                file.seek(0, os.SEEK_END)
                total_size += file.tell()
                file.seek(0)
            
            if total_size > 20 * 1024 * 1024:  # 20MB限制
                return jsonify({
                    "code": 400,
                    "message": "文件总大小超过20MB限制"
                }), 400
    
    # 创建作业信息
    homework = InformationModel(
        group_id=task.group_id,  # 使用任务的小组ID
        type=5,  # 5代表作业信息
        title=title,
        content=content,
        student_id=user.id,  # 记录提交作业的学生ID
        status=0,  # 0表示未批改
        range=task_id  # 存储关联的任务ID
    )
    
    db.session.add(homework)
    db.session.commit()
    
    # 更新任务的students_id字段，添加当前学生ID
    # 检查用户是否为组长（组长不应该被添加到students_id中）
    is_group_leader = GroupModel.query.filter_by(group_id=task.group_id, teacher_id=user.id).first() is not None
    
    # 只有当用户不是组长时，才添加其ID到任务的students_id中
    if not is_group_leader:
        # 处理task.students_id，确保它是字符串格式
        current_student_ids = task.students_id or ""
        
        # 解析已有的学生ID列表
        student_id_list = []
        if current_student_ids:
            student_id_list = [sid.strip() for sid in current_student_ids.split(",") if sid.strip()]
        
        # 检查学生ID是否已存在
        student_id_str = str(user.id)
        if student_id_str not in student_id_list:
            student_id_list.append(student_id_str)
            # 更新任务的students_id字段
            task.students_id = ",".join(student_id_list)
            db.session.commit()
    
    # 如果有文件，保存文件
    if files and files[0].filename != '':
        # 创建作业文件目录
        homework_dir = os.path.join('BME_platform_flask/data/homework', str(homework.id))
        os.makedirs(homework_dir, exist_ok=True)
        
        # 保存文件
        for file in files:
            filename = file.filename
            file_path = os.path.join(homework_dir, filename)
            file.save(file_path)
            saved_files.append(filename)
        
        # 更新数据库中的资源路径，存储文件名列表
        homework.resource = ",".join(saved_files)
        db.session.commit()
    
    # 创建提醒给组长
    # 查找组长
    group = GroupModel.query.filter_by(group_id=task.group_id).first()
    if group and group.teacher_id:
        reminder_title = f"新作业提交: {title}"
        reminder_content = f"{user.username}提交了任务\"{task.title}\"的作业"
        
        create_reminder(
            title=reminder_title,
            content=reminder_content,
            related_info_id=homework.id,
            student_id=group.teacher_id,
            group_id=task.group_id,
            source_type=5  # 5代表作业信息
        )
    
    return jsonify({
        "code": 200,
        "message": "作业提交成功",
        "data": {
            "id": homework.id
        }
    })

@bp.route("/information/homework/update", methods=["POST"])
@jwt_required()
@swag_from('../apidocs/information/homework/update.yaml')
def homework_update():
    """
    修改作业信息
    """
    # 获取当前用户
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    if not user:
        return jsonify({
            "code": 404,
            "message": "用户不存在"
        }), 404
    
    # 获取表单数据
    homework_id = request.form.get('homework_id')  # 作业ID
    title = request.form.get('title')  # 作业标题
    content = request.form.get('content')  # 作业内容/说明
    
    if not homework_id:
        return jsonify({
            "code": 400,
            "message": "作业ID不能为空"
        }), 400
    
    # 查找作业信息
    homework = InformationModel.query.filter_by(id=homework_id, type=5).first()
    if not homework:
        return jsonify({
            "code": 404,
            "message": "作业信息不存在"
        }), 404
    
    # 验证修改权限：只有作业提交人可以修改
    if homework.student_id != user.id:
        return jsonify({
            "code": 403,
            "message": "无权修改此作业，仅提交人可修改"
        }), 403
    
    # 更新标题和内容
    if title:
        homework.title = title
    if content:
        homework.content = content
    
    # 如果有新文件上传
    if 'files' in request.files and request.files.getlist('files')[0].filename != '':
        files = request.files.getlist('files')
        
        # 检查文件大小总和
        total_size = 0
        for file in files:
            file.seek(0, os.SEEK_END)
            total_size += file.tell()
            file.seek(0)
        
        if total_size > 20 * 1024 * 1024:  # 20MB限制
            return jsonify({
                "code": 400,
                "message": "文件总大小超过20MB限制"
            }), 400
        
        # 删除旧文件
        homework_dir = os.path.join('BME_platform_flask/data/homework', str(homework.id))
        if os.path.exists(homework_dir):
            for filename in os.listdir(homework_dir):
                os.remove(os.path.join(homework_dir, filename))
        else:
            os.makedirs(homework_dir, exist_ok=True)
        
        # 保存新文件
        saved_files = []
        for file in files:
            filename = file.filename
            file_path = os.path.join(homework_dir, filename)
            file.save(file_path)
            saved_files.append(filename)
        
        # 更新数据库中的资源路径
        homework.resource = ",".join(saved_files)
    
    # 清除Redis缓存
    cache_key = f"homework_zip:{homework_id}"
    redis_client.delete(cache_key)
    print(f"已清除作业ID {homework_id} 的Redis缓存")
    
    db.session.commit()
    
    return jsonify({
        "code": 200,
        "message": "作业修改成功"
    })

@bp.route("/information/homework/delete", methods=["POST"])
@jwt_required()
@swag_from('../apidocs/information/homework/delete.yaml')
def homework_delete():
    """
    删除作业信息
    """
    # 获取请求数据
    data = request.get_json()
    homework_id = data.get("id")
    
    if not homework_id:
        return jsonify({
            "code": 400,
            "message": "作业ID不能为空"
        }), 400
    
    # 获取当前用户
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    if not user:
        return jsonify({
            "code": 404,
            "message": "用户不存在"
        }), 404
    
    # 查找作业信息
    homework = InformationModel.query.filter_by(id=homework_id, type=5).first()
    if not homework:
        return jsonify({
            "code": 404,
            "message": "作业信息不存在"
        }), 404
    
    # 验证删除权限：提交人或组长可删除
    is_submitter = (homework.student_id == user.id)
    is_group_leader = False
    
    # 查询小组信息，检查用户是否为小组组长
    group = GroupModel.query.filter_by(group_id=homework.group_id).first()
    if group and group.teacher_id == user.id:
        is_group_leader = True
    
    if not (is_submitter or is_group_leader):
        return jsonify({
            "code": 403,
            "message": "无权删除此作业，仅提交人或组长可删除"
        }), 403
        
    # 如果是组长删除学生作业，发送通知给学生
    if is_group_leader and not is_submitter:
        # 获取学生信息
        student = UserModel.query.filter_by(id=homework.student_id).first()
        if student:
            reminder_title = f"作业被退回: {homework.title}"
            reminder_content = f"您的作业\"{homework.title}\"已被{user.username}退回，请修改后再次提交"
            
            # 创建提醒给学生
            create_reminder(
                title=reminder_title,
                content=reminder_content,
                related_info_id=homework_id,
                student_id=homework.student_id,
                group_id=homework.group_id,
                source_type=5  # 5代表作业信息
            )
    
    # 删除关联的提醒信息
    reminders_deleted = delete_related_reminders(homework_id)
    
    # 删除作业文件
    homework_dir = os.path.join('BME_platform_flask/data/homework', str(homework.id))
    if os.path.exists(homework_dir):
        import shutil
        shutil.rmtree(homework_dir)
    
    # 清除Redis缓存
    cache_key = f"homework_zip:{homework_id}"
    redis_client.delete(cache_key)
    print(f"已清除作业ID {homework_id} 的Redis缓存")
    
    # 从关联任务的students_id中移除该学生ID
    if homework.range:
        task = InformationModel.query.filter_by(id=homework.range, type=2).first()
        if task and task.students_id is not None:
            # 处理task.students_id，确保正确解析
            current_student_ids = task.students_id or ""
            if current_student_ids.strip():
                # 解析逗号分隔的字符串
                student_id_list = [sid.strip() for sid in current_student_ids.split(",") if sid.strip()]
                student_id_str = str(homework.student_id)
                if student_id_str in student_id_list:
                    student_id_list.remove(student_id_str)
                    task.students_id = ",".join(student_id_list)
                    db.session.commit()
    
    # 删除作业信息
    db.session.delete(homework)
    db.session.commit()
    
    return jsonify({
        "code": 200,
        "message": "作业删除成功",
        "data": {
            "reminders_deleted": reminders_deleted
        }
    })

@bp.route("/information/homework/query", methods=["GET"])
@jwt_required()
@swag_from('../apidocs/information/homework/query.yaml')
def homework_query():
    """
    查询作业信息
    """
    # 获取查询参数
    task_id = request.args.get("task_id", type=int)
    group_id = request.args.get("group_id", type=int)
    student_id = request.args.get("student_id", type=int)
    
    # 获取当前用户
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    if not user:
        return jsonify({
            "code": 404,
            "message": "用户不存在"
        }), 404
    
        # 查询用户作为组长的组
    teacher_groups = GroupModel.query.filter_by(teacher_id=user.id).all()
    teacher_group_ids = [group.group_id for group in teacher_groups]
    
    # 查询用户作为组员的组
    student_groups = GroupModel.query.filter_by(student_id=user.id).all()
    student_group_ids = [group.group_id for group in student_groups]
    
    # 如果用户没有任何组关联
    if not teacher_group_ids and not student_groups:
        return jsonify({
            "code": 200,
            "message": "查询成功，但用户不属于任何组",
            "data": {
                "graded": [],
                "ungraded": [],
                "all": []
            }
        }), 200
    
    # 初始化最终查询结果（使用union）
    final_query = None
    
    # 处理用户作为组长的组
    if teacher_group_ids:
        # 获取这些组中的所有任务
        tasks_in_teacher_groups = InformationModel.query.filter(
            InformationModel.group_id.in_(teacher_group_ids),
            InformationModel.type == 2
        ).all()
        task_ids_as_teacher = [str(task.id) for task in tasks_in_teacher_groups]
        
        if task_ids_as_teacher:
            # 组长可查看所有作业
            teacher_query = InformationModel.query.filter(
                InformationModel.type == 5,
                InformationModel.range.in_(task_ids_as_teacher)
            )
            
            if final_query is None:
                final_query = teacher_query
            else:
                final_query = final_query.union(teacher_query)
    
    # 处理用户作为组员的组
    if student_group_ids:
        # 获取这些组中的所有任务
        tasks_in_student_groups = InformationModel.query.filter(
            InformationModel.group_id.in_(student_group_ids),
            InformationModel.type == 2
        ).all()
        task_ids_as_student = [str(task.id) for task in tasks_in_student_groups]
        
        if task_ids_as_student:
            # 组员只能查看自己的作业
            student_query = InformationModel.query.filter(
                InformationModel.type == 5,
                InformationModel.range.in_(task_ids_as_student),
                InformationModel.student_id == user.id
            )
            
            if final_query is None:
                final_query = student_query
            else:
                final_query = final_query.union(student_query)
    
    # 如果有final_query，则使用它，否则使用空查询
    if final_query is not None:
        query = final_query
    
    if task_id:
        # 检查任务是否存在
        task = InformationModel.query.filter_by(id=task_id, type=2).first()
        if not task:
            return jsonify({
                "code": 404,
                "message": "任务信息不存在"
            }), 404
            
        # 添加任务ID筛选
        query = query.filter_by(range=task_id)
        
        # 检查用户权限
        # 如果是组长，可以查看该组的所有作业
        is_group_leader = GroupModel.query.filter_by(
            group_id=task.group_id, 
            teacher_id=user.id
        ).first() is not None
        
        if not is_group_leader and student_id != user.id:
            # 非组长只能查看自己的作业
            query = query.filter_by(student_id=user.id)
    if group_id:
        # 检查组是否存在

        group = GroupModel.query.filter_by(group_id=group_id).first()
        if not group:
            return jsonify({
                "code": 404,
                "message": "小组不存在"
            }), 404
            
        # 先查询该小组的所有任务
        tasks = InformationModel.query.filter_by(group_id=group_id, type=2).all()
        task_ids = [str(task.id) for task in tasks]
        
        if not task_ids:
            # 如果小组没有任何任务，返回空结果
            return jsonify({
                "code": 200,
                "message": "查询成功，但该小组没有任何任务相关的作业",
                "data": {
                    "graded": [],
                    "ungraded": [],
                    "all": []
                }
            }), 200
        
        # 使用任务ID筛选作业
        query = query.filter(InformationModel.range.in_(task_ids))
        
        # 检查用户权限
        is_group_leader = GroupModel.query.filter_by(
            group_id=group_id, 
            teacher_id=user.id
        ).first() is not None
        
        if not is_group_leader and student_id != user.id:
            # 非组长只能查看自己的作业
            query = query.filter_by(student_id=user.id)

    # 如果指定了学生ID，并且用户有权限查看该学生的作业
    if student_id:
        # 管理员或组长可以查看指定学生的作业
        if user.user_mode == 'admin' or GroupModel.query.filter_by(teacher_id=user.id).first():
            query = query.filter_by(student_id=student_id)
        # 普通用户只能查看自己的作业
        elif student_id != user.id:
            return jsonify({
                "code": 403,
                "message": "无权查看其他学生的作业"
            }), 403
    
    # 执行查询
    homeworks = query.order_by(InformationModel.create_time.desc()).all()
    
    # 构建返回数据
    result = []
    for homework in homeworks:
        # 获取提交学生信息
        student = UserModel.query.filter_by(id=homework.student_id).first()
        student_name = student.username if student else "未知用户"
        
        # 获取关联任务信息
        task = InformationModel.query.filter_by(id=homework.range, type=2).first()
        task_title = task.title if task else "未知任务"
        
        # 获取文件信息
        files_info = []
        if homework.resource:
            file_names = homework.resource.split(',')
            homework_dir = os.path.join('BME_platform_flask/data/homework', str(homework.id))
            
            for file_name in file_names:
                file_path = os.path.join(homework_dir, file_name)
                if os.path.exists(file_path):
                    file_size = os.path.getsize(file_path)
                    files_info.append({
                        "name": file_name,
                        "size": file_size,
                        "size_readable": f"{file_size / 1024:.2f} KB" if file_size < 1024 * 1024 else f"{file_size / 1024 / 1024:.2f} MB"
                    })
        
        homework_data = {
            "id": homework.id,
            "group_id": homework.group_id,
            "title": homework.title,
            "content": homework.content,
            "student_id": homework.student_id,
            "student_name": student_name,
            "task_id": int(homework.range) if homework.range else None,
            "task_title": task_title,
            "status": homework.status,  # 0未批改，1已批改
            "comment": homework.comment,  # 评语
            "score": homework.score,  # 分数
            "create_time": homework.create_time.strftime("%Y-%m-%d %H:%M:%S"),
            "files": files_info,
            "files_count": len(files_info),
            "total_size": sum(f["size"] for f in files_info),
            "total_size_readable": f"{sum(f['size'] for f in files_info) / 1024:.2f} KB" if sum(f["size"] for f in files_info) < 1024 * 1024 else f"{sum(f['size'] for f in files_info) / 1024 / 1024:.2f} MB"
        }
        
        result.append(homework_data)
    
    # 按批改状态分组返回
    graded_homeworks = [hw for hw in result if hw["status"] == 1]
    ungraded_homeworks = [hw for hw in result if hw["status"] == 0]
    
    return jsonify({
        "code": 200,
        "message": "查询成功",
        "data": {
            "graded": graded_homeworks,
            "ungraded": ungraded_homeworks,
            "all": result  # 保留原有的所有作业列表
        }
    })

@bp.route("/information/homework/download", methods=["GET"])
@jwt_required()
@swag_from('../apidocs/information/homework/download.yaml')
def homework_download():
    """
    下载作业文件（打包为zip）
    """
    # 确保导入所需模块
    import os
    import tempfile
    import zipfile
    import shutil
    from flask import send_file
    
    # 获取作业ID
    homework_id = request.args.get("id", type=int)
    
    if not homework_id:
        return jsonify({
            "code": 400,
            "message": "作业ID不能为空"
        }), 400
    
    # 获取当前用户
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    if not user:
        return jsonify({
            "code": 404,
            "message": "用户不存在"
        }), 404
    
    # 查找作业信息
    homework = InformationModel.query.filter_by(id=homework_id, type=5).first()
    if not homework:
        return jsonify({
            "code": 404,
            "message": "作业信息不存在"
        }), 404
    
    # 定义Redis缓存键
    cache_key = f"homework_zip:{homework_id}"
    
    # 尝试从Redis缓存中获取ZIP文件
    cached_zip = redis_client.get(cache_key)
    
    # 如果缓存命中，直接返回缓存的ZIP文件
    if cached_zip:
        print(f"Redis缓存命中：作业ID {homework_id}")
        
        # 创建临时文件保存缓存的ZIP数据
        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.zip')
        temp_file.write(cached_zip)
        temp_file.close()
        
        # 发送缓存的ZIP文件
        return send_file(
            temp_file.name,
            mimetype='application/zip',
            as_attachment=True,
            download_name=f"homework_{homework_id}.zip"
        )
    
    # 检查权限
    is_owner = (homework.student_id == user.id)
    is_group_leader = GroupModel.query.filter_by(
        group_id=homework.group_id, 
        teacher_id=user.id
    ).first() is not None
    is_admin = (user.user_mode == 'admin')
    
    if not (is_owner or is_group_leader or is_admin):
        return jsonify({
            "code": 403,
            "message": "无权下载此作业"
        }), 403
    
    # 检查作业是否有文件
    if not homework.resource:
        return jsonify({
            "code": 404,
            "message": "该作业没有上传文件"
        }), 404
    
    # 准备文件目录
    homework_dir = os.path.join('BME_platform_flask/data/homework', str(homework.id))
    if not os.path.exists(homework_dir):
        return jsonify({
            "code": 404,
            "message": "作业文件不存在"
        }), 404
    
    # 创建临时目录用于存放zip文件
    temp_dir = tempfile.mkdtemp()
    zip_path = os.path.join(temp_dir, f"homework_{homework_id}.zip")
    
    try:
        # 创建zip文件
        with zipfile.ZipFile(zip_path, 'w') as zipf:
            for file_name in homework.resource.split(','):
                file_path = os.path.join(homework_dir, file_name)
                if os.path.exists(file_path):
                    zipf.write(file_path, file_name)
        
        # 缓存ZIP文件到Redis（1天过期）
        with open(zip_path, 'rb') as zip_file:
            zip_data = zip_file.read()
            redis_client.setex(cache_key, 60*60*24, zip_data)
            print(f"已缓存作业ID {homework_id} 的ZIP文件到Redis")
        
        # 发送zip文件
        return send_file(
            zip_path,
            mimetype='application/zip',
            as_attachment=True,
            download_name=f"homework_{homework_id}.zip"
        )
    finally:
        # 延迟删除临时目录（通过后台任务）
        def cleanup_temp_dir(directory):
            import time
            time.sleep(60)  # 等待60秒后删除
            shutil.rmtree(directory, ignore_errors=True)
        
        import threading
        cleanup_thread = threading.Thread(target=cleanup_temp_dir, args=(temp_dir,))
        cleanup_thread.daemon = True
        cleanup_thread.start()

@bp.route("/information/homework/grade", methods=["POST"])
@jwt_required()
@swag_from('../apidocs/information/homework/grade.yaml')
def homework_grade():
    """
    批改作业
    """
    # 获取请求数据
    data = request.get_json()
    homework_id = data.get("id")
    grade_status = data.get("status")
    comment = data.get("comment")  # 获取评语
    score = data.get("score")      # 获取分数
    
    if not homework_id:
        return jsonify({
            "code": 400,
            "message": "作业ID不能为空"
        }), 400
        
    if grade_status is None:
        return jsonify({
            "code": 400,
            "message": "批改状态不能为空"
        }), 400
    
    # 获取当前用户
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    if not user:
        return jsonify({
            "code": 404,
            "message": "用户不存在"
        }), 404
    
    # 查找作业信息
    homework = InformationModel.query.filter_by(id=homework_id, type=5).first()
    if not homework:
        return jsonify({
            "code": 404,
            "message": "作业信息不存在"
        }), 404
    
    # 验证批改权限：只有组长可以批改作业
    is_group_leader = False
    
    # 检查是否为组长
    group = GroupModel.query.filter_by(group_id=homework.group_id).first()
    if not group:
        return jsonify({
            "code": 404,
            "message": "小组不存在"
        }), 404
    
    is_group_leader = (group.teacher_id == user.id)
    
    # 检查是否有权限批改
    if not is_group_leader:
        return jsonify({
            "code": 403,
            "message": "无权批改作业，仅组长可批改"
        }), 403
    
    # 记录之前的状态
    previous_status = homework.status
    
    # 更新作业批改状态
    homework.status = 1 if grade_status else 0
    
    # 更新评语和分数（如果提供了）
    if comment is not None:
        homework.comment = comment
    if score is not None:
        homework.score = score
    
    db.session.commit()
    
    # 如果是首次批改或状态从未批改变为已批改，则创建提醒给学生
    if previous_status == 0 and homework.status == 1:
        # 创建提醒给提交作业的学生
        reminder_title = "作业已批改"
        
        # 构建提醒内容
        reminder_content = f"您提交的作业\"{homework.title}\"已被批改"
        if score:
            reminder_content += f"，分数: {score}"
        
        create_reminder(
            title=reminder_title,
            content=reminder_content,
            related_info_id=homework_id,
            student_id=homework.student_id,
            group_id=homework.group_id,
            source_type=5  # 5代表作业信息
        )
    
    return jsonify({
        "code": 200,
        "message": "作业批改成功",
        "data": {
            "id": homework.id,
            "status": homework.status,
            "comment": homework.comment,
            "score": homework.score
        }
    })

# 提醒信息查询接口
@bp.route("/information/reminder/query", methods=["GET"])
@jwt_required()
@swag_from('../apidocs/information/reminder/query.yaml')
def reminder_query():
    """
    查询当前用户的提醒信息
    """
    # 获取当前用户
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    if not user:
        return jsonify({
            "code": 404,
            "message": "用户不存在"
        }), 404
    
    # 查询该用户的所有提醒信息
    reminders = InformationModel.query.filter_by(
        type=0,  # 0代表提醒信息
        student_id=user.id
    ).order_by(InformationModel.create_time.desc()).all()
    
    # 初始化分类结果
    total_unread = len(reminders)  # 所有提醒都是未读的，读完后会被删除
    categorized_reminders = {
        "leave": [],      # 请假相关提醒
        "task": [],       # 任务相关提醒
        "notice": [],     # 通知相关提醒
        "error": [],      # 报错相关提醒
        "homework": [],   # 作业相关提醒
        "other": []       # 其他提醒
    }
    
    # 处理每个提醒，直接根据priority字段进行分类
    for reminder in reminders:
        # 构建基本提醒数据
        reminder_data = {
            "id": reminder.id,
            "title": reminder.title,
            "content": reminder.content,
            "related_info_id": reminder.range,  # 关联的原始信息ID
            "source_type": reminder.priority,   # 原始信息类型
            "create_time": reminder.create_time.strftime("%Y-%m-%d %H:%M:%S")
        }
        
        # 根据原始信息类型直接分类
        source_type = reminder.priority
        if source_type == "1":
            categorized_reminders["leave"].append(reminder_data)
        elif source_type == "2":
            categorized_reminders["task"].append(reminder_data)
        elif source_type == "3":
            categorized_reminders["notice"].append(reminder_data)
        elif source_type == "4":
            categorized_reminders["error"].append(reminder_data)
        elif source_type == "5":
            categorized_reminders["homework"].append(reminder_data)
        else:
            categorized_reminders["other"].append(reminder_data)
    
    return jsonify({
        "code": 200,
        "message": "查询成功",
        "data": {
            "total_unread": total_unread,  # 未读提醒总数
            "reminders": categorized_reminders  # 按类型分类的提醒
        }
    }), 200

# 单个提醒信息删除接口
@bp.route("/information/reminder/delete", methods=["POST"])
@jwt_required()
@swag_from('../apidocs/information/reminder/delete.yaml')
def reminder_delete():
    """
    删除单个提醒信息
    """
    # 获取请求数据
    data = request.get_json()
    reminder_id = data.get("id")
    
    if not reminder_id:
        return jsonify({
            "code": 400,
            "message": "提醒ID不能为空"
        }), 400
    
    # 获取当前用户
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    if not user:
        return jsonify({
            "code": 404,
            "message": "用户不存在"
        }), 404
    
    # 查找提醒信息
    reminder = InformationModel.query.filter_by(id=reminder_id, type=0).first()
    
    if not reminder:
        return jsonify({
            "code": 404,
            "message": "提醒信息不存在"
        }), 404
    
    # 验证删除权限：只有提醒的接收者可以删除
    if reminder.student_id != user.id:
        return jsonify({
            "code": 403,
            "message": "无权删除此提醒，仅接收者可删除"
        }), 403
    
    # 删除提醒信息
    db.session.delete(reminder)
    db.session.commit()
    
    return jsonify({
        "code": 200,
        "message": "提醒删除成功"
    }), 200

# 批量删除提醒信息接口
@bp.route("/information/reminder/batch_delete", methods=["POST"])
@jwt_required()
@swag_from('../apidocs/information/reminder/batch_delete.yaml')
def reminder_batch_delete():
    """
    批量删除提醒信息，支持多种条件
    """
    # 获取请求数据
    data = request.get_json()
    
    # 支持的筛选条件
    user_id = data.get("user_id")       # 指定接收者ID
    info_id = data.get("info_id")       # 关联的原始信息ID
    ids = data.get("ids")               # 指定要删除的提醒ID列表
    source_type = data.get("type")      # 提醒源的类型(1:请假 2:任务 3:通知 4:报错 5:作业)
    
    # 获取当前用户
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    if not user:
        return jsonify({
            "code": 404,
            "message": "用户不存在"
        }), 404
    
    # 构建查询条件
    query = InformationModel.query.filter_by(type=0)  # 筛选提醒信息
    
    # 只有管理员可以按接收者ID删除其他用户的提醒
    if user_id and user_id != user.id:
        if user.user_mode != 'admin':
            return jsonify({
                "code": 403,
                "message": "权限不足，只有管理员可以删除其他用户的提醒"
            }), 403
        query = query.filter_by(student_id=user_id)
    else:
        # 非管理员或未指定用户ID时，只能删除自己的提醒
        query = query.filter_by(student_id=user.id)
    
    # 如果提供了原始信息ID，筛选关联该信息的提醒
    if info_id:
        query = query.filter_by(range=str(info_id))
    
    # 如果提供了提醒ID列表，筛选这些ID的提醒
    if ids and isinstance(ids, list):
        query = query.filter(InformationModel.id.in_(ids))
    
    # 如果提供了提醒源类型，按类型筛选
    if source_type:
        query = query.filter_by(priority=source_type)
    
    # 查找所有符合条件的提醒
    reminders = query.all()
    
    if not reminders:
        return jsonify({
            "code": 200,
            "message": "未找到符合条件的提醒",
            "data": {
                "deleted_count": 0
            }
        }), 200
    
    # 记录删除数量
    deleted_count = len(reminders)
    
    # 批量删除
    for reminder in reminders:
        db.session.delete(reminder)
    
    db.session.commit()
    
    return jsonify({
        "code": 200,
        "message": "批量删除提醒成功",
        "data": {
            "deleted_count": deleted_count
        }
    }), 200



import os
import datetime

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
    
    # 删除请假信息
    db.session.delete(leave)
    db.session.commit()
    
    return jsonify({
        "code": 200,
        "message": "请假信息删除成功"
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
        student_query = query.filter_by(student_id=user.id)
        student_leaves = student_query.all()
        

        # 用户没有请假记录，尝试作为教师查询
        teacher_groups = GroupModel.query.filter_by(teacher_id=user.id).all()
        if teacher_groups:
            # 如果是教师，查询所有负责小组的请假信息
            group_ids = [group.group_id for group in teacher_groups]
            query = query.filter(InformationModel.group_id.in_(group_ids))
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
        else:  # 未批准
            pending_leaves.append(leave_data)
    
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
    leave.status = 1
    
    # 如果请假开始/结束时间只有日期部分，处理为23:59:59
    if leave.start_time:
        leave.start_time = process_datetime(leave.start_time)
    if leave.end_time:
        leave.end_time = process_datetime(leave.end_time)
    
    db.session.commit()
    
    return jsonify({
        "code": 200,
        "message": "请假已批准"
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
                priority=priority
                # 不再记录student_id
            )
            
            db.session.add(information)
            db.session.commit()
            
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
    
    # 删除任务信息
    db.session.delete(task)
    db.session.commit()
    
    return jsonify({
        "code": 200,
        "message": "任务删除成功"
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
        if user.user_mode != 'admin':
            return jsonify({
                "code": 403,
                "message": "权限不足，只有管理员可以通过小组ID查询"
            }),403
        # 管理员可以查询特定小组
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
                    "high_priority": [],
                    "medium_priority": [],
                    "low_priority": []
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
    high_priority = []   # 优先级1-2
    medium_priority = [] # 优先级3
    low_priority = []    # 优先级4-5
    
    for task in tasks:
        # 获取所属小组信息
        group = GroupModel.query.filter_by(group_id=task.group_id).first()
        group_name = group.name if group else "未知小组"
        
        task_data = {
            "id": task.id,
            "group_id": task.group_id,
            "group_name": group_name,
            "title": task.title,
            "content": task.content,
            "end_time": task.end_time.strftime("%Y-%m-%d %H:%M:%S") if task.end_time else None,
            "priority": task.priority,
            "create_time": task.create_time.strftime("%Y-%m-%d %H:%M:%S")
        }
        
        # 确保优先级是整数类型
        try:
            priority_value = int(task.priority) if task.priority is not None else 3
        except (ValueError, TypeError):
            priority_value = 3  # 默认为中等优先级
        
        # 根据优先级分组
        if priority_value <= 2:
            high_priority.append(task_data)
        elif priority_value == 3:
            medium_priority.append(task_data)
        else:
            low_priority.append(task_data)
    
    return jsonify({
        "code": 200,
        "message": "查询成功",
        "data": {
            "high_priority": high_priority,
            "medium_priority": medium_priority,
            "low_priority": low_priority
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
                range=range_str
                # 不再记录student_id
            )
            
            db.session.add(information)
            db.session.commit()
            
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
    
    # 删除通知信息
    db.session.delete(notice)
    db.session.commit()
    
    return jsonify({
        "code": 200,
        "message": "通知删除成功"
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
        if user.user_mode != 'admin':
            return jsonify({
                "code": 403,
                "message": "权限不足，只有管理员可以通过小组ID查询"
            }),403
        # 管理员可以查询特定小组
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
from datetime import datetime, timedelta
from collections import defaultdict
from functools import wraps
from flask import jsonify, request
from flask_jwt_extended import get_jwt_identity

from exts import db
from models import UserModel, PermissionModel, UserPermissionModel

# 安全审计装饰器
def audit_log(operation=None, is_login=False):
    """
    安全审计装饰器
    记录用户操作日志
    operation: 操作描述（可选）
    is_login: 是否为登录操作（特殊处理）
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            # 对于登录操作，延迟获取用户信息
            user = None
            user_email = None
            # 如果不是登录操作，则提前获取用户身份
            if not is_login:
                try:
                    user_email = get_jwt_identity()
                    user = UserModel.query.filter_by(email=user_email).first()
                except:
                    # JWT验证失败的情况
                    pass

            # 记录操作开始前的状态
            start_time = datetime.utcnow()
            operation_data = None
            result = '失败'

            try:
                # 执行原函数
                response = func(*args, **kwargs)
                # 如果是登录操作，在执行后获取用户信息
                if is_login:
                    user_email = request.json.get('User_Email') if request.json else None
                    status_code = 200
                    user = UserModel.query.filter_by(email=user_email).first()
                # 处理普通响应
                if isinstance(response, tuple) and len(response) == 2:
                    response_obj = response[0]
                    status_code = response[1]
                else:
                    response_obj = response
                    status_code = 200

                # 解析响应数据
                if hasattr(response_obj, 'get_json'):
                    operation_data = response_obj.get_json()
                elif isinstance(response_obj, dict):
                    operation_data = response_obj
                elif isinstance(response_obj, str):
                    operation_data = {'message': response_obj}

                # 设置操作结果
                result = '成功' if status_code < 400 else '失败'

                # 记录审计日志
                if user or user_email:
                    from models import AuditLog
                    # 尝试从代理头获取真实IP地址
                    real_ip = request.headers.get('X-Real-IP') or request.headers.get('X-Forwarded-For', '').split(',')[
                        0].strip()
                    client_ip = real_ip or request.remote_addr

                    username = user.username if user else user_email
                    user_id = user.id if user else None

                    log_entry = AuditLog(
                        user_id=user_id,
                        username=username,
                        ip_address=client_ip,
                        user_agent=request.headers.get('User-Agent', ''),
                        operation=operation or func.__name__,
                        operation_url=request.url,
                        operation_data=str(operation_data),
                        result=result,
                        timestamp=start_time
                    )
                    db.session.add(log_entry)
                    db.session.commit()

                return response
            except Exception as e:
                # 记录异常情况
                username = user.username if user else '未知用户'
                user_id = user.id if user else None

                if user or is_login:
                    from models import AuditLog
                    # 尝试从代理头获取真实IP地址
                    real_ip = request.headers.get('X-Real-IP') or request.headers.get('X-Forwarded-For', '').split(',')[
                        0].strip()
                    client_ip = real_ip or request.remote_addr

                    log_entry = AuditLog(
                        user_id=user_id,
                        username=username,
                        ip_address=client_ip,
                        user_agent=request.headers.get('User-Agent', ''),
                        operation=operation or func.__name__,
                        operation_url=request.url,
                        operation_data=str({'error': str(e)}),
                        result='失败',
                        timestamp=start_time
                    )
                    db.session.add(log_entry)
                    db.session.commit()
                raise
        return wrapper
    return decorator


# 辅助函数：格式化时长
def format_duration(duration):
    hours = int(duration)
    minutes = int(round((duration - hours) * 60))
    if minutes >= 60:
        hours += 1
        minutes = 0
    return f"{hours}小时{minutes}分钟"

def generate_date_range(start_date, end_date):
    """生成日期范围内的所有日期列表"""
    return [start_date + timedelta(days=x) for x in range((end_date - start_date).days + 1)]

def build_result(dates, date_info, today, now, is_current_month, format_duration):
    """
    构建返回结果
    :param dates: 日期列表
    :param date_info: 日期信息字典
    :param today: 今天日期
    :param now: 当前时间
    :param is_current_month: 是否是当前月
    :param format_duration: 格式化时长的函数
    :return: 结果列表
    """
    result = []
    for day in dates:
        current_date = day.date()
        info = date_info.get(current_date, {"total": 0.0, "has_open": False})
        total = info["total"]
        status = "已完成" if total > 0 else "未签到"

        if is_current_month and current_date == today:
            if info["has_open"]:
                latest_checkin = info["latest_checkin"]
                if latest_checkin:
                    current_duration = (now - latest_checkin).total_seconds() / 3600
                    total += current_duration
                    status = "进行中"
                else:
                    status = "进行中"
            else:
                status = "未签到" if total == 0 else "已完成"

        formatted_duration = format_duration(total)

        result.append({
            "date": current_date.isoformat(),
            "total_duration": formatted_duration,
            "total_hours": round(total, 2),
            "status": status
        })
    return result


def check_permission(permission_name):
    """
    权限检查装饰器
    用法: @check_permission('course_management')
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            # 获取当前用户
            user_email = get_jwt_identity()
            user = UserModel.query.filter_by(email=user_email).first()
            
            if not user:
                return jsonify({
                    "code": 401,
                    "message": "用户未认证"
                }), 401
            
            # 检查是否是管理员（保持向后兼容）
            if user.user_mode == 'admin':
                return func(*args, **kwargs)
            
            # 检查用户是否有特定权限
            permission = PermissionModel.query.filter_by(name=permission_name).first()
            if not permission:
                return jsonify({
                    "code": 403,
                    "message": f"权限 '{permission_name}' 不存在"
                }), 403
            
            user_permission = UserPermissionModel.query.filter_by(
                user_id=user.id, 
                permission_id=permission.id
            ).first()
            
            if not user_permission:
                return jsonify({
                    "code": 403,
                    "message": "用户权限不足"
                }), 403
                
            return func(*args, **kwargs)
        return wrapper
    return decorator


def check_multiple_permissions(permission_names, require_all=False):
    """
    检查多个权限的装饰器
    permission_names: 权限名称列表
    require_all: True表示需要所有权限，False表示只要有其中一个权限即可
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            # 获取当前用户
            user_email = get_jwt_identity()
            user = UserModel.query.filter_by(email=user_email).first()
            
            if not user:
                return jsonify({
                    "code": 401,
                    "message": "用户未认证"
                }), 401
            
            # 检查是否是管理员（保持向后兼容）
            if user.user_mode == 'admin':
                return func(*args, **kwargs)
            
            permissions = PermissionModel.query.filter(
                PermissionModel.name.in_(permission_names)
            ).all()
            
            if len(permissions) != len(permission_names):
                missing_permissions = set(permission_names) - set([p.name for p in permissions])
                return jsonify({
                    "code": 403,
                    "message": f"权限 {missing_permissions} 不存在"
                }), 403
            
            permission_ids = [p.id for p in permissions]
            
            user_permissions = UserPermissionModel.query.filter(
                UserPermissionModel.user_id == user.id,
                UserPermissionModel.permission_id.in_(permission_ids)
            ).all()
            
            user_permission_ids = [up.permission_id for up in user_permissions]
            
            if require_all:
                # 需要所有权限
                if len(user_permission_ids) != len(permission_ids):
                    missing_permissions = [p.name for p in permissions if p.id not in user_permission_ids]
                    return jsonify({
                        "code": 403,
                        "message": f"用户缺少权限: {missing_permissions}"
                    }), 403
            else:
                # 只需要其中一个权限
                if not user_permission_ids:
                    return jsonify({
                        "code": 403,
                        "message": f"用户需要以下权限之一: {permission_names}"
                    }), 403
                    
            return func(*args, **kwargs)
        return wrapper
    return decorator


# 确保所有蓝图模块都被导入
from .auth import bp as auth_bp
from .user import bp as user_bp
from .course import bp as course_bp
from .course_group import bp as course_group_bp
from .article import bp as article_bp
from .medal import bp as medal_bp
from .codecheck import bp as codecheck_bp
from .homeCover import bp as homeCover_bp
from .learningProgress import bp as learningprogress_bp
from .information import bp as information_bp
from .permissionACL import bp as permission_bp
from .task import bp as task_bp
from .discussion import bp as discussion_bp
from .llm import bp as llm_bp
from .notification import bp as notification_bp
from .seat import bp as seat_bp
from .attendance_report import bp as attendance_report_bp

__all__ = [
    'auth_bp',
    'user_bp',
    'course_bp',
    'course_group_bp',
    'article_bp',
    'medal_bp',
    'codecheck_bp',
    'homeCover_bp',
    'learningprogress_bp',
    'information_bp',
    'permission_bp',
    'task_bp',
    'discussion_bp',
    'llm_bp',
    'notification_bp',
    'seat_bp',
    'attendance_report_bp'
]
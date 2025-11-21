from datetime import datetime, timedelta
from collections import defaultdict
from functools import wraps
from flask import jsonify
from flask_jwt_extended import get_jwt_identity

from models import UserModel, PermissionModel, UserPermissionModel


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


def has_permission(user_id, permission_name):
    """
    检查用户是否具有特定权限（供内部调用）
    """
    user = UserModel.query.get(user_id)
    if not user:
        return False
    
    # 管理员拥有所有权限
    if user.user_mode == 'admin':
        return True
        
    permission = PermissionModel.query.filter_by(name=permission_name).first()
    if not permission:
        return False
        
    user_permission = UserPermissionModel.query.filter_by(
        user_id=user.id,
        permission_id=permission.id
    ).first()
    
    return user_permission is not None


def init_permissions():
    """
    初始化默认权限
    """
    from models import db, PermissionModel
    
    default_permissions = [
        ('course_management', '课程管理权限'),
        ('user_management', '用户管理权限'),
        ('article_management', '文章管理权限'),
        ('medal_management', '勋章管理权限'),
        ('system_management', '系统管理权限')
    ]
    
    for name, description in default_permissions:
        perm = PermissionModel.query.filter_by(name=name).first()
        if not perm:
            perm = PermissionModel(name=name, description=description)
            db.session.add(perm)
    
    try:
        db.session.commit()
    except:
        db.session.rollback()



# 确保所有蓝图模块都被导入
from .auth import bp as auth_bp
from .user import bp as user_bp
from .course import bp as course_bp
from .article import bp as article_bp
from .medal import bp as medal_bp
from .codecheck import bp as codecheck_bp
from .homeCover import bp as homeCover_bp
from .learningProgress import bp as learningprogress_bp
from .information import bp as information_bp
from .permissionACL import bp as permission_bp

__all__ = [
    'auth_bp',
    'user_bp',
    'course_bp',
    'article_bp',
    'medal_bp',
    'codecheck_bp',
    'homeCover_bp',
    'learningprogress_bp',
    'information_bp',
    'permission_bp'
]
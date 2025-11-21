from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from models import db, UserModel, PermissionModel, UserPermissionModel

from . import check_permission

# 导入api文档模块
from flasgger import swag_from

bp = Blueprint("permissionACL", __name__, url_prefix="/permissions")

@bp.route("/list", methods=["GET"])
@jwt_required()
@swag_from('../apidocs/permissions/list.yaml')
def list_permissions():
    """
    列出所有权限
    """
    permissions = PermissionModel.query.all()
    result = []
    for perm in permissions:
        result.append({
            "id": perm.id,
            "name": perm.name,
            "description": perm.description
        })
    
    return jsonify({
        "code": 200,
        "permissions": result
    })


@bp.route("/assign", methods=["POST"])
@jwt_required()
@check_permission('system_management')
@swag_from('../apidocs/permissions/assign.yaml')
def assign_permission():
    """
    为用户分配权限
    请求参数:
    {
        "user_id": 1,
        "permission_id": 1
    }
    或
    {
        "user_id": 1,
        "permission_name": "course_management"
    }
    """
    data = request.get_json()
    user_id = data.get('user_id')
    permission_id = data.get('permission_id')
    permission_name = data.get('permission_name')
    
    if not user_id:
        return jsonify({
            "code": 400,
            "message": "缺少 user_id 参数"
        }), 400
    
    # 检查用户是否存在
    user = UserModel.query.get(user_id)
    if not user:
        return jsonify({
            "code": 404,
            "message": "用户不存在"
        }), 404
    
    # 获取权限
    permission = None
    if permission_id:
        permission = PermissionModel.query.get(permission_id)
    elif permission_name:
        permission = PermissionModel.query.filter_by(name=permission_name).first()
    
    if not permission:
        return jsonify({
            "code": 404,
            "message": "权限不存在"
        }), 404
    
    # 检查是否已经分配了该权限
    existing = UserPermissionModel.query.filter_by(
        user_id=user_id,
        permission_id=permission.id
    ).first()
    
    if existing:
        return jsonify({
            "code": 400,
            "message": "用户已经拥有该权限"
        }), 400
    
    # 分配权限
    user_permission = UserPermissionModel(
        user_id=user_id,
        permission_id=permission.id
    )
    db.session.add(user_permission)
    db.session.commit()
    
    return jsonify({
        "code": 200,
        "message": f"成功为用户 {user.username} 分配权限 {permission.name}"
    })


@bp.route("/revoke", methods=["POST"])
@jwt_required()
@check_permission('system_management')
@swag_from('../apidocs/permissions/revoke.yaml')
def revoke_permission():
    """
    撤销用户的权限
    请求参数:
    {
        "user_id": 1,
        "permission_id": 1
    }
    或
    {
        "user_id": 1,
        "permission_name": "course_management"
    }
    """
    data = request.get_json()
    user_id = data.get('user_id')
    permission_id = data.get('permission_id')
    permission_name = data.get('permission_name')
    
    if not user_id:
        return jsonify({
            "code": 400,
            "message": "缺少 user_id 参数"
        }), 400
    
    # 获取权限
    permission = None
    if permission_id:
        permission = PermissionModel.query.get(permission_id)
    elif permission_name:
        permission = PermissionModel.query.filter_by(name=permission_name).first()
    
    if not permission:
        return jsonify({
            "code": 404,
            "message": "权限不存在"
        }), 404
    
    # 查找用户权限记录
    user_permission = UserPermissionModel.query.filter_by(
        user_id=user_id,
        permission_id=permission.id
    ).first()
    
    if not user_permission:
        return jsonify({
            "code": 400,
            "message": "用户没有该权限"
        }), 400
    
    # 撤销权限
    db.session.delete(user_permission)
    db.session.commit()
    
    return jsonify({
        "code": 200,
        "message": f"成功撤销用户权限 {permission.name}"
    })


@bp.route("/user/<int:user_id>", methods=["GET"])
@jwt_required()
@check_permission('user_management')
@swag_from('../apidocs/permissions/user_permissions.yaml')
def get_user_permissions(user_id):
    """
    获取用户的所有权限
    """
    user = UserModel.query.get(user_id)
    if not user:
        return jsonify({
            "code": 404,
            "message": "用户不存在"
        }), 404
    
    user_permissions = UserPermissionModel.query.filter_by(user_id=user_id).all()
    permissions = []
    for up in user_permissions:
        permission = PermissionModel.query.get(up.permission_id)
        permissions.append({
            "id": permission.id,
            "name": permission.name,
            "description": permission.description
        })
    
    return jsonify({
        "code": 200,
        "user": user.username,
        "permissions": permissions
    })
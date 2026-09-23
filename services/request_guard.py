"""全站账户状态与管理端路径的入口校验。"""
from flask import jsonify, request
from flask_jwt_extended import get_jwt_identity, verify_jwt_in_request

from models import UserModel


def enforce_request_access():
    """每次请求读取当前账号状态；管理端路径只接受平台管理员。"""
    if request.method == 'OPTIONS' or request.path.startswith('/auth/'):
        return None

    is_admin_api = request.path.startswith('/admin/')
    auth_header = request.headers.get('Authorization', '')
    if not auth_header.startswith('Bearer '):
        return (jsonify({"code": 401, "message": "未提供访问令牌"}), 401) if is_admin_api else None

    try:
        verify_jwt_in_request()
        identity = get_jwt_identity()
    except Exception:
        return (jsonify({"code": 401, "message": "访问令牌无效"}), 401) if is_admin_api else None

    user = UserModel.query.filter_by(email=identity).first()
    if user and (user.status or 'active') == 'banned':
        return jsonify({"code": 403, "message": "账号已被封禁，请联系管理员"}), 403
    if is_admin_api and (not user or not user.is_admin()):
        return jsonify({"code": 403, "message": "无管理端访问权限"}), 403
    return None

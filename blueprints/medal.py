from flask import Blueprint, request, redirect, jsonify

# 导入拓展
from exts import db

# 导入数据库表
from models import MedalModel, MedalUserModel, UserModel, GroupModel

# 导入表单验证
from .forms import MedalForm

# 导入token验证模块
from flask_jwt_extended import (create_access_token, get_jwt_identity, jwt_required, JWTManager)

# 导入api文档模块
from flasgger import swag_from

bp = Blueprint("medal", __name__, url_prefix="/medal")


# 创建勋章
@bp.route("/medal_create", methods=["POST"])
@jwt_required()
@swag_from('../apidocs/medal/medal_create.yaml')
def medal_create():
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    mode = user.user_mode
    if mode != 'admin':
        return jsonify({
            "code": 400,
            'message': "用户权限不够"
        }), 400

    form = MedalForm()
    if form.validate():
        medal_name = form.Medal_Name.data
        description = form.Medal_Name_CN.data
        tag = form.Medal_Tag.data
        medal = MedalModel(medal_name=medal_name, description=description, tags=tag)
        db.session.add(medal)
        db.session.commit()
        return jsonify({
            "code": 200,
            "message": "勋章创建成功"
        })
    else:
        return jsonify({
            "code": 401,
            "message": "勋章创建失败",
            "error": form.errors
        }), 401

@bp.route("/my_medal_count")
@jwt_required()
@swag_from('../apidocs/medal/my_medal_count.yaml')
def my_medal_count():
    """返回当前登录用户拥有的奖牌数量"""
    try:
        user_email = get_jwt_identity()
        user = UserModel.query.filter_by(email=user_email).first()
        if not user:
            return jsonify({
                "code": 401,
                "message": "用户不存在"
            }), 401
        count = MedalUserModel.query.filter_by(user_id=user.id).count()

        return jsonify({
            "code": 200,
            "message": "查询成功",
            "medal_count": count
        })
    
    except Exception as e:
        return jsonify({
            "code": 500,
            "message": str(e)
        }), 500
    
# 查询勋章列表
@bp.route("/medal_list")
@jwt_required()
@swag_from('../apidocs/medal/medal_list.yaml')
def medal_list():
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    mode = user.user_mode
    if mode != 'admin':
        return jsonify({
            "code": 400,
            'message': "用户权限不够"
        }), 400

    medals = MedalModel.query.all()
    data = []
    for medal in medals:
        data.append({
            "Medal_Name": medal.medal_name,
            "Medal_Description": medal.description,
            "Medal_Tag": medal.tags,
            "Medal_Id": medal.id
        })

    return jsonify({
        "code": 200,
        "message": "获取勋章列表成功",
        "medal": data
    })

# @bp.route("/medal_query_by_user_id")
# @jwt_required()
# @swag_from('../apidocs/medal/medal_query_by_user_id.yaml')
# def medal_query_by_user_id():
#     user_email = get_jwt_identity()
#     user = UserModel.query.filter_by(email=user_email).first()
#     if not user:
#         return jsonify({
#             "code": 404,
#             "message": "用户不存在"
#         }), 404
#     mode = user.user_mode
#     if mode != 'admin':
#         return jsonify({
#             "code": 400,
#             'message': "用户权限不够"
#         }), 400
    
#     medals = MedalUserModel.query.filter_by(user_id=user.id).all()

#     result = []

#     for medal in medals:
#         medal_info = MedalModel.query.filter_by(id=medal.medal_id).first()
#         result.append({
#             "Medal_Id": medal.medal_id,
#             "Medal_Name": medal_info.medal_name
#         })

#     return jsonify({
#         "code": 200,
#         "message": "获取用户勋章成功",
#         "medals": result
#     })


# 删除勋章
@bp.route("/medal_delete", methods=["POST"])
@jwt_required()
@swag_from('../apidocs/medal/medal_delete.yaml')
def medal_delete():
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    mode = user.user_mode
    if mode != 'admin':
        return jsonify({
            "code": 400,
            'message': "用户权限不够"
        }), 400

    medal_id = request.json.get("Medal_Id")
    medal = MedalModel.query.filter_by(id=medal_id).first()
    try:
        db.session.delete(medal)
        db.session.commit()
        return jsonify({
            "code": 200,
            "message": "勋章删除成功"
        })

    except Exception as e:
        return jsonify({
            "code": 402,
            'message': str(e)
        }), 402

# 修改勋章（需要改什么就传什么key）
@bp.route("/medal_edit", methods=["POST"])
@jwt_required()
@swag_from('../apidocs/medal/medal_edit.yaml')
def medal_edit():
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    mode = user.user_mode
    if mode != 'admin':
        return jsonify({
            "code": 400,
            'message': "用户权限不够"
        }), 400

    # 获取请求中的参数
    Medal_Id = request.json.get("Medal_Id")
    
    # 检查是否提供了Medal_Id
    if not Medal_Id:
        return jsonify({
            "code": 401,
            "message": "未提供勋章ID"
        }), 401
    
    # 获取要修改的属性
    Medal_Name = request.json.get("Medal_Name")
    Medal_Description = request.json.get("Medal_Description")
    Medal_Tag = request.json.get("Medal_Tag")
    
    # 查找勋章
    medal = MedalModel.query.filter_by(id=Medal_Id).first()
    if not medal:
        return jsonify({
            "code": 401,
            "message": "勋章不存在"
        }), 401
    
    try:
        # 直接修改勋章属性（如果提供了相应的参数）
        if Medal_Name is not None:
            medal.medal_name = Medal_Name
        if Medal_Tag is not None:
            medal.tags = Medal_Tag
        if Medal_Description is not None:
            medal.description = Medal_Description
        
        # 保存修改
        db.session.commit()
        
        return jsonify({
            "code": 200,
            "message": "勋章修改成功",
            "Medal_Name": medal.medal_name,
            "Medal_Tag": medal.tags,
            "Medal_Description": medal.description
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({
            "code": 500,
            "message": f"勋章修改失败: {str(e)}"
        }), 500

# 创建用户勋章
@bp.route("/user_medal_add", methods=["POST"])
@jwt_required()
@swag_from('../apidocs/medal/user_medal_add.yaml')
def user_medal_add():
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    mode = user.user_mode
    if mode!= 'admin':
        return jsonify({
            "code": 400,
           'message': "用户权限不够"
        }), 400

    student_id = request.json.get("Student_Id")
    medal_name = request.json.get("Medal_Name")
    description = request.json.get("Medal_Description")
    user = UserModel.query.filter_by(id=student_id).first()
    if not user:
        return jsonify({
            "code": 401,
            "message": "学生不存在"
        }), 401
    medal = MedalModel.query.filter_by(medal_name=medal_name).first()
    if not medal:
        return jsonify({
            "code": 402,
            "message": "勋章不存在"
        })
    medal_user = MedalUserModel.query.filter_by(user_id=user.id, medal_id=medal.id).first()
    if medal_user:
        return jsonify({
            "code": 403,
            "message": "用户已经拥有该勋章"
        }), 403
    medal_user = MedalUserModel(user_id=user.id, medal_id=medal.id, description=description)
    user.medal = user.medal + 1
    db.session.add(medal_user)
    db.session.commit()
    return jsonify({
        "code": 200,
        "message": "勋章授予成功"
    })

# 查询勋章列表
@bp.route("/user_medal_list")
@jwt_required()
@swag_from('../apidocs/medal/user_medal_list.yaml')
def user_medal_list():
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    mode = user.user_mode
    if mode!= 'admin':
        return jsonify({
            "code": 400,
           'message': "用户权限不够"
        }), 400

    student_id = request.args.get("Student_Id")
    student_name = UserModel.query.filter_by(id=student_id).first().username
    medals = MedalUserModel.query.filter_by(user_id=student_id).all()
    data = []
    for medal in medals:
        data.append({
            "Medal_Id": medal.id,
            "Medal_Name": medal.medal.medal_name,
            "Medal_Tag": medal.medal.tags,
            "Medal_Name_CN": medal.medal.description
        })

    return jsonify({
        "code": 200,
        "Student": student_name,
        "Medal": data,
        "message": "查询用户勋章列表成功"
    })


@bp.route("/user_medal_show")
@jwt_required()
@swag_from('../apidocs/medal/user_medal_show.yaml')
def user_medal_show():
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    student_id = user.id
    medals = MedalUserModel.query.filter_by(user_id=student_id).all()
    medal_list = MedalModel.query.all()
    data = []
    for medal_ in medal_list:
        found = False  # 添加标记位
        for medal in medals:
            if medal.medal_id == medal_.id:
                data.append({
                    "Medal_Id": medal.medal_id,
                    "Medal_Name": medal_.medal_name,
                    "Medal_Tag": medal_.tags,
                    "Medal_Name_CN": medal_.description,
                    "Get_Time": medal.get_time.strftime('%Y-%m-%d'),
                    "Description": medal.description
                })
                found = True
                break  # 找到后立即跳出内层循环

        # 只有未找到匹配时才添加 Null 条目
        if not found:
            data.append({
                "Medal_Id": medal_.id,
                "Medal_Name": medal_.medal_name,
                "Medal_Tag": medal_.tags,
                "Medal_Name_CN": medal_.description,
                "Get_Time": None,
                "Description": None
            })

    return jsonify({
        "code": 200,
        "Medal": data,
        "User_Id": user.id,
        "message": "查询用户勋章列表成功"
    })



@bp.route("/user_medal_list_by_medal_id")
@jwt_required()
@swag_from('../apidocs/medal/user_medal_list_by_medal_id.yaml')
def user_medal_list_by_medal_id():
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    mode = user.user_mode
    if mode!= 'admin':
        return jsonify({
            "code": 400,
           'message': "用户权限不够"
        }), 400    
    
    medal_id = request.args.get("Medal_Id")
    if not medal_id:
        return jsonify({
            "code": 401,
            "message": "未提供奖牌ID"
        }), 401
    
    # 检查奖牌是否存在
    medal = MedalModel.query.filter_by(id=medal_id).first()
    if not medal:
        return jsonify({
            "code": 402,
            "message": "奖牌不存在"
        }), 402
    
    # 查询所有获得该奖牌的用户记录
    medal_users = MedalUserModel.query.filter_by(medal_id=medal_id).all()
    
    users_data = []
    for medal_user in medal_users:
        user_info = UserModel.query.filter_by(id=medal_user.user_id).first()
        if user_info:
            users_data.append({
                "User_Id": user_info.id,
                "User_Name": user_info.username,
                "Get_Time": medal_user.get_time.strftime('%Y-%m-%d'),
                "Description": medal_user.description
            })
    
    return jsonify({
        "code": 200,
        "message": "查询获得该奖牌的用户列表成功",
        "Medal_Name": medal.medal_name,
        "Medal_Description": medal.description,
        "Users": users_data
    })
    

@bp.route("/user_medal_delete", methods=["POST"])
@jwt_required()
@swag_from('../apidocs/medal/user_medal_delete.yaml')
def user_medal_delete():
    # 验证用户权限
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    mode = user.user_mode
    if mode != 'admin':
        return jsonify({
            "code": 400,
            'message': "用户权限不够"
        }), 400
    
    # 获取请求参数
    user_id = request.json.get("User_Id")
    group_id = request.json.get("Group_Id")
    medal_id = request.json.get("Medal_Id")
    
    # 参数验证
    if user_id is None and group_id is None and medal_id is None:
        return jsonify({
            "code": 401,
            "message": "至少需要提供一个参数：User_Id、Group_Id或Medal_Id"
        }), 401
    
    if user_id is not None and group_id is not None:
        return jsonify({
            "code": 402,
            "message": "User_Id和Group_Id不能同时使用"
        }), 402
    
    try:
        # 根据参数组合执行不同的删除逻辑
        records_to_delete = []
        affected_users = []
        deleted_count = 0
        
        # 情况1: 只有user_id - 删除该用户的所有勋章
        if user_id is not None and medal_id is None:
            # 验证用户存在
            target_user = UserModel.query.filter_by(id=user_id).first()
            if not target_user:
                return jsonify({
                    "code": 404,
                    "message": "指定的用户不存在"
                }), 404
                
            # 获取该用户的所有勋章记录
            records_to_delete = MedalUserModel.query.filter_by(user_id=user_id).all()
            affected_users.append({
                "User_Id": target_user.id,
                "User_Name": target_user.username
            })
            
            # 更新用户的勋章数量
            if records_to_delete:
                target_user.medal = 0
                db.session.commit()
        
        # 情况2: 只有group_id - 删除该组所有成员的所有勋章
        elif group_id is not None and medal_id is None:
            # 查询组内所有成员
            group_members = GroupModel.query.filter_by(group_id=group_id).all()
            if not group_members:
                return jsonify({
                    "code": 404,
                    "message": "指定的组不存在或组内没有成员"
                }), 404
                
            # 获取组内所有成员ID
            member_ids = [member.student_id for member in group_members]
            
            # 对每个成员，删除其所有勋章记录
            for member_id in member_ids:
                member = UserModel.query.filter_by(id=member_id).first()
                if member:
                    member_records = MedalUserModel.query.filter_by(user_id=member_id).all()
                    records_to_delete.extend(member_records)
                    if member_records:
                        affected_users.append({
                            "User_Id": member.id,
                            "User_Name": member.username
                        })
                        # 重置该成员的勋章数量
                        member.medal = 0
            
            if affected_users:
                db.session.commit()
        
        # 情况3: 只有medal_id - 删除所有拥有该勋章的记录
        elif medal_id is not None and user_id is None and group_id is None:
            # 验证勋章存在
            target_medal = MedalModel.query.filter_by(id=medal_id).first()
            if not target_medal:
                return jsonify({
                    "code": 404,
                    "message": "指定的勋章不存在"
                }), 404
                
            # 获取拥有该勋章的所有记录
            records_to_delete = MedalUserModel.query.filter_by(medal_id=medal_id).all()
            
            # 收集受影响的用户
            for record in records_to_delete:
                user_info = UserModel.query.filter_by(id=record.user_id).first()
                if user_info:
                    affected_users.append({
                        "User_Id": user_info.id,
                        "User_Name": user_info.username
                    })
                    # 减少用户的勋章数量
                    if user_info.medal > 0:
                        user_info.medal -= 1
            
            if affected_users:
                db.session.commit()
        
        # 情况4: user_id和medal_id - 删除特定用户的特定勋章
        elif user_id is not None and medal_id is not None:
            # 验证用户和勋章存在
            target_user = UserModel.query.filter_by(id=user_id).first()
            target_medal = MedalModel.query.filter_by(id=medal_id).first()
            
            if not target_user:
                return jsonify({
                    "code": 404,
                    "message": "指定的用户不存在"
                }), 404
                
            if not target_medal:
                return jsonify({
                    "code": 404,
                    "message": "指定的勋章不存在"
                }), 404
            
            # 获取该用户的特定勋章记录
            records_to_delete = MedalUserModel.query.filter_by(
                user_id=user_id, 
                medal_id=medal_id
            ).all()
            
            if records_to_delete:
                affected_users.append({
                    "User_Id": target_user.id,
                    "User_Name": target_user.username
                })
                
                # 减少用户的勋章数量
                if target_user.medal > 0:
                    target_user.medal -= 1
                    db.session.commit()
        
        # 情况5: group_id和medal_id - 删除组内所有成员的特定勋章
        elif group_id is not None and medal_id is not None:
            # 验证组和勋章存在
            group_members = GroupModel.query.filter_by(group_id=group_id).all()
            target_medal = MedalModel.query.filter_by(id=medal_id).first()
            
            if not group_members:
                return jsonify({
                    "code": 404,
                    "message": "指定的组不存在或组内没有成员"
                }), 404
                
            if not target_medal:
                return jsonify({
                    "code": 404,
                    "message": "指定的勋章不存在"
                }), 404
            
            # 获取组内所有成员ID
            member_ids = [member.student_id for member in group_members]
            
            # 对每个成员，删除其特定勋章记录
            for member_id in member_ids:
                member_records = MedalUserModel.query.filter_by(
                    user_id=member_id, 
                    medal_id=medal_id
                ).all()
                
                if member_records:
                    records_to_delete.extend(member_records)
                    member = UserModel.query.filter_by(id=member_id).first()
                    if member:
                        affected_users.append({
                            "User_Id": member.id,
                            "User_Name": member.username
                        })
                        # 减少用户的勋章数量
                        if member.medal > 0:
                            member.medal -= 1
            
            if affected_users:
                db.session.commit()
        
        # 执行删除操作
        if records_to_delete:
            deleted_count = len(records_to_delete)
            for record in records_to_delete:
                db.session.delete(record)
            db.session.commit()
            
            return jsonify({
                "code": 200,
                "message": "勋章记录删除成功",
                "deleted_count": deleted_count,
                "affected_users": affected_users
            })
        else:
            return jsonify({
                "code": 200,
                "message": "没有找到符合条件的勋章记录",
                "deleted_count": 0
            })
            
    except Exception as e:
        db.session.rollback()
        return jsonify({
            "code": 500,
            "message": f"删除勋章记录失败: {str(e)}"
        }), 500
    
    
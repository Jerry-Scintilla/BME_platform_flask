from flask import Blueprint, request, jsonify

import os
import base64
# 导入拓展
from exts import db, redis_client

# 导入数据库表
from models import HomeCover, UserModel

# 导入表单验证
from .forms import HomeCoverForm

# 导入token验证模块
from flask_jwt_extended import (get_jwt_identity, jwt_required)

# 导入api文档模块
from flasgger import swag_from

bp = Blueprint("homeCover", __name__, url_prefix="")

@bp.route("/homeCover/update", methods=['POST'])
@jwt_required()
@swag_from('../apidocs/homeCover/update.yaml')
def upgrade():
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    mode = user.user_mode
    if mode != 'admin':
        return jsonify({
            "code": 400,
            'message': "用户权限不够"
        }), 400

    cover_id = request.args.get('Cover_Id')

    # 检查 cover_id 是否存在且为正整数
    if not cover_id or not cover_id.isdigit() or int(cover_id) <= 0:
        return jsonify({
            "code": 401,
            "message": "cover_id 必须是正整数"
        }), 401

    cover_id = int(cover_id)

    # 查询数据库，获取当前最大 cover_id
    max_cover_id = db.session.query(db.func.max(HomeCover.cover_id)).scalar()
    max_cover_id = max_cover_id or 0  # 如果表为空，max_cover_id 为 0

    # 检查 cover_id 是否符合逻辑
    if cover_id > max_cover_id + 1:
        return jsonify({
            "code": 402,
            "message": f"cover_id 超出范围，当前最大值为 {max_cover_id}，允许的最大值为 {max_cover_id + 1}"
        }), 402

    form = HomeCoverForm(request.files)
    # print(f"cover_id={cover_id}")
    # print(f"form={form.homeCover}")

    if form.validate():

        def adjustAndInsert_cover_ids(target_id):
            covers = HomeCover.query.filter(HomeCover.cover_id >= target_id).order_by(HomeCover.cover_id).all()
            if covers:
                for cover in covers:
                    cover.cover_id += 1
                db.session.commit()

            new_cover = HomeCover(cover_id=target_id)
            db.session.add(new_cover)
            db.session.commit()

        #插入记录
        adjustAndInsert_cover_ids(cover_id)

        cover = HomeCover.query.filter(HomeCover.cover_id == cover_id).first()

        #保存图片数据
        file = form.HomeCover.data
        filename = file.filename
        file.save('./data/homeCover/' + str(cover.id) + '.' + filename.rsplit(".", 1)[1].lower())
        # 保存头像路径到数据库
        cover.url = str(cover.id) + '.' + filename.rsplit(".", 1)[1].lower()
        db.session.commit()

        return jsonify({
            "code": 200,
            'message': "头像上传完成"
        })
    else:
        return jsonify({
            "code": 400,
            'message': form.errors
        }), 400


@bp.route("/homeCover/delete", methods=['POST'])
@jwt_required()
@swag_from('../apidocs/homeCover/delete.yaml')
def delete():
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    mode = user.user_mode
    if mode != 'admin':
        return jsonify({
            "code": 400,
            'message': "用户权限不够"
        }), 400

    cover_id = request.args.get('Cover_Id')

    if not cover_id:
        return jsonify({
            "code": 400,
            "message": "缺少 Cover_Id 参数"
        }), 400

    cover = HomeCover.query.filter_by(cover_id=cover_id).first()

    if not cover:
        return jsonify({
            "code": 404,
            "message": f"未找到 cover_id={cover_id} 的记录"
        }), 404

    image_path = cover.url
    if image_path:
        # 构造完整文件路径
        full_image_path = os.path.join('./data/homeCover/', image_path)

        # 检查文件是否存在并删除
        if os.path.exists(full_image_path):
            try:
                os.remove(full_image_path)  # 删除文件
            except Exception as e:
                return jsonify({
                    "code": 500,
                    "message": f"删除图片失败: {str(e)}"
                }), 500

    # 定义 Redis 缓存键
    cache_key = f"homeCover:base64:{cover.id}"
    # 清理 Redis 缓存
    redis_client.delete(cache_key)

    db.session.delete(cover)
    db.session.commit()

    # 新增逻辑：重新整理剩余记录的 cover_id 顺序
    remaining_covers = HomeCover.query.order_by(HomeCover.cover_id).all()
    for index, remaining_cover in enumerate(remaining_covers, start=1):
        remaining_cover.cover_id = index
    db.session.commit()

    return jsonify({
        "code": 200,
        "message": f"成功删除 cover_id={cover_id} 的记录"
    })

@bp.route("/homeCover/list", methods=["GET"])
@jwt_required()
@swag_from('../apidocs/homeCover/list.yaml')
def list():
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    mode = user.user_mode
    if mode != 'admin':
        return jsonify({
            "code": 400,
            'message': "用户权限不够"
        }), 400
    #查询数据库中的所有记录
    covers = HomeCover.query.order_by(HomeCover.cover_id).all()

    if not covers:
        return jsonify({
            "code": 404,
            "message": "数据库内没有图片"
        }), 404

    # 初始化结果数组
    result = []

    # 遍历每条记录
    for cover in covers:
        # 获取图片路径
        image_path = cover.url
        if not image_path:
            continue

        # 构造完整文件路径
        full_image_path = os.path.join('./data/homeCover/', image_path)

        # 定义 Redis 缓存键
        cache_key = f"homeCover:base64:{cover.id}"

        # 尝试从 Redis 缓存中获取 Base64 编码
        cached_base64 = redis_client.get(cache_key)
        if cached_base64:
            print(f"Cache hit for id: {cover.id}")
            result.append({
                "cover": cached_base64.decode('utf-8'),  # 将 bytes 转换为字符串
                "id": cover.cover_id
            })
            continue

        # 如果缓存未命中，检查文件是否存在并读取
        if os.path.exists(full_image_path):
            try:
                with open(full_image_path, 'rb') as image_file:
                    image_stream = image_file.read()
                    image_base64 = base64.b64encode(image_stream).decode()  # 转换为 Base64 编码
            except Exception as e:
                return jsonify({
                    "code": 500,
                    "message": f"读取图片失败: {str(e)}"
                }), 500

            # 将 Base64 编码存储到 Redis 缓存（设置过期时间为 1 小时）
            redis_client.setex(cache_key, 60*60*24*7, image_base64)

            # 将数据添加到结果数组
            result.append({
                "cover": image_base64,
                "id": cover.cover_id
            })
        else:
            # 如果图片文件不存在，跳过该记录
            continue

    # 返回成功响应
    return jsonify({
        "code": 200,
        "covers": result,
        "message": "图片流传输成功"
    })

@bp.route("/homeCover/search", methods=['GET'])
# @jwt_required()
@swag_from('../apidocs/homeCover/search.yaml')
def search():
    cover_id = request.args.get('Cover_Id')

    cover = HomeCover.query.filter(HomeCover.cover_id == cover_id).first()

    if not cover:
        return jsonify({
            "code": 404,
            "message": "数据库内没有图片"
        }), 404

    # 获取图片路径
    image_path = cover.url
    if not image_path:
        return jsonify({
            "code": 404,
            "message":"没有该图片路径"
        })

    picture = ''
    coverid = None

    # 构造完整文件路径
    full_image_path = os.path.join('./data/homeCover/', image_path)

    # 定义 Redis 缓存键
    cache_key = f"homeCover:base64:{cover.id}"
    # 尝试从 Redis 缓存中获取 Base64 编码
    cached_base64 = redis_client.get(cache_key)

    if cached_base64:
        print(f"Cache hit for id: {cover.id}")

        picture = cached_base64.decode('utf-8')# 将 bytes 转换为字符串
        coverid = cover.cover_id

    # 如果缓存未命中，检查文件是否存在并读取
    if os.path.exists(full_image_path):
        try:
            with open(full_image_path, 'rb') as image_file:
                image_stream = image_file.read()
                image_base64 = base64.b64encode(image_stream).decode()  # 转换为 Base64 编码
        except Exception as e:
            return jsonify({
                "code": 500,
                "message": f"读取图片失败: {str(e)}"
            }), 500
        # 将 Base64 编码存储到 Redis 缓存（设置过期时间为 1 小时）
        redis_client.setex(cache_key, 60*60*24*7, image_base64)
        # 将数据添加到结果
        picture = image_base64  # 将 bytes 转换为字符串
        coverid = cover.cover_id

    # 返回成功响应
    return jsonify({
        "code": 200,
        "covers": {
            "cover": picture,
            "cover_id":coverid
        },
        "message": "图片流传输成功"
    })




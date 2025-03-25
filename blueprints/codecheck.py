import random
from datetime import datetime, timezone, timedelta

from flask import Blueprint, request, redirect, jsonify, send_file
from wtforms.validators import email
import calendar, time, os

# 导入拓展
from exts import db, limiter, redis_client

# 导入数据库
from models import ArticleModel, UserModel, CheckRecord

# 导入表单验证
from .forms import ArticleForm

# 导入token验证模块
from flask_jwt_extended import (create_access_token, get_jwt_identity, jwt_required, JWTManager)

# 导入api文档模块
from flasgger import swag_from

bp = Blueprint("codecheck", __name__, url_prefix="")


# 生成验证码（管理员）
@bp.route('/generate-code', methods=['POST'])
@jwt_required()
@limiter.limit("1 per 5 seconds")
@swag_from('../apidocs/codecheck/generate_check_code.yaml')
def generate_check_code():
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    mode = user.user_mode
    if mode != 'admin':
        return jsonify({
            "code": 400,
            'message': "用户权限不够"
        }), 400

    code_type = request.json.get('type')
    if code_type not in ['check_in', 'check_out']:
        return jsonify({"error": "不正确的签码类型，类型需为check_in, check_out"}), 401

    # 生成并存储到Redis（自动过期）
    code = str(random.randint(100000, 999999))
    pipe = redis_client.pipeline()
    pipe.hset(f'check_code:{code}', mapping={
        'type': code_type,
        'generator_id': user.id,
        'used': '0'
    })
    pipe.expire(f'check_code:{code}', 300)  # 5分钟过期
    pipe.execute()

    return jsonify({
        "code": 200,
        "message": "签码生成成功",
        "check_code": code,
        "expires_in": "5min",
        "type": code_type
    })


# 签到/签退
@bp.route('/check', methods=['POST'])
@jwt_required()
@swag_from('../apidocs/codecheck/check_in_out.yaml')
def check_in_out():
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    code = request.json.get('check_code')

    # 从Redis获取验证码
    code_key = f'check_code:{code}'
    code_data = redis_client.hgetall(code_key)

    if not code_data:
        return jsonify({"error": "不存在的签码"}), 400

    # 转换为字符串
    code_data = {k.decode(): v.decode() for k, v in code_data.items()}

    if code_data['used'] == '1':
        return jsonify({"error": "签码已经使用"}), 401

    # 验证类型
    code_type = code_data['type']
    now = datetime.now()

    # 处理签到/签退逻辑
    if code_type == 'check_in':
        # 查找并删除最近未签退的记录
        reco = CheckRecord.query.filter(
            CheckRecord.user_id == user.id,
            CheckRecord.check_out.is_(None),
            CheckRecord.date == now.date()
        ).delete()
        record = CheckRecord(
            user_id=user.id,
            check_in=now,
            date=now.date()
        )
        db.session.add(record)
    else:
        # 查找最近未签退的记录
        record = CheckRecord.query.filter(
            CheckRecord.user_id == user.id,
            CheckRecord.check_out.is_(None),
            CheckRecord.date == now.date()
        ).order_by(CheckRecord.check_in.desc()).first()

        if not record:
            return jsonify({"error": "没有签到记录"}), 402

        record.check_out = now
        record.duration = (now - record.check_in).total_seconds() / 3600

    # 标记验证码已使用
    redis_client.hset(code_key, 'used', '1')
    db.session.commit()

    return jsonify({"message": "签到/签退成功"})


# 获取记录
@bp.route('/records', methods=['GET'])
@jwt_required()
@swag_from('../apidocs/codecheck/get_records.yaml')
def get_records():
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    now = datetime.now()

    # 获取当月第一天和最后一天
    first_day = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    next_month = first_day + timedelta(days=31)
    last_day = next_month.replace(day=1) - timedelta(days=1)

    # 查询当月记录并按日期聚合
    records = db.session.query(
        CheckRecord.date,
        db.func.sum(CheckRecord.duration).label('total_duration')
    ).filter(
        CheckRecord.user_id == user.id,
        CheckRecord.date >= first_day.date(),
        CheckRecord.date <= last_day.date(),
        CheckRecord.duration.isnot(None)
    ).group_by(CheckRecord.date).all()

    def format_duration(duration):
        hours = int(duration)
        minutes = int(round((duration - hours) * 60))
        if minutes >= 60:
            hours += 1
            minutes = 0
        return f"{hours}小时{minutes}分钟"

    # 生成当月完整日期列表
    date_range = [first_day + timedelta(days=x) for x in range((last_day - first_day).days + 1)]

    # 构建包含所有日期的结果
    result = []
    for day in date_range:
        date_str = day.date().isoformat()
        # 查找匹配的记录
        total = next((r.total_duration for r in records if r.date == day.date()), 0.0)

        result.append({
            "date": date_str,
            "total_duration": format_duration(total),
            "total_hours": round(total, 2)
        })

    return jsonify(result)

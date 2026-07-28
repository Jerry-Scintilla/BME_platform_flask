import random, json
from collections import defaultdict
from datetime import datetime, timedelta
from . import format_duration, generate_date_range, build_result
from flask import Blueprint, request, jsonify

# 导入拓展
from exts import db, limiter, redis_client

# 导入数据库
from models import UserModel, CheckRecord

# 导入token验证模块
from flask_jwt_extended import (get_jwt_identity, jwt_required)

# 导入api文档模块
from flasgger import swag_from

from . import check_permission, audit_log

bp = Blueprint("codecheck", __name__, url_prefix="")


# 生成签码（管理员）
@bp.route('/generate-code', methods=['POST'])
@jwt_required()
@check_permission('user_management')
@limiter.limit("1 per 5 seconds")
@swag_from('../apidocs/codecheck/generate_check_code.yaml')
def generate_check_code():
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()

    code_type = request.json.get('type')
    if code_type not in ['check_in', 'check_out']:
        return jsonify({"error": "不正确的签码类型，类型需为check_in, check_out"}), 401

    # 生成并存储到Redis（自动过期）
    # 使用时间戳+随机数确保唯一性
    import time
    timestamp = int(time.time())
    code = str(random.randint(100000, 999999))
    unique_key = f'check_code:{code_type}:{timestamp}:{code}'
    
    pipe = redis_client.pipeline()
    pipe.hset(unique_key, mapping={
        'type': code_type,
        'generator_id': user.id,
        'used': '0',
        'timestamp': timestamp
    })
    # 添加一个额外的键用于反查，将简单验证码映射到完整键名
    pipe.set(f'check_code_lookup:{code}', unique_key, ex=300)  # 5分钟过期
    pipe.expire(unique_key, 300)  # 5分钟过期
    pipe.execute()

    return jsonify({
        "code": 200,
        "message": "签码生成成功",
        "check_code": code,  # 只返回简单的6位数字码
        "expires_in": "5min",
        "type": code_type
    })


def invalidate_check_aggregate_cache():
    """任意用户签到/签退后失效全员聚合缓存。

    三个全员聚合接口（admin_records/records_top10/weekly_records）都是全员扫描，
    任一用户数据变更都会影响其结果，故无 user 维度、统一删当前周期 key。
    key 格式必须与三个接口内部拼出的 key 完全一致。
    """
    now = datetime.now()
    iso_year, iso_week, _ = now.isocalendar()
    keys = [
        f"annual_check_records_cache:{now.year}",
        f"check_records_top10:{now.strftime('%Y-%m')}",
        f"weekly_check_records_cache:{iso_year}-W{iso_week:02d}",
    ]
    redis_client.delete(*keys)


# 签到/签退
@bp.route('/check', methods=['POST'])
@jwt_required()
@swag_from('../apidocs/codecheck/check_in_out.yaml')
def check_in_out():
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    code = request.json.get('check_code')

    # 通过查找表获取完整的键名
    lookup_key = f'check_code_lookup:{code}'
    full_key = redis_client.get(lookup_key)
    
    if not full_key:
        return jsonify({"error": "不存在的签码或签码已过期"}), 400
        
    # 解码为字符串
    if isinstance(full_key, bytes):
        full_key = full_key.decode()
    
    # 从Redis获取验证码详细信息
    code_data = redis_client.hgetall(full_key)

    if not code_data:
        return jsonify({"error": "不存在的签码或签码已过期"}), 400

    # 转换为字符串
    code_data = {k.decode(): v.decode() for k, v in code_data.items()}

    if code_data['used'] == '1':
        return jsonify({"error": "签码已经使用"}), 401

    # 验证类型
    code_type = code_data['type']
    now = datetime.now()

    # 处理签到/签退逻辑
    if code_type == 'check_in':
        # 查找最近未签退的记录
        records = CheckRecord.query.filter(
            CheckRecord.user_id == user.id,
            CheckRecord.check_out.is_(None)
        ).order_by(CheckRecord.check_in.desc()).all()

        if records:
            # 取最近的记录判断时间
            latest_record = records[0]
            time_diff = (now - latest_record.check_in).total_seconds() / 3600
            if time_diff <= 6:
                return jsonify({"error": "已有未签退记录且未超过6小时"}), 403

            # 超过6小时则删除所有未签退记录
            for record in records:
                db.session.delete(record)

        record = CheckRecord(
            user_id=user.id,
            check_in=now,
            date=now.date()
        )
        db.session.add(record)

        # 签到成功后删除该用户的最新记录缓存
        redis_client.delete(f"latest_check:{user.id}")

    else:
        # 查找最近未签退的记录
        record = CheckRecord.query.filter(
            CheckRecord.user_id == user.id,
            CheckRecord.check_out.is_(None),
        ).order_by(CheckRecord.check_in.desc()).first()

        if not record:
            return jsonify({"error": "没有签到记录"}), 409

        record.duration = (now - record.check_in).total_seconds() / 3600

        # 如果时长超过6小时则删除记录，否则更新签退时间
        if record.duration > 6:
            db.session.delete(record)
        else:
            record.check_out = now
        
        # 签退成功后删除该用户的最新记录缓存
        redis_client.delete(f"latest_check:{user.id}")
        # 标记验证码已使用
        redis_client.hset(full_key, 'used', '1')

    db.session.commit()
    invalidate_check_aggregate_cache()

    return jsonify({"message": "签到/签退成功"})


# 人脸签到/签退（第三方服务接入）
@bp.route('/face_check', methods=['POST'])
@jwt_required()
@audit_log(operation='人脸签到/签退')
@swag_from('../apidocs/codecheck/face_check.yaml')
def face_check():
    check_status = request.json.get('status')  # 'check_in' 或 'check_out'
    third_party_token = request.json.get('token')  # 第三方凭据
    target_email = request.json.get('email')  # 实际签到的用户邮箱

    import os

    if os.getenv("FACE_SECRET") != third_party_token:
        return jsonify({"error": "第三方凭据无效"}), 401

    if not target_email:
        return jsonify({"error": "缺少签到用户邮箱"}), 400

    user = UserModel.query.filter_by(email=target_email).first()
    if not user:
        return jsonify({"error": "签到用户不存在"}), 404
    
    # 验证签到状态参数
    if check_status not in ['check_in', 'check_out']:
        return jsonify({"error": "不正确的签到状态，状态需为 check_in 或 check_out"}), 401

    now = datetime.now()
    
    # 处理签到/签退逻辑
    if check_status == 'check_in':
        # 查找最近未签退的记录
        records = CheckRecord.query.filter(
            CheckRecord.user_id == user.id,
            CheckRecord.check_out.is_(None)
        ).order_by(CheckRecord.check_in.desc()).all()
        
        if records:
            # 取最近的记录判断时间
            latest_record = records[0]
            time_diff = (now - latest_record.check_in).total_seconds() / 3600
            if time_diff <= 6:
                return jsonify({"error": "已有未签退记录且未超过 6 小时"}), 403
            
            # 超过 6 小时则删除所有未签退记录
            for record in records:
                db.session.delete(record)
        
        record = CheckRecord(
            user_id=user.id,
            check_in=now,
            date=now.date()
        )
        db.session.add(record)
        
        # 签到成功后删除该用户的最新记录缓存
        redis_client.delete(f"latest_check:{user.id}")
        
    else:
        # 查找最近未签退的记录
        record = CheckRecord.query.filter(
            CheckRecord.user_id == user.id,
            CheckRecord.check_out.is_(None),
        ).order_by(CheckRecord.check_in.desc()).first()
        
        if not record:
            return jsonify({"error": "没有签到记录"}), 409
        
        record.duration = (now - record.check_in).total_seconds() / 3600
        
        # 如果时长超过 6 小时则删除记录，否则更新签退时间
        if record.duration > 6:
            db.session.delete(record)
        else:
            record.check_out = now
        
        # 签退成功后删除该用户的最新记录缓存
        redis_client.delete(f"latest_check:{user.id}")
    
    db.session.commit()
    invalidate_check_aggregate_cache()

    return jsonify({"message": "人脸签到/签退成功"})


# 获取记录
@bp.route('/records', methods=['GET'])
@jwt_required()
@swag_from('../apidocs/codecheck/get_records.yaml')
def get_records():
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    now = datetime.now()
    today = now.date()

    # 获取本月和上个月的第一天和最后一天
    current_month_first = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    next_month = current_month_first + timedelta(days=31)
    current_month_last = next_month.replace(day=1) - timedelta(days=1)

    # 上个月的计算
    prev_month_last = current_month_first - timedelta(days=1)
    prev_month_first = prev_month_last.replace(day=1)

    # 查询两个月内的所有记录
    all_records = CheckRecord.query.filter(
        CheckRecord.user_id == user.id,
        CheckRecord.date >= prev_month_first.date(),
        CheckRecord.date <= current_month_last.date()
    ).all()

    # 按日期聚合数据
    date_info = defaultdict(lambda: {"total": 0.0, "has_open": False, "latest_checkin": None})

    for record in all_records:
        date = record.date
        if record.duration is not None:
            date_info[date]["total"] += record.duration
        if record.check_out is None:
            date_info[date]["has_open"] = True
            if date == today and (
                    date_info[date]["latest_checkin"] is None or record.check_in > date_info[date]["latest_checkin"]):
                date_info[date]["latest_checkin"] = record.check_in

    # 生成两个月完整日期列表
    current_month_dates = generate_date_range(current_month_first, current_month_last)
    prev_month_dates = generate_date_range(prev_month_first, prev_month_last)

    return jsonify({
        "current_month": {
            "month_name": current_month_first.strftime("%Y年%m月"),
            "records": build_result(
                dates=current_month_dates,
                date_info=date_info,
                today=today,
                now=now,
                is_current_month=True,
                format_duration=format_duration
            )
        },
        "previous_month": {
            "month_name": prev_month_first.strftime("%Y年%m月"),
            "records": build_result(
                dates=prev_month_dates,
                date_info=date_info,
                today=today,
                now=now,
                is_current_month=False,
                format_duration=format_duration
            )
        }
    })


@bp.route('/records/yearly', methods=['GET'])
@jwt_required()
@swag_from('../apidocs/codecheck/get_yearly_records.yaml')
def get_yearly_records():
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    # 个人年度日历：单用户当年记录仅几百~两千行、date 有索引、毫秒级，无需缓存。
    # 去掉缓存后每次请求直查 DB，新打卡立即可见；同时消除 key 不带年份的跨年串味隐患。
    now = datetime.now()
    # 获取当年第一天和最后一天
    first_day = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    last_day = now.replace(month=12, day=31, hour=23, minute=59, second=59, microsecond=999999)
    # 查询当年所有记录（包含已签退和未签退）
    all_records = CheckRecord.query.filter(
        CheckRecord.user_id == user.id,
        CheckRecord.date >= first_day.date(),
        CheckRecord.date <= last_day.date()
    ).all()

    # 按日聚合数据
    from collections import defaultdict
    daily_info = defaultdict(lambda: {"total": 0.0})
    for record in all_records:
        date_key = record.date.isoformat()
        if record.duration is not None:
            daily_info[date_key]["total"] += record.duration

    # 生成当年完整日期列表
    date_range = [first_day + timedelta(days=x) for x in range((last_day - first_day).days + 1)]

    # 构建返回结果
    result = []
    for day in date_range:
        date_key = day.date().isoformat()
        info = daily_info.get(date_key, {"total": 0.0})
        total = info["total"]

        formatted_duration = format_duration(total)

        result.append({
            "date": date_key,
            "total_duration": formatted_duration,
            "total_hours": round(total, 2)
        })

    return jsonify(result)


@bp.route('/admin_records', methods=['GET'])
@jwt_required()
@check_permission('user_management')
@swag_from('../apidocs/codecheck/admin_records.yaml')
def admin_records():
    # 获取当前日期
    now = datetime.now()
    current_year = now.year
    # Redis缓存键（带年份维度，避免跨年串味）
    cache_key = f"annual_check_records_cache:{current_year}"
    # 尝试从Redis获取缓存
    cached_data = redis_client.get(cache_key)
    if cached_data:
        # 如果缓存存在，直接返回缓存数据
        return jsonify(json.loads(cached_data))
    # 计算一年前的时间范围（当前日期往前推12个月）
    one_year_ago = now - timedelta(days=365)
    # 查询所有用户一年内的考勤记录
    all_records = CheckRecord.query.filter(
        CheckRecord.date >= one_year_ago.date(),
        CheckRecord.date <= now.date()
    ).all()

    # 按用户ID和年月分组汇总数据
    from collections import defaultdict
    user_monthly_data = defaultdict(lambda: defaultdict(float))

    for record in all_records:
        if record.duration is not None:  # 只计算已签退的记录
            year_month = record.date.strftime("%Y-%m")
            user_monthly_data[record.user_id][year_month] += record.duration

    # 获取所有用户信息用于返回用户名和学号
    users = UserModel.query.all()
    user_info = {user.id: {"name": user.username, "email": user.email, "student_id": user.student_id} for user in users}

    # 构建返回结果
    result = []
    for user_id, monthly_data in user_monthly_data.items():
        user_entry = {
            "user_id": user_id,
            "user_name": user_info.get(user_id, {}).get("name", ""),
            "user_email": user_info.get(user_id, {}).get("email", ""),
            "student_id": user_info.get(user_id, {}).get("student_id", ""),  # 添加学号字段
            "monthly_records": []
        }

        # 格式化每月数据
        for year_month, total_hours in sorted(monthly_data.items()):
            # 格式化时长
            hours = int(total_hours)
            minutes = int(round((total_hours - hours) * 60))
            if minutes >= 60:
                hours += 1
                minutes = 0
            formatted_duration = f"{hours}小时{minutes}分钟"

            user_entry["monthly_records"].append({
                "year_month": year_month,
                "total_duration": formatted_duration,
                "total_hours": round(total_hours, 2)
            })

        result.append(user_entry)

    # 将结果存入Redis，有效期1小时
    redis_client.setex(cache_key, timedelta(hours=1), json.dumps(result))

    return jsonify(result)


@bp.route('/records_top10', methods=['GET'])
@swag_from('../apidocs/codecheck/records_top10.yaml')
def records_top10():
    # Redis缓存键
    now = datetime.now()
    current_month = now.strftime("%Y-%m")
    cache_key = f"check_records_top10:{current_month}"  # 带月份维度，避免月初串味
    # 尝试从Redis获取缓存
    cached_data = redis_client.get(cache_key)
    if cached_data:
        return jsonify(json.loads(cached_data))

    # 获取当前月份的第一天和最后一天
    first_day = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last_day = (first_day + timedelta(days=32)).replace(day=1) - timedelta(days=1)

    # 查询所有用户当前月份的考勤记录
    all_records = CheckRecord.query.filter(
        CheckRecord.date >= first_day.date(),
        CheckRecord.date <= last_day.date()
    ).all()

    # 按用户ID汇总数据
    from collections import defaultdict
    user_data = defaultdict(float)

    for record in all_records:
        if record.duration is not None:  # 只计算已签退的记录
            user_data[record.user_id] += record.duration

    # 获取所有用户信息用于返回用户名
    users = UserModel.query.all()
    user_info = {user.id: {"name": user.username, "id": user.id} for user in users}

    # 按时长排序并取前10
    top_users = sorted(
        [(user_id, total_hours) for user_id, total_hours in user_data.items()],
        key=lambda x: x[1],
        reverse=True
    )[:10]

    # 构建返回结果
    result = []
    for rank, (user_id, total_hours) in enumerate(top_users, 1):  # 添加排名，从1开始
        # 格式化时长
        hours = int(total_hours)
        minutes = int(round((total_hours - hours) * 60))
        if minutes >= 60:
            hours += 1
            minutes = 0
        formatted_duration = f"{hours}小时{minutes}分钟"

        result.append({
            "rank": rank,  # 添加排名字段
            "user_name": user_info.get(user_id, {}).get("name", ""),
            "user_id": user_info.get(user_id, {}).get("id", ""),
            "total_duration": formatted_duration,
            "total_hours": round(total_hours, 2)
        })

    # 将结果存入Redis，有效期1小时
    redis_client.setex(cache_key, timedelta(hours=1), json.dumps(result))

    return jsonify(result)


# 获取用户个人出勤统计
@bp.route('/records/my_stats', methods=['GET'])
@jwt_required()
def get_my_records_stats():
    """获取当前用户的出勤统计数据：累计天数、本月时长、本月排名"""
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    if not user:
        return jsonify({"code": 404, "message": "用户不存在"}), 404

    now = datetime.now()

    # 1. 获取累计出勤天数（从有记录以来）
    all_records = CheckRecord.query.filter(
        CheckRecord.user_id == user.id,
        CheckRecord.duration != None
    ).all()

    # 按日期去重计算天数
    attendance_dates = set()
    for record in all_records:
        if record.date:
            attendance_dates.add(record.date)
    total_days = len(attendance_dates)

    # 2. 获取本月时长
    first_day = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last_day = (first_day + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    month_records = CheckRecord.query.filter(
        CheckRecord.user_id == user.id,
        CheckRecord.date >= first_day.date(),
        CheckRecord.date <= last_day.date(),
        CheckRecord.duration != None
    ).all()

    month_hours = sum(record.duration or 0 for record in month_records)

    # 3. 获取本月排名
    # 先计算所有用户的本月时长
    all_month_records = CheckRecord.query.filter(
        CheckRecord.date >= first_day.date(),
        CheckRecord.date <= last_day.date(),
        CheckRecord.duration != None
    ).all()

    user_month_hours = defaultdict(float)
    for record in all_month_records:
        user_month_hours[record.user_id] += record.duration or 0

    # 排序获取排名
    sorted_users = sorted(user_month_hours.items(), key=lambda x: x[1], reverse=True)
    my_rank = None
    for rank, (uid, hours) in enumerate(sorted_users, 1):
        if uid == user.id:
            my_rank = rank
            break

    return jsonify({
        "code": 200,
        "data": {
            "total_days": total_days,
            "month_hours": round(month_hours, 2),
            "month_rank": my_rank
        }
    })


@bp.route('/weekly_records', methods=['GET'])
@jwt_required()
@check_permission('user_management')
@swag_from('../apidocs/codecheck/weekly_records.yaml')
def weekly_records():
    # 获取当前日期和时间
    now = datetime.now()
    # Redis缓存键（带 ISO 周维度，避免跨周串味）
    iso_year, iso_week, _ = now.isocalendar()
    cache_key = f"weekly_check_records_cache:{iso_year}-W{iso_week:02d}"
    # 尝试从Redis获取缓存
    cached_data = redis_client.get(cache_key)
    if cached_data:
        return jsonify(json.loads(cached_data))
    # 计算本周的开始日期（周一）和结束日期（周日）
    current_week_start = now - timedelta(days=now.weekday())
    current_week_end = current_week_start + timedelta(days=6)
    # 计算上周的开始日期和结束日期
    last_week_start = current_week_start - timedelta(days=7)
    last_week_end = last_week_start + timedelta(days=6)
    # 查询所有用户本周和上周的考勤记录
    all_records = CheckRecord.query.filter(
        CheckRecord.date >= last_week_start.date(),
        CheckRecord.date <= current_week_end.date()
    ).all()

    # 按用户ID和周分组汇总数据
    from collections import defaultdict
    user_weekly_data = defaultdict(lambda: {
        'current_week': 0.0,
        'last_week': 0.0
    })
    for record in all_records:
        if record.duration is not None:  # 只计算已签退的记录
            if last_week_start.date() <= record.date <= last_week_end.date():
                user_weekly_data[record.user_id]['last_week'] += record.duration
            elif current_week_start.date() <= record.date <= current_week_end.date():
                user_weekly_data[record.user_id]['current_week'] += record.duration

    # 获取所有用户信息用于返回用户名和学号
    users = UserModel.query.all()
    user_info = {
        user.id: {
            "name": user.username,
            "email": user.email,
            "student_id": user.student_id  # 添加学号字段
        }
        for user in users
    }
    # 构建返回结果
    result = []
    for user_id, weekly_data in user_weekly_data.items():
        # 格式化本周时长
        current_hours = int(weekly_data['current_week'])
        current_minutes = int(round((weekly_data['current_week'] - current_hours) * 60))
        if current_minutes >= 60:
            current_hours += 1
            current_minutes = 0
        current_formatted = f"{current_hours}小时{current_minutes}分钟"
        # 格式化上周时长
        last_hours = int(weekly_data['last_week'])
        last_minutes = int(round((weekly_data['last_week'] - last_hours) * 60))
        if last_minutes >= 60:
            last_hours += 1
            last_minutes = 0
        last_formatted = f"{last_hours}小时{last_minutes}分钟"
        user_entry = {
            "user_id": user_id,
            "user_name": user_info.get(user_id, {}).get("name", ""),
            "user_email": user_info.get(user_id, {}).get("email", ""),
            "student_id": user_info.get(user_id, {}).get("student_id", ""),  # 添加学号字段
            "current_week": {
                "start_date": current_week_start.strftime("%Y-%m-%d"),
                "end_date": current_week_end.strftime("%Y-%m-%d"),
                "total_duration": current_formatted,
                "total_hours": round(weekly_data['current_week'], 2)
            },
            "last_week": {
                "start_date": last_week_start.strftime("%Y-%m-%d"),
                "end_date": last_week_end.strftime("%Y-%m-%d"),
                "total_duration": last_formatted,
                "total_hours": round(weekly_data['last_week'], 2)
            }
        }
        result.append(user_entry)

    # 将结果存入Redis，有效期1小时
    redis_client.setex(cache_key, timedelta(hours=1), json.dumps(result))

    return jsonify(result)


@bp.route('/lateset_checktime', methods=['GET'])
@jwt_required()
@swag_from('../apidocs/codecheck/lateset_checktime.yaml')
def lateset_checktime():
    # 从JWT中获取用户邮箱
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    
    # 定义Redis缓存键
    cache_key = f"latest_check:{user.id}"
    
    # 尝试从Redis获取缓存
    cached_data = redis_client.get(cache_key)
    if cached_data:
        # 如果缓存存在，直接返回缓存数据
        return jsonify(json.loads(cached_data))
    
    # 缓存未命中，从数据库查询
    latest_record = CheckRecord.query.filter(
        CheckRecord.user_id == user.id
    ).order_by(CheckRecord.check_in.desc()).first()
    
    # 如果没有记录，返回空结果
    if not latest_record:
        result = {
            "user_id": user.id,
            "username": user.username,
            "has_record": False
        }
    else:
        # 构建结果
        result = {
            "user_id": user.id,
            "username": user.username,
            "has_record": True,
            "check_in_time": latest_record.check_in.strftime("%Y-%m-%d %H:%M:%S") if latest_record.check_in else None,
            "check_out_time": latest_record.check_out.strftime("%Y-%m-%d %H:%M:%S") if latest_record.check_out else None,
            "duration": round(latest_record.duration, 2) if latest_record.duration else None,
            "date": latest_record.date.strftime("%Y-%m-%d") if latest_record.date else None
        }
    
    # 将结果存入Redis，设置1小时过期时间
    redis_client.setex(cache_key, timedelta(hours=24), json.dumps(result))
    
    return jsonify(result)
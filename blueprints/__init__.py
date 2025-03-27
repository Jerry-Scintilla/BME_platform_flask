from datetime import datetime, timedelta
from collections import defaultdict

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


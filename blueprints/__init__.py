# 辅助函数：格式化时长
def format_duration(duration):
    hours = int(duration)
    minutes = int(round((duration - hours) * 60))
    if minutes >= 60:
        hours += 1
        minutes = 0
    return f"{hours}小时{minutes}分钟"
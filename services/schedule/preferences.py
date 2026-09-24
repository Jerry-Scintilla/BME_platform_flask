"""日程偏好：每人一份 Profile 的 get_or_create 与校验更新（Phase 1 首用引导仅三个必设项的落点）。"""
import re
from datetime import time

from exts import db
from models import ScheduleProfile

from . import VersionConflict

AUTOMATION_MODES = ('manual', 'suggest', 'auto')

_HHMM_RE = re.compile(r'^(\d{1,2}):(\d{2})$')


def parse_hhmm(value, field='时间'):
    """'HH:MM' → datetime.time；格式非法 raise ValueError。"""
    if not isinstance(value, str):
        raise ValueError(f"{field} 应为 HH:MM 格式")
    m = _HHMM_RE.match(value.strip())
    if not m:
        raise ValueError(f"{field} 应为 HH:MM 格式")
    hour, minute = int(m.group(1)), int(m.group(2))
    if hour > 23 or minute > 59:
        raise ValueError(f"{field} 不是合法时间")
    return time(hour, minute)


def lock_profile(user_id):
    """排程序闸锁（锁序规范见包 docstring）：将要运行规划器的事务必须先持
    该用户 profile 行锁，再写任何 task/event/block 行。返回 profile。"""
    profile = (db.session.query(ScheduleProfile)
               .filter(ScheduleProfile.user_id == user_id)
               .with_for_update().populate_existing().first())
    if profile is None:
        profile = get_or_create_profile(user_id)
        db.session.flush()
        # get_or_create 无并发竞争防护，但 uq_schedule_profile_user 撞键即
        # IntegrityError → 整事务失败重试，语义可接受（首次访问才会走到）
    return profile


def get_or_create_profile(user_id):
    """返回该用户的 Profile；首次访问自动建行（默认值见模型定义）。
    只 add/flush 不 commit（事务边界见包 docstring）。"""
    profile = ScheduleProfile.query.filter_by(user_id=user_id).first()
    if profile is None:
        profile = ScheduleProfile(user_id=user_id)
        db.session.add(profile)
        db.session.flush()
    return profile


def apply_profile_update(profile, payload, expected_version):
    """按白名单更新偏好（timezone Phase 1 不开放修改）。版本不符 raise
    VersionConflict；任何字段非法 raise ValueError（整次更新不落半截）。"""
    if expected_version != profile.version:
        raise VersionConflict("偏好已被修改，请刷新后重试")

    updates = {}
    if 'day_start_time' in payload:
        parse_hhmm(payload['day_start_time'], '可安排开始时间')   # 先整体校验
        updates['day_start_time'] = payload['day_start_time'].strip()
    if 'day_end_time' in payload:
        parse_hhmm(payload['day_end_time'], '可安排结束时间')
        updates['day_end_time'] = payload['day_end_time'].strip()
    new_start = updates.get('day_start_time', profile.day_start_time)
    new_end = updates.get('day_end_time', profile.day_end_time)
    if parse_hhmm(new_start, '可安排开始时间') >= parse_hhmm(new_end, '可安排结束时间'):
        raise ValueError("可安排开始时间必须早于结束时间")

    if 'default_reminder_minutes' in payload:
        updates['default_reminder_minutes'] = _int_range(
            payload['default_reminder_minutes'], 0, 24 * 60, '默认提醒提前量')
    if 'automation_mode' in payload:
        if payload['automation_mode'] not in AUTOMATION_MODES:
            raise ValueError("自动安排模式取值非法")
        updates['automation_mode'] = payload['automation_mode']

    for field, value in updates.items():
        setattr(profile, field, value)
    profile.version += 1
    return profile


def _int_range(value, lo, hi, field):
    if value in (None, ''):
        raise ValueError(f"{field} 不能为空")
    try:
        v = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} 应为整数")
    if not (lo <= v <= hi):
        raise ValueError(f"{field} 应在 {lo}-{hi} 之间")
    return v

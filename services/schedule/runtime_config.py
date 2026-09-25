"""日程服务在线配置（面板批次 B3，§9.2）。

读取优先级：schedule_setting 表非空 value > 环境变量/应用默认。三个白名单
键在请求路径即时读取（无进程缓存——desired 与 effective 即时收敛，不需要
desired/effective 双版本核对；后续若引入缓存须回到 §9.2 的双版本口径）。

DB 故障时静默回落默认（可用性优先：配置读挂了不能打死用户端功能）。
模型、扫描间隔、线程池等需各自生效机制的键不进白名单（仍走 .env）。
"""
from datetime import datetime

from flask import current_app
from sqlalchemy.exc import SQLAlchemyError

from exts import db
from models import ScheduleSetting, UserModel

from . import VersionConflict

# 白名单：key -> {label, type(bool/int), validator, default(), component}
EDITABLE_KEYS = {
    'SCHEDULE_INTENT_ENABLED': {
        'label': 'AI 意图录入开关', 'type': 'bool',
        'component': 'POST /schedule/captures',
        'note': '关闭仅阻止新的 AI 录入请求；已提交的处理正常收尾',
    },
    'SCHEDULE_PLANNER_ENABLED': {
        'label': '自动排程开关', 'type': 'bool',
        'component': '任务自动安排与事件让位重排',
        'note': '关闭不取消既有任务、执行块和提醒，仅停止新的自动安排',
    },
    'SCHEDULE_INTENT_DAILY_LIMIT': {
        'label': 'AI 录入每日限额（每用户）', 'type': 'int', 'min': 1, 'max': 1000,
        'component': 'POST /schedule/captures 限流',
        'note': '超出限额的用户当日收到 429 提示',
    },
}


def _row(key):
    try:
        return ScheduleSetting.query.filter_by(key=key).first()
    except SQLAlchemyError:
        db.session.rollback()
        return None


def _default(key):
    return current_app.config.get(key)


def effective(key):
    """有效值：DB 覆盖（非空）> 环境变量/默认。bool 键以 'true'/'false' 文本存储。"""
    spec = EDITABLE_KEYS.get(key)
    if spec is None:
        return _default(key)
    row = _row(key)
    if row is None or row.value is None:
        return _default(key)
    if spec['type'] == 'bool':
        return str(row.value).lower() == 'true'
    try:
        return int(row.value)
    except (TypeError, ValueError):
        return _default(key)


def intent_enabled():
    return effective('SCHEDULE_INTENT_ENABLED')


def planner_enabled():
    return effective('SCHEDULE_PLANNER_ENABLED')


def intent_daily_limit():
    value = effective('SCHEDULE_INTENT_DAILY_LIMIT')
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return 50


def _validate(key, value):
    """入参值 → 规范文本；非法 raise ValueError。None=恢复默认。"""
    spec = EDITABLE_KEYS.get(key)
    if spec is None:
        raise ValueError(f"配置键 {key} 不在可在线修改白名单")
    if value is None:
        return None
    if spec['type'] == 'bool':
        if isinstance(value, bool):
            return 'true' if value else 'false'
        if str(value).lower() in ('true', '1', 'on'):
            return 'true'
        if str(value).lower() in ('false', '0', 'off'):
            return 'false'
        raise ValueError(f"{spec['label']} 应为布尔值")
    if spec['type'] == 'int':
        try:
            number = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"{spec['label']} 应为整数")
        if not (spec['min'] <= number <= spec['max']):
            raise ValueError(f"{spec['label']} 应在 {spec['min']}-{spec['max']} 之间")
        return str(number)
    return str(value)[:200]


def apply_updates(updates, admin_user):
    """发布新版本（§9.2）：逐键 expected_version 乐观锁（行不存在视为版本 0，
    首次发布 expected_version 传 0），任一冲突整批 409 不落半截。
    返回 [{'key', 'from', 'to', 'version'}]；调用方事务内执行、蓝图 commit。"""
    results = []
    for item in updates or []:
        key = item.get('key')
        if key not in EDITABLE_KEYS:
            raise ValueError(f"配置键 {key} 不在可在线修改白名单")
        new_value = _validate(key, item.get('value'))
        expected = item.get('expected_version')
        if expected is None:
            raise ValueError(f"{key} 缺少 expected_version")
        row = (ScheduleSetting.query
               .filter_by(key=key)
               .with_for_update().populate_existing().first())
        current_version = row.version if row is not None else 0
        if int(expected) != current_version:
            raise VersionConflict(f"{EDITABLE_KEYS[key]['label']} 已被他人修改，请刷新后重试")
        old_value = row.value if row is not None else None
        if row is None:
            row = ScheduleSetting(key=key, value=new_value, version=1)
            db.session.add(row)
        else:
            row.value = new_value
            row.version += 1
        row.previous_value = old_value
        row.reason = (item.get('reason') or '')[:200] or None
        row.updated_by = admin_user.id if admin_user is not None else None
        row.updated_at = datetime.now()
        results.append({'key': key,
                        'label': EDITABLE_KEYS[key]['label'],
                        'from': _display(old_value), 'to': _display(new_value),
                        'version': row.version})
    db.session.flush()
    return results


def editable_view():
    """白名单键的期望值/生效值/版本与发布信息（管理端渲染用）。"""
    items = []
    for key, spec in EDITABLE_KEYS.items():
        row = _row(key)
        default_value = _default(key)
        current = effective(key)
        overridden = row is not None and row.value is not None
        publisher = None
        if row is not None and row.updated_by:
            user = UserModel.query.filter_by(id=row.updated_by).first()
            publisher = user.username if user else None
        items.append({
            'key': key, 'label': spec['label'], 'type': spec['type'],
            'component': spec['component'], 'note': spec.get('note'),
            'default_value': default_value,
            'desired_value': row.value if overridden else None,
            'effective_value': current,
            'overridden': overridden,
            'version': row.version if row is not None else 0,
            'updated_at': row.updated_at.strftime('%Y-%m-%d %H:%M') if row is not None and row.updated_at else None,
            'updated_by_name': publisher,
            'reason': row.reason if row is not None else None,
        })
    return items


def _display(value):
    if value is None:
        return '默认'
    return value

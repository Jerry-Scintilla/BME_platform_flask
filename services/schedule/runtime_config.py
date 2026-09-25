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

# 白名单：key -> {label, type(bool/int/str/secret), validator, default(), component}
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
        'label': 'AI 录入每日限额', 'type': 'int', 'min': 1, 'max': 1000,
        'unit': '次/天（每用户）',
        'component': 'POST /schedule/captures 限流',
        'note': '单位=提交次数：每用户每天最多提交 N 句「说一句」录入（一句话记 1 次，'
                '与拆出的事项数无关；同一 request_id 的网络重试不重复计数）；超出当日返回 429',
    },
    'SCHEDULE_INTENT_MODEL': {
        'label': '意图理解模型', 'type': 'str', 'max_len': 60,
        'component': 'intent.parse_capture（逐请求无状态读取，改后下一次录入即用新模型）',
        'note': '留空恢复回退链：环境变量 SCHEDULE_INTENT_MODEL > AI_TOPIC_MODEL > deepseek-chat',
    },
    'DEEPSEEK_API_KEY': {
        'label': 'DeepSeek API Key', 'type': 'secret', 'min_len': 20, 'max_len': 200,
        'component': 'litellm_chat.chat_completion（逐请求读取）',
        'note': '平台覆盖 > .env；值只写不读（任何接口与审计都不回显）；留空回落 .env',
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
    """有效值：DB 覆盖（非空）> 环境变量/默认。bool 键以 'true'/'false' 文本存储，
    str/secret 键按原文返回（secret 仅供 llm_api_key() 等内部消费方读取，
    管理端序列化层永不透出）。"""
    spec = EDITABLE_KEYS.get(key)
    if spec is None:
        return _default(key)
    row = _row(key)
    if row is None or row.value is None:
        return _default(key)
    if spec['type'] == 'bool':
        return str(row.value).lower() == 'true'
    if spec['type'] in ('str', 'secret'):
        return str(row.value)
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


def intent_model():
    """意图模型解析链：DB 覆盖 > env SCHEDULE_INTENT_MODEL > env AI_TOPIC_MODEL > deepseek-chat。"""
    override = effective('SCHEDULE_INTENT_MODEL')
    if isinstance(override, str) and override.strip():
        return override.strip()
    fallback = current_app.config.get('AI_TOPIC_MODEL')
    return fallback or 'deepseek-chat'


def llm_api_key():
    """LLM 密钥解析链：DB 覆盖（schedule_setting）> .env DEEPSEEK_API_KEY。
    返回 None 表示未配置（chat_completion 会抛 RuntimeError）。"""
    override = effective('DEEPSEEK_API_KEY')
    if isinstance(override, str) and override.strip():
        return override.strip()
    import os
    return os.getenv('DEEPSEEK_API_KEY')


def _validate(key, value):
    """入参值 → 规范文本；非法 raise ValueError。None=恢复默认/回落 .env。"""
    spec = EDITABLE_KEYS.get(key)
    if spec is None:
        raise ValueError(f"配置键 {key} 不在可在线修改白名单")
    if value is None or (spec['type'] != 'str' and spec['type'] != 'secret' and value == ''):
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
    text = str(value).strip()
    if spec['type'] == 'secret':
        # 密钥：只做长度/字符白名单校验，绝不回显；入库前不再变形
        if not (spec.get('min_len', 20) <= len(text) <= spec.get('max_len', 200)):
            raise ValueError(f"{spec['label']} 长度应为 {spec.get('min_len', 20)}-{spec.get('max_len', 200)} 字符")
        if any(ch.isspace() for ch in text):
            raise ValueError(f"{spec['label']} 不应包含空白字符")
        return text
    # str：模型名等标识符
    if not text or len(text) > spec.get('max_len', 60) or any(ch.isspace() for ch in text):
        raise ValueError(f"{spec['label']} 应为无空白的标识符（≤{spec.get('max_len', 60)} 字符）")
    return text


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
                        'from': _display(old_value, key), 'to': _display(new_value, key),
                        'version': row.version})
    db.session.flush()
    return results


def editable_view():
    """白名单键的期望值/生效值/版本与发布信息（管理端渲染用）。
    secret 键【绝不】返回 value——只出配置状态与长度提示（审计日志只记响应体，
    响应不含明文即审计不含明文）。"""
    import os
    items = []
    for key, spec in EDITABLE_KEYS.items():
        row = _row(key)
        overridden = row is not None and row.value is not None
        publisher = None
        if row is not None and row.updated_by:
            user = UserModel.query.filter_by(id=row.updated_by).first()
            publisher = user.username if user else None
        item = {
            'key': key, 'label': spec['label'], 'type': spec['type'],
            'unit': spec.get('unit'),
            'component': spec['component'], 'note': spec.get('note'),
            'overridden': overridden,
            'version': row.version if row is not None else 0,
            'updated_at': row.updated_at.strftime('%Y-%m-%d %H:%M') if row is not None and row.updated_at else None,
            'updated_by_name': publisher,
            'reason': row.reason if row is not None else None,
        }
        if spec['type'] == 'secret':
            env_configured = bool(os.getenv(key))
            item.update({
                'default_value': '环境变量' if env_configured else '未配置',
                'desired_value': None,          # 永不回显
                'effective_value': '已配置（%s）' % ('平台配置' if overridden
                                                    else ('环境变量' if env_configured else '缺失')),
                'configured': overridden or env_configured,
            })
        else:
            default_value = _default(key)
            item.update({
                'default_value': default_value,
                'desired_value': row.value if overridden else None,
                'effective_value': effective(key),
            })
        items.append(item)
    return items


def _display(value, key=None):
    if value is None:
        return '默认'
    if key is not None and EDITABLE_KEYS.get(key, {}).get('type') == 'secret':
        return '已更新（不回显）'
    return value

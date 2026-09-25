"""文字意图提取（Phase 2）：LLM 只产候选，服务端白名单校验产事实（§9）。

四层防御：response_json JSON mode；逐项降级（单条坏不毁整单）；仅
JSONDecodeError 重试 1 次（timeout=45s，最坏 ~95s）；最终落库在 capture
worker 的单个大事务里（全有或全无）。

歧义策略（§4.3）：不能确定的时间不写成已确定事实——含任一 ambiguity 的事项
不落库，随 capture 行持久保存问题与选项，等用户 resolve 补答。默认值必须标
注来源（30 分钟 → duration_source='default_30'）。
"""
import json
from datetime import datetime, timedelta

import requests

from litellm_chat import chat_completion          # 测试 monkeypatch 本模块属性即可离线

from .calendar import parse_date, parse_datetime

MAX_ITEMS = 10
DEFAULT_DURATION = 30
PAST_TOLERANCE = timedelta(minutes=5)

WEEKDAYS = ['一', '二', '三', '四', '五', '六', '日']

_UNSET = '__unset__'      # 歧义选项「不设定该时间」的哨兵值


class IntentError(Exception):
    """LLM 输出不可解析。code: llm_invalid / llm_timeout / internal"""

    def __init__(self, message, code='llm_invalid'):
        super().__init__(message)
        self.code = code


# ── Prompt ────────────────────────────────────────────────────────────────

def build_messages(text, now=None):
    now = now or datetime.now()
    tomorrow = now + timedelta(days=1)
    system = f"""你是「BME 个人日程助手」的意图提取器。用户用一句中文描述要做的事，你把它拆解成结构化事项。

当前时间：{now.strftime('%Y-%m-%d %H:%M')}（星期{WEEKDAYS[now.weekday()]}，时区 Asia/Shanghai）。今天={now.strftime('%Y-%m-%d')}，明天={tomorrow.strftime('%Y-%m-%d')}。

提取规则：
1. 只提取「新增事项」。待办任务 kind=task（有截止或时长，无确定起止）；有明确开始和结束时刻的固定安排 kind=event（会议/实验/课程）。如果整句是改期、完成、取消、查询类请求（例如"把报告改到明天""报告写完了"），不要生成 items，把原句放进 unparsed。
2. 所有相对时间换算为绝对时间，格式 YYYY-MM-DD HH:MM。例如"明早九点"= {tomorrow.strftime('%Y-%m-%d')} 09:00，"周五下午三点"按当前日期推算的那个周五。
3. 有日期但没有几点几分：若是截止（"周五前交"），填 due_date=YYYY-MM-DD、due_at 留 null；若是日程（"明天下午开会"但没有起止时刻），不要编造 start_at，在 ambiguities 里提问。
4. 无法判断上午/下午的时刻（"三点"）：不要猜，在 ambiguities 里提问，options 给上午/下午两个选项，value 必须是完整的 YYYY-MM-DD HH:MM。
5. 用户明确说了时长（"两小时""半小时"）才填 duration_minutes；没说就留 null，禁止估计。priority、reminder_minutes 同理，只在用户明说时填。
6. 一句话可能包含多件事，逐件拆开，每件附 evidence=用户原话片段（不超过 30 字）。
7. 无法归类为新增事项的片段，原样放入 unparsed，不要硬造事项。
8. 最多输出 10 件；超出的部分写进 unparsed。
9. 时间均为 24 小时制。标题不要超过 20 字。
10. 只输出 JSON，禁止任何解释文字或代码块标记。结构：
{{"items":[{{"kind":"task 或 event","title":"标题","evidence":"原话片段","start_at":"YYYY-MM-DD HH:MM 或 null","end_at":"YYYY-MM-DD HH:MM 或 null","due_at":"YYYY-MM-DD HH:MM 或 null","due_date":"YYYY-MM-DD 或 null","duration_minutes":数字或 null,"priority":"low/medium/high 或 null","location":"地点 或 null","reminder_minutes":数字或 null,"ambiguities":[{{"field":"字段名","question":"一句中文提问","options":[{{"label":"选项","value":"值"}}]}}]}}],"unparsed":["无法归类的片段"]}}"""
    return [{'role': 'system', 'content': system},
            {'role': 'user', 'content': text}]


def parse_capture(text, now=None, *, llm=None):
    """LLM 提取 + 服务端归一化。返回 {'items': [...], 'unparsed': [...]}。

    items 每项：{index, kind(task/event), title, evidence, status(ready/
    needs_clarification/failed), fields{...}, duration_source, ambiguities}。
    模型与密钥走 runtime_config 在线解析链（DB 覆盖 > env/默认）。
    LLM 两次结构不可解析 raise IntentError('llm_invalid')。"""
    from .runtime_config import intent_model, llm_api_key
    now = now or datetime.now()
    call = llm or chat_completion
    api_key = llm_api_key() if llm is None else None      # 测试注入 llm 时不覆盖密钥
    from flask import current_app
    try:
        timeout = current_app.config.get('SCHEDULE_INTENT_LLM_TIMEOUT', 45)
        model = intent_model()
    except RuntimeError:
        timeout, model = 45, None

    last_err = None
    raw = None
    for _ in range(2):
        try:
            content = call(build_messages(text, now), model=model,
                           temperature=0.2, timeout=timeout, response_json=True,
                           api_key=api_key)
            raw = json.loads(content)
            break
        except (json.JSONDecodeError, ValueError) as exc:   # 仅解析失败重试
            last_err = exc
        except requests.exceptions.Timeout:
            raise IntentError('理解服务超时，请稍后重试', code='llm_timeout')
        except requests.exceptions.HTTPError:
            raise IntentError('理解服务暂时不可用，请稍后重试', code='internal')
    if raw is None:
        raise IntentError('无法理解这句话，请换个说法或手动创建', code='llm_invalid')
    if not isinstance(raw, dict):
        raise IntentError('无法理解这句话，请换个说法或手动创建', code='llm_invalid')

    raw_items = raw.get('items') if isinstance(raw.get('items'), list) else []
    unparsed = [str(x)[:120] for x in (raw.get('unparsed') or []) if x][:10]
    overflow = raw_items[MAX_ITEMS:]
    if overflow:
        unparsed = (['…（超出 10 项上限的部分未处理）'] + unparsed)[:10]

    items = [normalize_item(ri, idx, now) for idx, ri in enumerate(raw_items[:MAX_ITEMS])]
    return {'items': items, 'unparsed': unparsed}


# ── 归一化与校验（§3.2 逐项降级） ─────────────────────────────────────────

def _empty_fields():
    return {'start_at': None, 'end_at': None, 'due_at': None, 'due_date': None,
            'deadline_precision': 'none', 'duration_minutes': None,
            'priority': 'medium', 'location': None, 'reminder_minutes': None}


def _ask(item, field, question, options=None):
    item['ambiguities'].append({'field': field, 'question': question,
                                'options': options or []})


def _past_question(now, field):
    tomorrow = (now + timedelta(days=1)).strftime('%Y-%m-%d')
    return [
        {'label': '明天同一时间', 'value': f'{tomorrow} {now.strftime("%H:%M")}'},
        {'label': '不设定该时间', 'value': _UNSET},
    ]


def normalize_item(raw, idx, now, *, answers=None):
    """单个 LLM 候选 → 服务端规范化结构。answers（resolve 时）按字段回填后
    重新校验；'_unset' 哨兵清空字段。"""
    if not isinstance(raw, dict):
        raw = {}
    answers = answers or {}
    item = {'index': idx, 'kind': 'task', 'title': '', 'evidence': '',
            'status': 'ready', 'fields': _empty_fields(),
            'duration_source': 'user', 'ambiguities': []}
    f = item['fields']

    evidence = str(raw.get('evidence') or '').strip()
    item['evidence'] = evidence[:60]
    title = str(raw.get('title') or '').strip()[:200]
    if not title:
        title = evidence[:200]
    if not title:
        item['status'] = 'failed'
        return item
    item['title'] = title

    # LLM 自报的歧义先入列（服务端校验可能再追加）；字段/选项做白名单清洗
    for a in (raw.get('ambiguities') or []):
        if isinstance(a, dict) and a.get('field') and a.get('question'):
            options = [{'label': str(o.get('label'))[:50], 'value': str(o.get('value'))[:32]}
                       for o in (a.get('options') or [])
                       if isinstance(o, dict) and o.get('value')][:4]
            _ask(item, str(a['field'])[:20], str(a['question'])[:120], options)

    kind = raw.get('kind')
    start_raw, end_raw = raw.get('start_at'), raw.get('end_at')
    if kind == 'event' or (start_raw and end_raw):
        item['kind'] = 'event'
    else:
        item['kind'] = 'task'

    # 时刻字段：先套 answers 回填，再解析
    for field in ('start_at', 'end_at', 'due_at'):
        value = answers.get(field, raw.get(field))
        f[field] = _parse_time_field(item, field, value, now)
    due_date_raw = answers.get('due_date', raw.get('due_date'))
    if due_date_raw == _UNSET:
        due_date_raw = None
    if due_date_raw:
        try:
            f['due_date'] = parse_date(due_date_raw, '截止日期')
        except ValueError:
            _ask(item, 'due_date', '截止日期无法识别，请直接输入正确日期')
        if f['due_date'] and f['due_date'] < now.date():
            _ask(item, 'due_date', '截止日期是过去的日期，按哪一种处理？', _past_question(now, 'due_date'))

    if f['start_at'] and f['end_at'] and f['end_at'] <= f['start_at']:
        _ask(item, 'end_at', '结束时间必须晚于开始时间，请直接输入正确时间')

    # 时刻语义纠偏：任务但用户/补答给出了确定起止 → 是固定日程（确认的时间
    # 必须落成事实，不能静默丢弃）；只给了开始 → 追问结束时刻
    if item['kind'] == 'task' and f['start_at']:
        item['kind'] = 'event'

    # event 完整性：起止缺一即问（已因解析失败/过去时间问过的字段不重复问）；
    # 两个时刻都被 '_unset' 清掉 → 退化为任务
    if item['kind'] == 'event' and not f['start_at'] and not f['end_at'] and item['status'] != 'failed':
        item['kind'] = 'task'
    if item['kind'] == 'event' and item['status'] != 'failed':
        asked = {a['field'] for a in item['ambiguities']}
        if not f['start_at'] and 'start_at' not in asked:
            _ask(item, 'start_at', '这件事几点到几点？（请输入完整时间，如 2026-09-25 15:00）')
        if f['start_at'] and not f['end_at'] and 'end_at' not in asked:
            duration = _norm_int(raw.get('duration_minutes'), 1, 1440)
            if duration:
                f['end_at'] = f['start_at'] + timedelta(minutes=duration)
            else:
                _ask(item, 'end_at', '「%s」几点结束？（请输入完整时间）' % item['title'])

    # duration
    duration = _norm_int(raw.get('duration_minutes'), 1, 1440)
    if item['kind'] == 'event' and f['start_at'] and f['end_at']:
        duration = int((f['end_at'] - f['start_at']).total_seconds() / 60)
    if item['kind'] == 'task':
        if duration is None:
            duration = DEFAULT_DURATION
            item['duration_source'] = 'default_30'
        if f['due_at'] and f['due_date']:
            f['due_date'] = None            # 双截止口径：取 due_at（datetime 精度）
        f['deadline_precision'] = ('datetime' if f['due_at'] else
                                   'date' if f['due_date'] else 'none')
    f['duration_minutes'] = duration

    priority = raw.get('priority')
    f['priority'] = priority if priority in ('low', 'medium', 'high') else 'medium'
    reminder = _norm_int(raw.get('reminder_minutes'), 0, 1440)
    f['reminder_minutes'] = reminder
    location = raw.get('location')
    f['location'] = str(location).strip()[:200] if location else None

    if item['ambiguities']:
        item['status'] = 'needs_clarification'
    return item


def _parse_time_field(item, field, value, now):
    """解析单个时刻字段；解析失败/过去时间 → 歧义（不写成已确定事实）。"""
    if value is None or value == _UNSET:
        return None
    if not isinstance(value, str):
        _ask(item, field, '时间无法识别，请直接输入完整时间（如 2026-09-25 15:00）')
        return None
    try:
        parsed = parse_datetime(value.strip(), field)
    except ValueError:
        _ask(item, field, '时间无法识别，请直接输入完整时间（如 2026-09-25 15:00）')
        return None
    if parsed < now - PAST_TOLERANCE:
        _ask(item, field, '这个时间是过去的，按哪一种处理？', _past_question(now, field))
    return parsed


def _norm_int(value, lo, hi):
    if value is None:
        return None
    try:
        v = int(value)
    except (TypeError, ValueError):
        return None
    return v if lo <= v <= hi else None


# ── resolve：歧义补答 ─────────────────────────────────────────────────────

def resolve_item(item, answers, now=None):
    """把用户的补答回填到规范化 item 并重新校验（不再调 LLM）。
    answers: {field: value}，value 为完整值或 '__unset__'。"""
    now = now or datetime.now()
    return normalize_item({'kind': item['kind'], 'title': item['title'],
                           'evidence': item['evidence'],
                           'start_at': _answer_or(item, answers, 'start_at'),
                           'end_at': _answer_or(item, answers, 'end_at'),
                           'due_at': _answer_or(item, answers, 'due_at'),
                           'due_date': _answer_or(item, answers, 'due_date'),
                           'duration_minutes': item['fields'].get('duration_minutes'),
                           'priority': item['fields'].get('priority'),
                           'location': item['fields'].get('location'),
                           'reminder_minutes': item['fields'].get('reminder_minutes')},
                          item['index'], now, answers=answers)


def _answer_or(item, answers, field):
    """answers 里显式给出的值优先；'_unset' 语义由 _parse_time_field 处理；
    未回答的字段沿用上轮 LLM 原值（存疑时保持存疑）。"""
    if field in answers:
        return answers[field]
    previous = item['fields'].get(field)
    return previous.strftime('%Y-%m-%d %H:%M') if isinstance(previous, datetime) else previous

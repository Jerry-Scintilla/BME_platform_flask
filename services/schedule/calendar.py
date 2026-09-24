"""日程对象服务：任务/固定日程/执行块 CRUD、冲突检测、事实流水与逾期口径。

三类对象必须分开（规划文档 §5.1）：Event 固定占时、默认不可自动移动；Task
是待完成内容，执行时间落在 Block（计划≠实际耗时，事实在 Activity）；提醒
独立物料化。冲突只检测不阻断（§5.3 允许用户保留冲突并标红）。

逾期/截止口径（全链路单点，前端共用）：
- 已逾期   = status='open' 且（precision='datetime' 且 due_at < now，
             或 precision='date' 且 due_date < today）
- 今日截止 = due_at 落今日，或 due_date == today；precision='none' 永不逾期

服务层不 commit（事务边界见包 docstring）；入参错 ValueError / 越权或不存在
NotFound / 版本不符 VersionConflict，由蓝图映射 400/404/409。编辑截止时
前端须整组提交三件套（deadline_precision + due_at + due_date）。
"""
from datetime import date, datetime, time, timedelta

from sqlalchemy import and_, case, func, or_

from exts import db
from models import ScheduleActivity, ScheduleBlock, ScheduleEvent, ScheduleTask

from . import NotFound, VersionConflict
from .preferences import get_or_create_profile, parse_hhmm
from .reminders import cancel_target, rematerialize_target

AGENDA_MAX_DAYS = 31
TASK_BUCKETS = ('all', 'unscheduled', 'overdue', 'scheduled', 'today_due', 'done', 'cancelled')
DEADLINE_PRECISIONS = ('none', 'datetime', 'date')
PRIORITIES = ('low', 'medium', 'high')

_DATETIME_FORMATS = ('%Y-%m-%d %H:%M', '%Y-%m-%dT%H:%M', '%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S')


# ── 入参解析与校验 ────────────────────────────────────────────────────────

def parse_datetime(value, field):
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if not isinstance(value, str):
        raise ValueError(f"{field} 应为时间字符串")
    for fmt in _DATETIME_FORMATS:
        try:
            return datetime.strptime(value.strip(), fmt)
        except ValueError:
            continue
    raise ValueError(f"{field} 格式应为 YYYY-MM-DD HH:MM")


def parse_date(value, field):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise ValueError(f"{field} 应为日期字符串")
    try:
        return datetime.strptime(value.strip(), '%Y-%m-%d').date()
    except ValueError:
        raise ValueError(f"{field} 格式应为 YYYY-MM-DD")


def _text(value, field, max_len):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 不能为空")
    value = value.strip()
    if len(value) > max_len:
        raise ValueError(f"{field} 不能超过 {max_len} 字")
    return value


def _optional_int(value, field, lo=0, hi=24 * 60):
    if value in (None, ''):
        return None
    try:
        v = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} 应为整数")
    if not (lo <= v <= hi):
        raise ValueError(f"{field} 应在 {lo}-{hi} 之间")
    return v


def _optional_text(value, max_len):
    if value is None or value == '':
        return None
    return str(value).strip()[:max_len] or None


def _like(keyword):
    return "%" + keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _log(user_id, action, *, task_id=None, block_id=None, occurred_at=None,
         actual_minutes=None, note=None, source='manual'):
    db.session.add(ScheduleActivity(
        user_id=user_id, task_id=task_id, block_id=block_id, action=action,
        occurred_at=occurred_at or datetime.now(),
        actual_minutes=actual_minutes,
        note=_optional_text(note, 500),
        source=source))


def _fetch_locked(model, owner_id, obj_id):
    """按归属取行并加锁；不存在或不属于本人一律 NotFound（不暴露存在性）。
    populate_existing：同会话可能已有该对象的陈旧副本（如本请求早前读过）。"""
    row = (db.session.query(model)
           .filter(model.id == obj_id, model.owner_id == owner_id)
           .with_for_update().populate_existing().first())
    if row is None:
        raise NotFound("日程对象不存在")
    return row


def _deadline_from_payload(payload, task=None):
    """截止三态自洽（模型 docstring 口径）。创建（task=None）只看 payload；
    更新时三件套任一出现才整组覆盖，未出现则保持原值。"""
    if task is None or any(k in payload for k in ('deadline_precision', 'due_at', 'due_date')):
        precision = payload.get('deadline_precision') or 'none'
        if precision not in DEADLINE_PRECISIONS:
            raise ValueError("截止精度取值非法")
        if precision == 'datetime':
            if not payload.get('due_at'):
                raise ValueError("选择「截止到时刻」时必须填写截止时间")
            return precision, parse_datetime(payload['due_at'], '截止时间'), None
        if precision == 'date':
            if not payload.get('due_date'):
                raise ValueError("选择「截止到日期」时必须填写截止日期")
            return precision, None, parse_date(payload['due_date'], '截止日期')
        return precision, None, None
    return task.deadline_precision, task.due_at, task.due_date


# ── 任务 ─────────────────────────────────────────────────────────────────

def create_task(owner_id, payload):
    title = _text(payload.get('title'), '标题', 200)
    precision, due_at, due_date = _deadline_from_payload(payload)
    priority = payload.get('priority') or 'medium'
    if priority not in PRIORITIES:
        raise ValueError("优先级取值非法")

    task = ScheduleTask(
        owner_id=owner_id, title=title,
        description=_optional_text(payload.get('description'), 2000),
        deadline_precision=precision, due_at=due_at, due_date=due_date,
        estimated_minutes=_optional_int(payload.get('estimated_minutes'), '估时', 1, 24 * 60),
        priority=priority,
        reminder_minutes=_optional_int(payload.get('reminder_minutes'), '提醒提前量'))
    db.session.add(task)
    db.session.flush()
    _log(owner_id, 'task_create', task_id=task.id)
    reminder_created = rematerialize_target('task', task)
    return {'task': task.to_dict(), 'reminder_created': reminder_created}


def update_task(owner_id, task_id, payload, expected_version):
    task = _fetch_locked(ScheduleTask, owner_id, task_id)
    if expected_version != task.version:
        raise VersionConflict("任务已被修改，请刷新后重试")
    if 'status' in payload:
        raise ValueError("任务状态需通过操作接口修改")

    if 'title' in payload:
        task.title = _text(payload.get('title'), '标题', 200)
    if 'description' in payload:
        task.description = _optional_text(payload.get('description'), 2000)
    if 'priority' in payload:
        if payload['priority'] not in PRIORITIES:
            raise ValueError("优先级取值非法")
        task.priority = payload['priority']
    if 'estimated_minutes' in payload:
        task.estimated_minutes = _optional_int(payload.get('estimated_minutes'), '估时', 1, 24 * 60)
    if 'reminder_minutes' in payload:
        task.reminder_minutes = _optional_int(payload.get('reminder_minutes'), '提醒提前量')
    precision, due_at, due_date = _deadline_from_payload(payload, task)
    task.deadline_precision, task.due_at, task.due_date = precision, due_at, due_date

    task.version += 1
    db.session.flush()
    _log(owner_id, 'task_update', task_id=task.id)
    rematerialize_target('task', task)
    return {'task': task.to_dict()}


def task_action(owner_id, task_id, action, extra=None):
    extra = extra or {}
    task = _fetch_locked(ScheduleTask, owner_id, task_id)
    now = datetime.now()

    if action == 'complete':
        if task.status != 'open':
            raise ValueError("只有进行中的任务可以完成")
        task.status = 'done'
        task.completed_at = now
        task.version += 1
        _cancel_future_blocks(task, now)
        cancel_target('task', task.id)
        _log(owner_id, 'task_complete', task_id=task.id,
             actual_minutes=_optional_int(extra.get('actual_minutes'), '实际投入'),
             note=extra.get('note'))
    elif action == 'reopen':
        if task.status not in ('done', 'cancelled'):
            raise ValueError("只有已完成或已取消的任务可以重新打开")
        task.status = 'open'
        task.completed_at = None
        task.version += 1
        _log(owner_id, 'task_reopen', task_id=task.id)
        rematerialize_target('task', task)     # 按当前截止重算（含 clamp 立即提醒）
    elif action == 'cancel':
        if task.status != 'open':
            raise ValueError("只有进行中的任务可以取消")
        task.status = 'cancelled'
        task.version += 1
        _cancel_future_blocks(task, now)
        cancel_target('task', task.id)
        _log(owner_id, 'task_cancel', task_id=task.id, note=extra.get('note'))
    elif action == 'progress':
        if task.status != 'open':
            raise ValueError("只有进行中的任务可以反馈进度")
        task.remaining_minutes = _optional_int(extra.get('remaining_minutes'), '剩余时长')
        task.version += 1
        _log(owner_id, 'task_progress', task_id=task.id,
             actual_minutes=_optional_int(extra.get('actual_minutes'), '实际投入'),
             note=extra.get('note'))
    else:
        raise ValueError("不支持的操作")

    db.session.flush()
    return {'task': task.to_dict()}


def _cancel_future_blocks(task, now):
    """任务完结联动：未来 planned 块取消（各自的提醒一并作废、留审计）。"""
    for block in ScheduleBlock.query.filter(
            ScheduleBlock.task_id == task.id,
            ScheduleBlock.status == 'planned',
            ScheduleBlock.start_at > now).all():
        block.status = 'cancelled'
        block.version += 1
        cancel_target('block', block.id)
        _log(task.owner_id, 'block_cancel', task_id=task.id, block_id=block.id,
             note='任务完结联动取消')


def get_task_detail(owner_id, task_id):
    task = ScheduleTask.query.filter(
        ScheduleTask.id == task_id, ScheduleTask.owner_id == owner_id).first()
    if task is None:
        raise NotFound("任务不存在")
    blocks = (ScheduleBlock.query.filter(ScheduleBlock.task_id == task.id)
              .order_by(ScheduleBlock.start_at.desc()).limit(20).all())
    activities = (ScheduleActivity.query.filter(ScheduleActivity.task_id == task.id)
                  .order_by(ScheduleActivity.occurred_at.desc(), ScheduleActivity.id.desc())
                  .limit(20).all())
    return {'task': task.to_dict(),
            'blocks': [b.to_dict() for b in blocks],
            'activities': [a.to_dict() for a in activities]}


def list_tasks(owner_id, *, page=1, per_page=20, bucket='all', q=''):
    if bucket not in TASK_BUCKETS:
        raise ValueError("任务分组取值非法")
    now = datetime.now()
    today = now.date()

    query = ScheduleTask.query.filter(ScheduleTask.owner_id == owner_id)
    if q:
        query = query.filter(ScheduleTask.title.like(_like(q), escape="\\"))

    if bucket == 'done':
        query = query.filter(ScheduleTask.status == 'done')
    elif bucket == 'cancelled':
        query = query.filter(ScheduleTask.status == 'cancelled')
    else:
        query = query.filter(ScheduleTask.status == 'open')
        if bucket == 'overdue':
            query = query.filter(or_(
                and_(ScheduleTask.deadline_precision == 'datetime', ScheduleTask.due_at < now),
                and_(ScheduleTask.deadline_precision == 'date', ScheduleTask.due_date < today)))
        elif bucket == 'today_due':
            query = query.filter(or_(
                and_(ScheduleTask.deadline_precision == 'datetime',
                     ScheduleTask.due_at >= datetime.combine(today, time(0, 0)),
                     ScheduleTask.due_at < datetime.combine(today + timedelta(days=1), time(0, 0))),
                and_(ScheduleTask.deadline_precision == 'date', ScheduleTask.due_date == today)))
        else:
            has_future_block = db.session.query(ScheduleBlock.id).filter(
                ScheduleBlock.task_id == ScheduleTask.id,
                ScheduleBlock.status == 'planned',
                ScheduleBlock.start_at >= now).exists()
            if bucket == 'unscheduled':
                query = query.filter(~has_future_block)
            elif bucket == 'scheduled':
                query = query.filter(has_future_block)

    total = query.count()
    due_expr = func.coalesce(ScheduleTask.due_at, ScheduleTask.due_date)
    rows = (query
            .order_by(case((due_expr.is_(None), 1), else_=0), due_expr, ScheduleTask.id.desc())
            .offset((page - 1) * per_page).limit(per_page).all())
    pages = (total + per_page - 1) // per_page
    return {'items': [t.to_dict() for t in rows], 'total': total,
            'page': page, 'per_page': per_page, 'pages': pages}


# ── 固定日程 ─────────────────────────────────────────────────────────────

def create_event(owner_id, payload):
    title = _text(payload.get('title'), '标题', 200)
    start_at = parse_datetime(payload.get('start_at'), '开始时间')
    end_at = parse_datetime(payload.get('end_at'), '结束时间')
    if end_at <= start_at:
        raise ValueError("结束时间必须晚于开始时间")

    event = ScheduleEvent(
        owner_id=owner_id, title=title,
        description=_optional_text(payload.get('description'), 2000),
        location=_optional_text(payload.get('location'), 200),
        start_at=start_at, end_at=end_at,
        all_day=bool(payload.get('all_day', False)),
        busy=bool(payload.get('busy', True)),
        reminder_minutes=_optional_int(payload.get('reminder_minutes'), '提醒提前量'))
    db.session.add(event)
    db.session.flush()
    _log(owner_id, 'event_create')
    rematerialize_target('event', event)
    return {'event': event.to_dict(), 'conflicts': conflicts_for(owner_id, 'event', event)}


def update_event(owner_id, event_id, payload, expected_version):
    event = _fetch_locked(ScheduleEvent, owner_id, event_id)
    if expected_version != event.version:
        raise VersionConflict("日程已被修改，请刷新后重试")
    if 'status' in payload:
        raise ValueError("日程状态需通过删除接口修改")

    if 'title' in payload:
        event.title = _text(payload.get('title'), '标题', 200)
    if 'description' in payload:
        event.description = _optional_text(payload.get('description'), 2000)
    if 'location' in payload:
        event.location = _optional_text(payload.get('location'), 200)
    start_at = parse_datetime(payload['start_at'], '开始时间') if 'start_at' in payload else event.start_at
    end_at = parse_datetime(payload['end_at'], '结束时间') if 'end_at' in payload else event.end_at
    if end_at <= start_at:
        raise ValueError("结束时间必须晚于开始时间")
    event.start_at, event.end_at = start_at, end_at
    if 'all_day' in payload:
        event.all_day = bool(payload['all_day'])
    if 'busy' in payload:
        event.busy = bool(payload['busy'])
    if 'reminder_minutes' in payload:
        event.reminder_minutes = _optional_int(payload.get('reminder_minutes'), '提醒提前量')

    event.version += 1
    db.session.flush()
    _log(owner_id, 'event_update')
    rematerialize_target('event', event)
    return {'event': event.to_dict(), 'conflicts': conflicts_for(owner_id, 'event', event)}


def cancel_event(owner_id, event_id):
    event = _fetch_locked(ScheduleEvent, owner_id, event_id)
    if event.status != 'active':
        raise NotFound("日程对象不存在")
    event.status = 'cancelled'
    event.version += 1
    cancel_target('event', event.id)
    _log(owner_id, 'event_cancel')
    db.session.flush()
    return {'id': event.id}


# ── 执行块 ───────────────────────────────────────────────────────────────

def create_block(owner_id, payload):
    task = ScheduleTask.query.filter(
        ScheduleTask.id == payload.get('task_id'),
        ScheduleTask.owner_id == owner_id,
        ScheduleTask.status == 'open').first()
    if task is None:
        raise NotFound("任务不存在或不可安排")
    start_at = parse_datetime(payload.get('start_at'), '开始时间')
    end_at = parse_datetime(payload.get('end_at'), '结束时间')
    if end_at <= start_at:
        raise ValueError("结束时间必须晚于开始时间")

    block = ScheduleBlock(
        owner_id=owner_id, task_id=task.id, start_at=start_at, end_at=end_at,
        locked=bool(payload.get('locked', True)))
    db.session.add(block)
    db.session.flush()
    _log(owner_id, 'block_create', task_id=task.id, block_id=block.id)
    rematerialize_target('block', block)
    return {'block': block.to_dict(with_task=True),
            'conflicts': conflicts_for(owner_id, 'block', block)}


def update_block(owner_id, block_id, payload, expected_version):
    block = _fetch_locked(ScheduleBlock, owner_id, block_id)
    if expected_version != block.version:
        raise VersionConflict("时间块已被修改，请刷新后重试")
    if 'status' in payload or 'task_id' in payload:
        raise ValueError("时间块状态与所属任务不可直接修改")
    if block.status != 'planned':
        raise ValueError("已结束或已取消的时间块不可修改")

    start_at = parse_datetime(payload['start_at'], '开始时间') if 'start_at' in payload else block.start_at
    end_at = parse_datetime(payload['end_at'], '结束时间') if 'end_at' in payload else block.end_at
    if end_at <= start_at:
        raise ValueError("结束时间必须晚于开始时间")
    block.start_at, block.end_at = start_at, end_at
    if 'locked' in payload:
        block.locked = bool(payload['locked'])

    block.version += 1
    db.session.flush()
    _log(owner_id, 'block_update', task_id=block.task_id, block_id=block.id)
    rematerialize_target('block', block)
    return {'block': block.to_dict(with_task=True),
            'conflicts': conflicts_for(owner_id, 'block', block)}


def cancel_block(owner_id, block_id):
    block = _fetch_locked(ScheduleBlock, owner_id, block_id)
    if block.status != 'planned':
        raise NotFound("时间块不存在")
    block.status = 'cancelled'
    block.version += 1
    cancel_target('block', block.id)
    _log(owner_id, 'block_cancel', task_id=block.task_id, block_id=block.id)
    db.session.flush()
    return {'id': block.id}


# ── 聚合视图与冲突 ───────────────────────────────────────────────────────

def _conflict_items(owner_id, window_start, window_end):
    """窗口内参与冲突检测的对象：active+busy+非全天日程 与 planned 执行块。"""
    items = []
    events = ScheduleEvent.query.filter(
        ScheduleEvent.owner_id == owner_id, ScheduleEvent.status == 'active',
        ScheduleEvent.busy.is_(True), ScheduleEvent.all_day.is_(False),
        ScheduleEvent.start_at < window_end, ScheduleEvent.end_at > window_start).all()
    blocks = ScheduleBlock.query.filter(
        ScheduleBlock.owner_id == owner_id, ScheduleBlock.status == 'planned',
        ScheduleBlock.start_at < window_end, ScheduleBlock.end_at > window_start).all()
    for e in events:
        items.append({'type': 'event', 'id': e.id, 'title': e.title,
                      'start_at': e.start_at, 'end_at': e.end_at})
    for b in blocks:
        items.append({'type': 'block', 'id': b.id,
                      'title': b.task.title if b.task else f'任务块 #{b.task_id}',
                      'start_at': b.start_at, 'end_at': b.end_at})
    return items


def _brief(item):
    return {'type': item['type'], 'id': item['id'], 'title': item['title'],
            'start_at': item['start_at'].strftime('%Y-%m-%d %H:%M'),
            'end_at': item['end_at'].strftime('%Y-%m-%d %H:%M')}


def detect_conflicts(owner_id, window_start, window_end):
    """窗口内全部冲突对（agenda 用）。只检测不阻断。"""
    items = _conflict_items(owner_id, window_start, window_end)
    pairs = []
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            a, b = items[i], items[j]
            if a['start_at'] < b['end_at'] and b['start_at'] < a['end_at']:
                pairs.append({'a': _brief(a), 'b': _brief(b)})
    return pairs


def conflicts_for(owner_id, kind, obj):
    """写操作响应用：与该对象直接相关的冲突对（排除自身）。"""
    window = (obj.start_at, obj.end_at)
    mine = {'type': kind, 'id': obj.id,
            'title': obj.title if kind == 'event' else
            (obj.task.title if getattr(obj, 'task', None) else f'任务块 #{obj.task_id}'),
            'start_at': obj.start_at, 'end_at': obj.end_at}
    pairs = []
    for other in _conflict_items(owner_id, window[0], window[1]):
        if other['type'] == kind and other['id'] == obj.id:
            continue
        if mine['start_at'] < other['end_at'] and other['start_at'] < mine['end_at']:
            pairs.append({'a': _brief(mine), 'b': _brief(other)})
    return pairs


def get_agenda(owner_id, date_from, date_to, profile=None):
    """区间视图：日程 + 执行块（含任务名）+ 冲突 + 净可用分钟。跨度 ≤31 天。"""
    date_from = parse_date(date_from, '开始日期')
    date_to = parse_date(date_to, '结束日期')
    if date_to < date_from:
        raise ValueError("结束日期不能早于开始日期")
    if (date_to - date_from).days + 1 > AGENDA_MAX_DAYS:
        raise ValueError(f"查询跨度不能超过 {AGENDA_MAX_DAYS} 天")

    window_start = datetime.combine(date_from, time(0, 0))
    window_end = datetime.combine(date_to + timedelta(days=1), time(0, 0))

    events = (ScheduleEvent.query.filter(
        ScheduleEvent.owner_id == owner_id, ScheduleEvent.status == 'active',
        ScheduleEvent.start_at < window_end, ScheduleEvent.end_at > window_start)
        .order_by(ScheduleEvent.start_at).all())
    blocks = (ScheduleBlock.query.filter(
        ScheduleBlock.owner_id == owner_id,
        ScheduleBlock.status.in_(('planned', 'done')),
        ScheduleBlock.start_at < window_end, ScheduleBlock.end_at > window_start)
        .order_by(ScheduleBlock.start_at).all())
    conflicts = detect_conflicts(owner_id, window_start, window_end)
    profile = profile or get_or_create_profile(owner_id)

    return {
        'from': date_from.strftime('%Y-%m-%d'),
        'to': date_to.strftime('%Y-%m-%d'),
        'events': [e.to_dict() for e in events],
        'blocks': [b.to_dict(with_task=True) for b in blocks],
        'conflicts': conflicts,
        'day_stats': _day_stats(profile, events, blocks, date_from, date_to,
                                conflict_count=len(conflicts)),
    }


def _day_stats(profile, events, blocks, date_from, date_to, *, conflict_count):
    """逐日净可用分钟：每日可安排窗口（profile 日窗口）扣除 busy 日程与
    planned 块占用（对齐规划 §5.4：不能把全部日历空白视为可工作时间）。"""
    day_start = parse_hhmm(profile.day_start_time, '可安排开始时间')
    day_end = parse_hhmm(profile.day_end_time, '可安排结束时间')
    busy_spans = [(e.start_at, e.end_at) for e in events if e.busy and not e.all_day]
    busy_spans += [(b.start_at, b.end_at) for b in blocks if b.status == 'planned']

    available = 0
    day = date_from
    while day <= date_to:
        w0 = datetime.combine(day, day_start)
        w1 = datetime.combine(day, day_end)
        used = sum((min(t, w1) - max(s, w0)).total_seconds() / 60
                   for s, t in busy_spans if t > w0 and s < w1)
        available += max(0.0, (w1 - w0).total_seconds() / 60 - used)
        day += timedelta(days=1)
    return {'available_minutes': int(available), 'conflict_count': conflict_count}

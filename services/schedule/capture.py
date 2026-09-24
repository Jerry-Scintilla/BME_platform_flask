"""「说一句」录入 worker（Phase 2）：提交→线程池→状态轮询形态（全站首例）。

capture 行是唯一真相，线程只是推进状态机。事务结构（上下文③，见包 docstring）：
1. claim 小事务：pending → processing（区分「排队没开始」与「开始了卡住」）；
2. LLM 调用：无事务（禁止跨 LLM 调用持有写事务）；
3. 最终大事务：锁 profile（锁序规范）→ 落库无歧义项 → 排程 → 写 plan/change
   →【条件更新】SET 终态 WHERE status='processing'，rowcount=0 整体 rollback
   ——被惰性恢复判死的旧 worker 自动放弃全部业务写入，绝不产生重复任务；
4. 失败小事务：仅条件更新 processing → failed。

线程卫生：ThreadPoolExecutor(2) 懒初始化（dev reloader 父进程不建池）；worker
内 app_context + finally db.session.remove()（scoped_session 按线程缓存连接）。
LLM 原始输出不落库不进日志（§12），日志只记 id/耗时/状态。
"""
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from exts import db
from models import ScheduleCapture, ScheduleEvent, ScheduleTask

from . import NotFound, VersionConflict
from . import intent as intent_mod
from . import planner
from .calendar import create_event, create_task
from .preferences import lock_profile
from .planner import record_change_dict

STALE_AFTER = timedelta(minutes=3)   # 惰性恢复阈值（> LLM 最坏 95s，留足裕量）

_executor = None


def get_executor():
    """懒初始化（首次提交时建池；max_workers=2 控制并发 LLM 调用与连接占用）。"""
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='schedule-capture')
    return _executor


def submit_capture(app, capture_id):
    """把处理任务投递到线程池；app 必须传真实对象（reloader/worker 均适用）。"""
    app_obj = app._get_current_object() if hasattr(app, '_get_current_object') else app
    get_executor().submit(_worker_entry, app_obj, capture_id)


def _worker_entry(app, capture_id):
    with app.app_context():
        started = time.time()
        try:
            outcome = process_capture(capture_id)
            app.logger.info(f"[schedule_capture] capture={capture_id} 完成 "
                            f"({outcome}) 耗时 {time.time() - started:.1f}s")
        except Exception as exc:                    # 兜底：worker 崩溃也要终态
            app.logger.exception(f"[schedule_capture] capture={capture_id} 处理异常")
            mark_failed(capture_id, '处理出错，请重试', 'internal')
        finally:
            db.session.remove()                     # 线程池线程常驻：必清 session


# ── 状态机推进 ────────────────────────────────────────────────────────────

def create_capture(user_id, text, request_id, input_type='text'):
    """建行（蓝图请求事务 commit）。重复 request_id 幂等返回既有行。"""
    row = ScheduleCapture(user_id=user_id, request_id=request_id or '',
                          input_type=input_type, text=text)
    db.session.add(row)
    try:
        db.session.flush()
        return row, True
    except IntegrityError:
        db.session.rollback()
        existing = ScheduleCapture.query.filter_by(
            user_id=user_id, request_id=request_id).first()
        return existing, False


def claim(capture_id):
    """pending → processing 小事务；被抢先（重复投递/已处理）返回 False。"""
    result = db.session.execute(
        update(ScheduleCapture)
        .where(ScheduleCapture.id == capture_id, ScheduleCapture.status == 'pending')
        .values(status='processing'))
    db.session.commit()
    return result.rowcount == 1


def mark_failed(capture_id, message, code):
    """失败小事务：仅条件更新（processing → failed），无业务写入可回滚。"""
    try:
        db.session.execute(
            update(ScheduleCapture)
            .where(ScheduleCapture.id == capture_id, ScheduleCapture.status == 'processing')
            .values(status='failed', error=(message or '')[:200], error_code=code))
        db.session.commit()
    except Exception:
        db.session.rollback()


def lazy_recover(capture_id, now=None):
    """GET 轮询时顺带恢复卡死行（worker 进程被杀/排队过久）。条件更新幂等。"""
    now = now or datetime.now()
    threshold = now - STALE_AFTER
    try:
        db.session.execute(
            update(ScheduleCapture)
            .where(ScheduleCapture.id == capture_id,
                   ScheduleCapture.status.in_(('pending', 'processing')),
                   ScheduleCapture.updated_at < threshold)
            .values(status='failed', error='处理超时，请重试', error_code='stale_timeout'))
        db.session.commit()
    except Exception:
        db.session.rollback()


# ── 处理主体 ──────────────────────────────────────────────────────────────

def process_capture(capture_id, *, llm=None, now=None):
    """worker 主体（也可测试直调）。返回终态字符串。"""
    now = now or datetime.now()
    capture = db.session.get(ScheduleCapture, capture_id)
    if capture is None:
        return 'gone'
    if not claim(capture_id):
        return 'skipped'
    db.session.expire_all()

    # ── LLM 调用（无事务） ──
    try:
        parsed = intent_mod.parse_capture(capture.text, now=now, llm=llm)
    except intent_mod.IntentError as exc:
        mark_failed(capture_id, str(exc), exc.code)
        return 'failed'
    except Exception:
        mark_failed(capture_id, '处理出错，请重试', 'internal')
        return 'failed'

    # ── 最终大事务 ──
    try:
        return _finalize(capture_id, parsed, now)
    except Exception:
        db.session.rollback()
        mark_failed(capture_id, '处理出错，请重试', 'internal')
        return 'failed'


def _finalize(capture_id, parsed, now):
    """最终大事务：锁 profile → 落库 → 排程 → 条件转移终态。"""
    capture = db.session.get(ScheduleCapture, capture_id)
    if capture is None:
        db.session.rollback()
        return 'gone'
    user_id = capture.user_id
    profile = lock_profile(user_id)                  # 锁序：先 profile

    plan = planner.create_plan(user_id, trigger='capture', capture_id=capture_id)
    items_out, ready_tasks, created_events = [], [], []

    for item in parsed['items']:
        out = {'index': item['index'], 'kind': item['kind'], 'title': item['title'],
               'evidence': item['evidence'], 'status': item['status'],
               'fields': _fields_dict(item), 'duration_source': item.get('duration_source', 'user'),
               'ambiguities': item['ambiguities'], 'answers': {},
               'result': None}
        if item['status'] == 'ready':
            try:
                out.update(_create_item(user_id, item, plan))
                if item['kind'] == 'task':
                    ready_tasks.append(db.session.get(ScheduleTask, out['result']['task_id']))
                elif item['kind'] == 'event':
                    created_events.append(db.session.get(ScheduleEvent, out['result']['event_id']))
            except ValueError:
                out['status'] = 'failed'
                out['result'] = {'message': '字段校验未通过，请手动创建'}
        items_out.append(out)

    # 排程（suggest：同 plan 应用；manual：另出 proposed 方案）
    scheduling = None
    if planner.planner_enabled() and ready_tasks:
        scheduling = planner.auto_schedule(
            user_id, ready_tasks, profile=profile, capture_id=capture_id,
            plan=plan if profile.automation_mode == 'suggest' else None, now=now)
    _annotate_scheduling(items_out, scheduling)

    # 事件可能压到既有 AI 未锁定块（本次排程已避开，但历史块未动）→ 让位重排
    extra_plan_ids = []
    for event_row in created_events:
        if planner.planner_enabled():
            outcome = planner.replan_conflicts(user_id, event_row, profile=profile, now=now)
            if outcome and outcome.get('plan'):
                extra_plan_ids.append(outcome['plan'].id)

    # 主 plan 终态：有创建即 applied（manual 的排程提案是独立 plan）
    plan_ids = list(extra_plan_ids)                # 重排 plan 也入列，供「撤销本次」整链补偿
    if scheduling and scheduling.get('plan') and profile.automation_mode == 'manual':
        plan_ids.append(scheduling['plan'].id)
    if any(o.get('result') for o in items_out) or (scheduling and scheduling.get('plan')):
        plan.status = 'applied'
        plan.applied_at = now
        plan.reason = ((scheduling or {}).get('reason') or '')[:500] or '已记录'
        if not plan.diff_json:
            plan.diff_json = json.dumps({'ops': []}, ensure_ascii=False)
        plan_ids.insert(0, plan.id)
    else:
        plan.status = 'expired'
        db.session.flush()

    # 只要有待补充项就保持 clarify_needed（混合场景分区展示，resolve 保持可用）
    terminal = 'clarify_needed' if any(
        o['status'] == 'needs_clarification' for o in items_out) else 'done'

    payload = json.dumps({'version': 1, 'now': now.strftime('%Y-%m-%d %H:%M'),
                          'items': items_out, 'unparsed': parsed['unparsed'],
                          'plan_ids': plan_ids}, ensure_ascii=False)

    # 条件转移终态：被惰性恢复抢先 → rowcount=0 → 整体放弃（不产生重复任务）
    result = db.session.execute(
        update(ScheduleCapture)
        .where(ScheduleCapture.id == capture_id, ScheduleCapture.status == 'processing')
        .values(status=terminal, items_json=payload))
    if result.rowcount == 0:
        db.session.rollback()
        return 'abandoned'
    db.session.commit()
    return terminal


def _annotate_scheduling(items, scheduling):
    """把排程结果回填到事项回执：已安排时段写进 message、状态升 scheduled、
    容量缺口如实标注（§2「还缺 N 分钟」口径）。"""
    if not scheduling:
        return
    if scheduling.get('mode') == 'applied':
        by_task = {}
        for op in scheduling.get('ops') or []:
            if op['op'] == 'create_block':
                by_task.setdefault(op['task_id'], []).append((op['start_at'], op['end_at']))
        for out in items:
            result = out.get('result')
            if not (isinstance(result, dict) and result.get('task_id') in by_task):
                continue
            spans = by_task[result['task_id']]
            desc = '、'.join(f"{s.strftime('%m-%d %H:%M')}–{e.strftime('%H:%M')}"
                            for s, e in spans)
            result['message'] = f"已安排 {desc}"
            out['status'] = 'scheduled'
    for u in scheduling.get('unscheduled') or []:
        for out in items:
            result = out.get('result')
            if isinstance(result, dict) and result.get('task_id') == u['task_id']:
                result['unscheduled_minutes'] = u['minutes']
                result['message'] += f"；还缺 {u['minutes']} 分钟容量不足"


def _fields_dict(item):
    f = item['fields']
    out = {}
    for key in ('start_at', 'end_at', 'due_at'):
        out[key] = f[key].strftime('%Y-%m-%d %H:%M') if f.get(key) else None
    out['due_date'] = f['due_date'].strftime('%Y-%m-%d') if f.get('due_date') else None
    out['deadline_precision'] = f.get('deadline_precision')
    out['duration_minutes'] = f.get('duration_minutes')
    out['priority'] = f.get('priority')
    out['location'] = f.get('location')
    out['reminder_minutes'] = f.get('reminder_minutes')
    return out


def _create_item(user_id, item, plan):
    """ready 事项落库（source='ai'）+ 记变更账 + 生成回执 message。"""
    f = item['fields']
    if item['kind'] == 'event':
        data = create_event(user_id, {
            'title': item['title'], 'location': f['location'],
            'start_at': f['start_at'].strftime('%Y-%m-%d %H:%M'),
            'end_at': f['end_at'].strftime('%Y-%m-%d %H:%M'),
            'reminder_minutes': f['reminder_minutes']}, source='ai')
        event = data['event']
        record_change_dict(user_id, plan.id, 'event', event['id'], 'create',
                           {'entity_version': event['version'], 'title': event['title'],
                            'status': 'active'})
        when = f"{f['start_at'].strftime('%m-%d %H:%M')}–{f['end_at'].strftime('%H:%M')}"
        return {'status': 'scheduled',
                'result': {'task_id': None, 'event_id': event['id'], 'block_ids': [],
                           'plan_id': plan.id, 'message': f'已创建日程：{when}'}}
    data = create_task(user_id, {
        'title': item['title'],
        'deadline_precision': f['deadline_precision'],
        'due_at': f['due_at'].strftime('%Y-%m-%d %H:%M') if f['due_at'] else None,
        'due_date': f['due_date'].strftime('%Y-%m-%d') if f['due_date'] else None,
        'estimated_minutes': f['duration_minutes'],
        'priority': f['priority'],
        'reminder_minutes': f['reminder_minutes']}, source='ai')
    task = data['task']
    record_change_dict(user_id, plan.id, 'task', task['id'], 'create',
                       {'entity_version': task['version'], 'title': task['title'],
                        'status': 'open'})
    note = ''
    if item.get('duration_source') == 'default_30':
        note = '（暂按 30 分钟估时）'
    due_note = f"，截止 {f['due_at'].strftime('%m-%d %H:%M') if f['due_at'] else f['due_date']}" \
        if (f['due_at'] or f['due_date']) else ''
    return {'status': 'created',
            'result': {'task_id': task['id'], 'event_id': None, 'block_ids': [],
                       'plan_id': plan.id, 'message': f'已创建任务{due_note}{note}'}}


# ── resolve（歧义补答，蓝图请求事务内同步执行） ────────────────────────────

def resolve_capture(user_id, capture_id, answers, *, now=None):
    """回答歧义后继续落库（不再调 LLM）。锁序：先 profile 再 capture 行。
    返回更新后的 capture ORM 对象；状态不符 raise VersionConflict。"""
    now = now or datetime.now()
    profile = lock_profile(user_id)                  # 锁序：先 profile
    capture = (db.session.query(ScheduleCapture)
               .filter(ScheduleCapture.id == capture_id, ScheduleCapture.user_id == user_id)
               .with_for_update().populate_existing().first())
    if capture is None:
        raise NotFound("录入不存在")
    if capture.status != 'clarify_needed':
        raise VersionConflict("该录入不在待补充状态")

    data = json.loads(capture.items_json or '{"items":[]}')
    plan = planner.create_plan(user_id, trigger='capture', capture_id=capture.id,
                               reason='补充信息后继续')
    resolved_tasks, resolved_events = [], []
    for out in data.get('items', []):
        if out.get('status') != 'needs_clarification':
            continue
        answer = answers.get(str(out['index'])) or answers.get(out['index'])
        if not answer:
            continue
        out['answers'] = answer
        item = intent_mod.resolve_item(out, answer, now=now)
        out['ambiguities'] = item['ambiguities']
        out['fields'] = _fields_dict(item)
        if item['status'] == 'ready':
            try:
                out.update(_create_item(user_id, item, plan))
                if item['kind'] == 'task':
                    resolved_tasks.append(db.session.get(ScheduleTask, out['result']['task_id']))
                elif item['kind'] == 'event':
                    resolved_events.append(db.session.get(ScheduleEvent, out['result']['event_id']))
            except ValueError:
                out['status'] = 'failed'
                out['result'] = {'message': '字段校验未通过，请手动创建'}

    scheduling = None
    if resolved_tasks and planner.planner_enabled():
        scheduling = planner.auto_schedule(
            user_id, resolved_tasks, profile=profile, capture_id=capture.id,
            plan=plan if profile.automation_mode == 'suggest' else None, now=now)
    _annotate_scheduling(data.get('items', []), scheduling)

    # 补答创建的固定日程可能与首次录入已排的块重叠 → 触发有限重排让位
    # （与蓝图 POST /events 挂钩同口径；重排 plan 计入 plan_ids 供整次撤销）
    for event_row in resolved_events:
        if planner.planner_enabled():
            outcome = planner.replan_conflicts(user_id, event_row, profile=profile, now=now)
            if outcome and outcome.get('plan'):
                data.setdefault('plan_ids', []).append(outcome['plan'].id)

    if scheduling and scheduling.get('plan') and profile.automation_mode == 'manual':
        data.setdefault('plan_ids', []).append(scheduling['plan'].id)
    has_result = any(o.get('result') for o in data.get('items', []))
    if has_result or resolved_tasks:
        plan.status = 'applied'
        plan.applied_at = now
        plan.reason = '补充信息后创建'
        if not plan.diff_json:
            plan.diff_json = json.dumps({'ops': []}, ensure_ascii=False)
        data.setdefault('plan_ids', []).insert(0, plan.id)
    else:
        plan.status = 'expired'
        db.session.flush()

    still_clarify = any(o.get('status') == 'needs_clarification' for o in data.get('items', []))
    capture.status = 'clarify_needed' if still_clarify else 'done'
    capture.items_json = json.dumps(data, ensure_ascii=False)
    return capture

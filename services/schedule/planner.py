"""排程器：确定性规则贪心（规划文档 §5.5），零 LLM——模型负责理解与解释，
规则负责时间表。锁序规范见包 docstring：调用本模块任何入口前，事务必须已持
该用户 profile 行锁（preferences.lock_profile）。

忙闲谓词与 calendar 冲突检测同源（busy_events_in_window / planned_blocks_
in_window 单点）；detect_conflicts 只作测试后验断言，不进放置决策。

硬约束（§5.3，违反即为 bug）：自动块不与任何忙区重叠、不越硬截止、不早于
now、≥MIN_BLOCK、日 AI 占用 ≤ DAY_CAP×(日窗−事件占用)；锁定块/固定日程/
稳定窗（now+60min）内的块永不被移动。缺口永不伪造——放不下如实报
unscheduled，绝不重叠塞入制造可行假象（§2 端到端示例的「还缺 1 小时」）。
"""
import json
from datetime import datetime, time, timedelta

from exts import db
from models import ScheduleActivity, ScheduleBlock, ScheduleChange, ScheduleEvent, SchedulePlan, ScheduleTask

from . import NotFound, VersionConflict
from .calendar import (_cancel_future_blocks, _fetch_locked, _log,
                       busy_events_in_window, planned_blocks_in_window)
from .preferences import parse_hhmm
from .reminders import _task_due_anchor, cancel_target, rematerialize_target

# ── 可调常量（current_app.config 里同名键可覆盖） ─────────────────────────
HORIZON_DAYS = 7          # 规划窗口（天）
MIN_BLOCK = 30            # 最小执行块（分钟）
MAX_BLOCK = 120           # 单块上限（可拆分任务的分块粒度）
BUFFER_MIN = 10           # AI 块与相邻忙区/块的缓冲
DAY_CAP = 0.8             # 日 AI 占用上限 = 0.8 × (日窗 − 事件占用)
GRID_MIN = 15             # 排程粒度（分钟对齐）
STABILITY_MIN = 60        # 未来 60 分钟内的既有安排保持稳定不被移动
MAX_MOVES_PER_TASK_PER_DAY = 2   # 每任务每日自动移动上限（§5.6）
DEFAULT_DURATION = 30     # 无时长任务的默认安排（§4.3 可配置产品默认）
MAX_MOVED_TASKS_AUTO = 3  # 单次重排影响任务数超过此值只出方案不自动应用


def _cfg(name, default):
    from flask import current_app
    try:
        return current_app.config.get(name, default)
    except RuntimeError:
        return default


def _align15_up(dt):
    """向上对齐到 15 分钟粒度。"""
    dt = dt.replace(second=0, microsecond=0)
    if dt.minute % GRID_MIN:
        dt += timedelta(minutes=GRID_MIN - dt.minute % GRID_MIN)
    return dt


def _floor15(minutes):
    return int(minutes // GRID_MIN) * GRID_MIN


def planner_enabled():
    return _cfg('SCHEDULE_PLANNER_ENABLED', True)


# ── 忙闲图 ────────────────────────────────────────────────────────────────

class BusyMap:
    """未来 N 天的逐日忙闲索引（内存结构，构建后纯计算）。"""

    def __init__(self, profile, window_days, now):
        self.day_start = parse_hhmm(profile.day_start_time, '日窗口起')
        self.day_end = parse_hhmm(profile.day_end_time, '日窗口止')
        self.window_days = window_days           # [date, ...]
        self.now = now
        self.spans = {}                          # date -> [(s,e)] 合并后的忙区间
        self.ai_minutes = {}                     # date -> planned 块分钟
        self.event_minutes = {}                  # date -> busy 事件分钟

    def build(self, user_id, exclude_blocks=None):
        """exclude_blocks：重排场景把将要移动的块排除在图外（它们要让位）。
        不能建图后做区间减法——victim 区间可能与邻居合并，减法会把邻居一起抹掉。"""
        first = datetime.combine(self.window_days[0], time(0, 0))
        last_day = self.window_days[-1]
        last = datetime.combine(last_day + timedelta(days=1), time(0, 0))
        exclude = exclude_blocks or frozenset()
        for day in self.window_days:
            self.spans[day] = []
            self.ai_minutes[day] = 0
            self.event_minutes[day] = 0
        for e in busy_events_in_window(user_id, first, last):
            self._add_span(e.start_at, e.end_at, 'event')
        for b in planned_blocks_in_window(user_id, first, last):
            if b.id not in exclude:
                self._add_span(b.start_at, b.end_at, 'block')
        for day in self.window_days:
            self.spans[day] = _merge(self.spans[day])
        return self

    def _add_span(self, s, t, kind):
        """逐日切分计入（跨天区间按各日窗口裁剪，跨天部分不丢）。"""
        day = _date_of(s)
        while day <= _date_of(t):
            if day in self.spans:
                ds = datetime.combine(day, self.day_start)
                de = datetime.combine(day, self.day_end)
                cs, ce = max(s, ds), min(t, de)
                if ce > cs:
                    self.spans[day].append((cs, ce))
                    minutes = (ce - cs).total_seconds() / 60
                    if kind == 'event':
                        self.event_minutes[day] += minutes
                    else:
                        self.ai_minutes[day] += minutes
            day += timedelta(days=1)

    def add_block(self, day, s, t, with_buffer):
        """放置新块后写回（带缓冲写回：后续放置自动避开缓冲带）。"""
        lo = s - timedelta(minutes=BUFFER_MIN) if with_buffer else s
        hi = t + timedelta(minutes=BUFFER_MIN) if with_buffer else t
        self.spans[day].append((lo, hi))
        self.spans[day] = _merge(self.spans[day])
        self.ai_minutes[day] += (t - s).total_seconds() / 60

    def free_gaps(self, day, *, for_ai=True):
        """日窗口内空档。for_ai=True 时各忙区间向两侧扩 BUFFER（贴日窗边缘
        不扩——窗口外本就不可用），保证 AI 块与任何邻居间隔 ≥10 分钟。"""
        ds = datetime.combine(day, self.day_start)
        de = datetime.combine(day, self.day_end)
        pad = timedelta(minutes=BUFFER_MIN) if for_ai else timedelta()
        expanded = []
        for (s, e) in self.spans.get(day, []):
            expanded.append((max(ds, s - pad), min(de, e + pad)))
        return _gaps(_merge(expanded), ds, de)

    def day_cap_minutes(self, day):
        """当日 AI 块总分钟上限 = DAY_CAP × (日窗 − 事件占用)。"""
        window_min = (datetime.combine(day, self.day_end)
                      - datetime.combine(day, self.day_start)).total_seconds() / 60
        return DAY_CAP * max(0.0, window_min - self.event_minutes[day])


def _date_of(dt):
    return dt.date()


def _merge(spans):
    """合并重叠区间（输入乱序可）。"""
    result = []
    for s, e in sorted(spans):
        if result and s <= result[-1][1]:
            result[-1] = (result[-1][0], max(result[-1][1], e))
        else:
            result.append((s, e))
    return result


def _gaps(merged, lo, hi):
    """[lo,hi] 对 merged 的补集。"""
    gaps, cur = [], lo
    for s, e in merged:
        if s > cur:
            gaps.append((cur, s))
        cur = max(cur, e)
    if cur < hi:
        gaps.append((cur, hi))
    return [(s, e) for s, e in gaps if (e - s).total_seconds() / 60 >= MIN_BLOCK]


# ── 方案生成 ──────────────────────────────────────────────────────────────

def _sort_key(task, profile):
    anchor = _task_due_anchor(task, profile)
    priority_rank = {'high': 0, 'medium': 1, 'low': 2}[task.priority or 'medium']
    return (anchor or datetime.max, priority_rank, task.id)


def generate_ops(user_id, tasks, *, profile, now=None):
    """为任务集生成执行块放置方案（纯计算，不落库不写块）。

    tasks 须已复核 status='open'。返回
    {'ops', 'unscheduled': [{task_id,title,minutes,reason}], 'reason'}。
    无截止任务默认不排（§4.3「仅记录意图」——待安排清单自行安排）。"""
    now = now or datetime.now()
    busy = BusyMap(profile, _window_days(now), now).build(user_id)
    ops, unscheduled = [], []
    total_placed = 0

    for task in sorted(tasks, key=lambda t: _sort_key(t, profile)):
        remaining = task.remaining_minutes
        if remaining is None:
            remaining = task.estimated_minutes
        if remaining is None:
            remaining = DEFAULT_DURATION
        deadline = _task_due_anchor(task, profile)
        if deadline is None:
            continue                          # 无截止：不自动排，留待安排清单
        if deadline <= now:
            unscheduled.append({'task_id': task.id, 'title': task.title,
                                'minutes': int(remaining), 'reason': 'overdue'})
            continue

        placed_any = False
        if not task.splittable and remaining > 2 * MAX_BLOCK:
            # 不可拆分且单块超上限（§5.5 步骤 4：符合最小长度的块才允许）：
            # 不硬塞 5 小时巨块，如实记缺口
            unscheduled.append({'task_id': task.id, 'title': task.title,
                                'minutes': int(remaining), 'reason': 'unsplittable_too_long'})
            continue
        for day in busy.window_days:
            if datetime.combine(day, busy.day_start) >= deadline:
                break
            cap_left = _floor15(busy.day_cap_minutes(day) - busy.ai_minutes[day])
            if cap_left < MIN_BLOCK:
                continue
            for gs, ge in busy.free_gaps(day):
                lo = _align15_up(max(gs, now if day == now.date() else gs))
                while remaining > 0:
                    hi_limit = min(ge, deadline)
                    want = min(remaining, MAX_BLOCK) if task.splittable else remaining
                    want = min(want, (hi_limit - lo).total_seconds() / 60, cap_left)
                    want = _floor15(want)
                    if want < MIN_BLOCK:
                        break
                    start, end = lo, lo + timedelta(minutes=want)
                    ops.append({'op': 'create_block', 'task_id': task.id,
                                'start_at': start, 'end_at': end, '_gap_end': ge})
                    busy.add_block(day, start, end, with_buffer=True)
                    remaining -= want
                    total_placed += want
                    placed_any = True
                    cap_left -= want
                    if remaining < MIN_BLOCK:
                        break
                    # 同一空档内连续放置：块间留缓冲（free_gaps 快照不含新块）
                    lo = _align15_up(end + timedelta(minutes=BUFFER_MIN))
                if remaining <= 0:
                    break
            if remaining <= 0:
                break

        # 尾料 < MIN_BLOCK 且当日上一块同任务：直接并入（避免碎片）；
        # 并入不得越出原空档（gap 边缘已含对邻居的缓冲，不能吃掉）
        if 0 < remaining < MIN_BLOCK and ops:
            last = ops[-1]
            if (last['op'] == 'create_block' and last['task_id'] == task.id
                    and last.get('_gap_end')
                    and last['end_at'] + timedelta(minutes=remaining) <= last['_gap_end']
                    and (last['end_at'] - last['start_at']).total_seconds() / 60 + remaining <= MAX_BLOCK):
                last['end_at'] = last['end_at'] + timedelta(minutes=remaining)
                remaining = 0

        if remaining > 0:
            unscheduled.append({'task_id': task.id, 'title': task.title,
                                'minutes': int(remaining),
                                'reason': 'capacity' if placed_any else 'no_fit'})

    reason_bits = []
    if total_placed:
        reason_bits.append(f"已安排 {int(total_placed)} 分钟执行时间")
    if unscheduled:
        reason_bits.append(f"{len(unscheduled)} 个任务容量不足（共缺 "
                           f"{sum(u['minutes'] for u in unscheduled)} 分钟）")
    for op in ops:                              # 剥离内部标记
        op.pop('_gap_end', None)
    return {'ops': ops, 'unscheduled': unscheduled,
            'reason': '；'.join(reason_bits) or '无需安排'}


def _window_days(now):
    return [now.date() + timedelta(days=i) for i in range(HORIZON_DAYS)]


# ── 方案落库 / 应用 / 撤销 ────────────────────────────────────────────────

def _dt_str(dt):
    return dt.strftime('%Y-%m-%d %H:%M') if dt else None


def _op_baselines(ops):
    """ops 快照序列化（含逐实体版本基线，manual 模式 apply 复验用）。"""
    return json.dumps({'ops': [
        {**op, 'start_at': _dt_str(op.get('start_at')), 'end_at': _dt_str(op.get('end_at')),
         'task_version': op.get('task_version'), 'block_version': op.get('block_version')}
        for op in ops]}, ensure_ascii=False)


def record_change(user_id, plan_id, entity, obj, operation, before=None):
    """写方案变更账（撤销补偿依据）。after 必含 entity_version。"""
    return record_change_dict(user_id, plan_id, entity, obj.id, operation,
                              {'entity_version': obj.version, **_entity_snapshot(entity, obj)}, before)


def _entity_snapshot(entity, obj):
    if entity == 'block':
        return {'start_at': _dt_str(obj.start_at), 'end_at': _dt_str(obj.end_at),
                'status': obj.status, 'task_id': obj.task_id}
    if entity == 'task':
        return {'title': obj.title, 'status': obj.status}
    return {'title': obj.title, 'status': obj.status}


def record_change_dict(user_id, plan_id, entity, entity_id, operation, after, before=None):
    """字典版变更记录（调用方只有 to_dict 快照时用，after 须自带 entity_version）。"""
    db.session.add(ScheduleChange(
        user_id=user_id, plan_id=plan_id, entity=entity, entity_id=entity_id,
        operation=operation,
        before_json=json.dumps(before, ensure_ascii=False) if before else None,
        after_json=json.dumps(after, ensure_ascii=False)))
    db.session.flush()


def create_plan(user_id, *, trigger, capture_id=None, reason=''):
    plan = SchedulePlan(user_id=user_id, trigger=trigger, capture_id=capture_id,
                        reason=(reason or '')[:500])
    db.session.add(plan)
    db.session.flush()
    return plan


def apply_ops(user_id, plan, ops, *, now=None):
    """按 cancel → create → move 顺序执行 ops 并写变更账。跳过失效 op
    （目标已死/已锁定/稳定窗内），返回执行数。调用方事务内。"""
    now = now or datetime.now()
    executed = 0
    stability = now + timedelta(minutes=STABILITY_MIN)

    def _op_key(op):
        return {'cancel_block': 0, 'create_block': 1, 'move_block': 2}.get(op['op'], 3)

    for op in sorted(ops, key=_op_key):
        if op['op'] == 'create_block':
            task = ScheduleTask.query.filter(
                ScheduleTask.id == op['task_id'],
                ScheduleTask.owner_id == user_id,
                ScheduleTask.status == 'open').first()
            if task is None:
                continue
            block = ScheduleBlock(owner_id=user_id, task_id=task.id,
                                  start_at=op['start_at'], end_at=op['end_at'],
                                  locked=False)          # AI 块默认不锁定，可被重排
            db.session.add(block)
            db.session.flush()
            rematerialize_target('block', block)
            _log(user_id, 'block_create', task_id=task.id, block_id=block.id,
                 source='plan', note='自动排程')
            record_change(user_id, plan.id, 'block', block, 'create')
        elif op['op'] == 'cancel_block':
            block = _fetch_locked(ScheduleBlock, user_id, op['block_id'])
            if block.status != 'planned':
                continue
            before = {'start_at': _dt_str(block.start_at), 'end_at': _dt_str(block.end_at)}
            block.status = 'cancelled'
            block.version += 1
            cancel_target('block', block.id)
            _log(user_id, 'block_cancel', task_id=block.task_id, block_id=block.id,
                 source='plan', note='重排腾位')
            record_change(user_id, plan.id, 'block', block, 'cancel', before)
        elif op['op'] == 'move_block':
            block = _fetch_locked(ScheduleBlock, user_id, op['block_id'])
            if block.status != 'planned' or block.locked or block.start_at < stability:
                continue                          # 锁定/稳定窗内：硬约束，宁重叠不动
            before = {'start_at': _dt_str(block.start_at), 'end_at': _dt_str(block.end_at)}
            block.start_at, block.end_at = op['start_at'], op['end_at']
            block.version += 1
            rematerialize_target('block', block)
            _log(user_id, 'block_update', task_id=block.task_id, block_id=block.id,
                 source='plan', note='重排移动')
            record_change(user_id, plan.id, 'block', block, 'update', before)
        executed += 1
    return executed


def save_and_apply(user_id, ops, *, trigger, capture_id=None, reason='', now=None):
    """suggest 模式：生成即应用（同事务，版本不可能漂移）。plan 仍落库可撤销。"""
    now = now or datetime.now()
    plan = create_plan(user_id, trigger=trigger, capture_id=capture_id, reason=reason)
    plan.diff_json = _op_baselines(ops)
    apply_ops(user_id, plan, ops, now=now)
    plan.status = 'applied'
    plan.applied_at = now
    db.session.flush()
    return plan


def save_proposed(user_id, ops, *, trigger, capture_id=None, reason=''):
    """manual 模式：只落 proposed 方案，等用户 POST apply。"""
    plan = create_plan(user_id, trigger=trigger, capture_id=capture_id, reason=reason)
    plan.diff_json = _op_baselines(ops)
    db.session.flush()
    return plan


def apply_proposed(user_id, plan_id, *, now=None):
    """manual 方案的应用：逐实体复验生成时的版本基线，任一不符 409（整单拒绝，
    重新生成比部分应用便宜——apply 是前瞻动作）。"""
    plan = (db.session.query(SchedulePlan)
            .filter(SchedulePlan.id == plan_id, SchedulePlan.user_id == user_id)
            .with_for_update().populate_existing().first())
    if plan is None:
        raise NotFound("方案不存在")
    if plan.status != 'proposed':
        raise VersionConflict("方案已应用、过期或已撤销")
    now = now or datetime.now()
    ops = _ops_with_baselines(plan)
    for op in ops:                              # 复验基线
        if op['op'] == 'create_block':
            task = ScheduleTask.query.filter_by(id=op['task_id'], owner_id=user_id).first()
            if task is None or task.version != op.get('task_version'):
                raise VersionConflict("任务已变化，请重新生成方案")
        elif op['op'] in ('cancel_block', 'move_block'):
            block = ScheduleBlock.query.filter_by(id=op['block_id'], owner_id=user_id).first()
            if block is None or block.version != op.get('block_version'):
                raise VersionConflict("时间安排已变化，请重新生成方案")
    executed = apply_ops(user_id, plan, ops, now=now)
    plan.status = 'applied'
    plan.applied_at = now
    db.session.flush()
    return {'plan': plan, 'changes_n': executed}


def _ops_with_baselines(plan):
    """生成期把版本基线嵌入 ops（save_proposed 时补齐）。"""
    data = json.loads(plan.diff_json or '{"ops": []}')
    ops = []
    for op in data.get('ops', []):
        op = dict(op)
        if 'start_at' in op and isinstance(op['start_at'], str):
            op['start_at'] = datetime.strptime(op['start_at'], '%Y-%m-%d %H:%M')
        if 'end_at' in op and isinstance(op['end_at'], str):
            op['end_at'] = datetime.strptime(op['end_at'], '%Y-%m-%d %H:%M')
        ops.append(op)
    return ops


def embed_baselines(user_id, ops):
    """生成后立即补版本基线（save_proposed 前调用一次）。"""
    for op in ops:
        if op['op'] == 'create_block':
            task = ScheduleTask.query.filter_by(id=op['task_id'], owner_id=user_id).first()
            op['task_version'] = task.version if task else -1
        elif op['op'] in ('cancel_block', 'move_block'):
            block = ScheduleBlock.query.filter_by(id=op['block_id'], owner_id=user_id).first()
            op['block_version'] = block.version if block else -1
    return ops


def revert_plan(user_id, plan_id, *, now=None):
    """撤销方案：补偿而非回滚。changes 按 id DESC 严格逆序；对象版本与
    change.after_json.entity_version 不符（我之后有人动过）或对象已死 → 该项
    skipped 列明原因，绝不毁掉后续合法编辑。"""
    now = now or datetime.now()
    plan = (db.session.query(SchedulePlan)
            .filter(SchedulePlan.id == plan_id, SchedulePlan.user_id == user_id)
            .with_for_update().populate_existing().first())
    if plan is None:
        raise NotFound("方案不存在")
    if plan.status != 'applied':
        raise VersionConflict("仅已应用的方案可以撤销")

    changes = (ScheduleChange.query.filter(ScheduleChange.plan_id == plan.id)
               .order_by(ScheduleChange.id.desc()).all())
    reverted, skipped = [], []
    for ch in changes:
        after = json.loads(ch.after_json or '{}')
        before = json.loads(ch.before_json) if ch.before_json else {}
        model = {'task': ScheduleTask, 'event': ScheduleEvent, 'block': ScheduleBlock}[ch.entity]
        obj = db.session.get(model, ch.entity_id)
        if obj is None or getattr(obj, 'owner_id', None) != user_id:
            skipped.append({'entity': ch.entity, 'entity_id': ch.entity_id, 'reason': '对象已删除'})
            continue
        if obj.version != after.get('entity_version'):
            skipped.append({'entity': ch.entity, 'entity_id': ch.entity_id, 'reason': '对象已被修改'})
            continue
        if ch.entity == 'task':
            if obj.status != 'open':
                skipped.append({'entity': 'task', 'entity_id': ch.entity_id, 'reason': '任务已完结'})
                continue
            if ch.operation == 'create':
                obj.status = 'cancelled'
                obj.version += 1
                _cancel_future_blocks(obj, now)
                cancel_target('task', obj.id)
                _log(user_id, 'task_cancel', task_id=obj.id, source='plan', note='撤销录入')
        elif ch.entity == 'event':
            if obj.status != 'active':
                skipped.append({'entity': 'event', 'entity_id': ch.entity_id, 'reason': '日程已删除'})
                continue
            if ch.operation == 'create':
                obj.status = 'cancelled'
                obj.version += 1
                cancel_target('event', obj.id)
                _log(user_id, 'event_cancel', source='plan', note='撤销录入')
        else:                                   # block
            if ch.operation == 'create':
                if obj.status != 'planned':
                    skipped.append({'entity': 'block', 'entity_id': ch.entity_id, 'reason': '块已结束或取消'})
                    continue
                obj.status = 'cancelled'
                obj.version += 1
                cancel_target('block', obj.id)
                _log(user_id, 'block_cancel', task_id=obj.task_id, block_id=obj.id,
                     source='plan', note='撤销自动排程')
            elif ch.operation == 'update':
                if obj.status != 'planned':
                    skipped.append({'entity': 'block', 'entity_id': ch.entity_id, 'reason': '块已结束或取消'})
                    continue
                obj.start_at = datetime.strptime(before['start_at'], '%Y-%m-%d %H:%M')
                obj.end_at = datetime.strptime(before['end_at'], '%Y-%m-%d %H:%M')
                obj.version += 1
                rematerialize_target('block', obj)
                _log(user_id, 'block_update', task_id=obj.task_id, block_id=obj.id,
                     source='plan', note='撤销自动移动')
            elif ch.operation == 'cancel':
                obj.status = 'planned'
                obj.start_at = datetime.strptime(before['start_at'], '%Y-%m-%d %H:%M')
                obj.end_at = datetime.strptime(before['end_at'], '%Y-%m-%d %H:%M')
                obj.version += 1
                rematerialize_target('block', obj)
                _log(user_id, 'block_update', task_id=obj.task_id, block_id=obj.id,
                     source='plan', note='撤销重排取消，恢复原位')
        reverted.append({'entity': ch.entity, 'entity_id': ch.entity_id, 'operation': ch.operation})

    plan.status = 'reverted'
    plan.reverted_at = now
    db.session.flush()
    return {'reverted': reverted, 'skipped': skipped}


def expire_proposed(user_id, plan_id):
    """未应用的 proposed 方案作废（撤销本次录入时顺带清理关联提案）。"""
    plan = (db.session.query(SchedulePlan)
            .filter(SchedulePlan.id == plan_id, SchedulePlan.user_id == user_id,
                    SchedulePlan.status == 'proposed')
            .with_for_update().populate_existing().first())
    if plan is not None:
        plan.status = 'expired'
        db.session.flush()


# ── 事件触发的有限重排 ────────────────────────────────────────────────────

def _moves_today(user_id, task_id, now):
    return ScheduleActivity.query.filter(
        ScheduleActivity.user_id == user_id,
        ScheduleActivity.task_id == task_id,
        ScheduleActivity.action == 'block_update',
        ScheduleActivity.source == 'plan',
        ScheduleActivity.occurred_at >= datetime.combine(now.date(), time(0, 0)),
        ScheduleActivity.occurred_at < datetime.combine(now.date() + timedelta(days=1), time(0, 0)),
    ).count()


def replan_conflicts(user_id, event, *, profile, now=None):
    """固定日程新建/变更后，与它冲突的 AI 未锁定块让位（§5.6）：
    稳定窗内/锁定/超出每任务每日移动上限的块不动（保持重叠，conflicts 标红）；
    影响任务 >MAX_MOVED_TASKS_AUTO 只出 proposed 方案不自动应用。"""
    now = now or datetime.now()
    stability = now + timedelta(minutes=STABILITY_MIN)
    victims = [b for b in planned_blocks_in_window(user_id, event.start_at, event.end_at)
               if not b.locked and b.start_at >= stability
               and b.task is not None and b.task.status == 'open'
               and b.start_at < event.end_at and b.end_at > event.start_at]
    if not victims:
        return None

    affected_tasks = {b.task_id for b in victims}
    # 让位块建图时即排除（原位腾出；事件与其它占用保留在图上）
    busy = BusyMap(profile, _window_days(now), now).build(
        user_id, exclude_blocks={b.id for b in victims})

    ops, unsched = [], []
    for block in sorted(victims, key=lambda b: b.start_at):
        if _moves_today(user_id, block.task_id, now) >= MAX_MOVES_PER_TASK_PER_DAY:
            continue                        # 移动上限：保持重叠，交给冲突标红
        duration = (block.end_at - block.start_at).total_seconds() / 60
        deadline = _task_due_anchor(block.task, profile) or datetime.combine(
            busy.window_days[-1], busy.day_end)
        placed = _place_duration(busy, block.task, duration, deadline, now)
        if placed:
            start, end = placed
            ops.append({'op': 'move_block', 'block_id': block.id,
                        'start_at': start, 'end_at': end})
        else:
            ops.append({'op': 'cancel_block', 'block_id': block.id})
            unsched.append(block.task.title)

    if not ops:
        return None
    reason = (f"「{event.title}」占用 {event.start_at.strftime('%m-%d %H:%M')}–"
              f"{event.end_at.strftime('%H:%M')}，已移动 {len(affected_tasks)} 个任务的执行块"
              + (f"；{len(unsched)} 个块无处安放已取消" if unsched else ""))
    if len(affected_tasks) > MAX_MOVED_TASKS_AUTO or profile.automation_mode == 'manual':
        embed_baselines(user_id, ops)
        plan = save_proposed(user_id, ops, trigger='event', reason=reason)
        return {'plan': plan, 'mode': 'proposed', 'ops': ops}
    plan = save_and_apply(user_id, ops, trigger='event', reason=reason, now=now)
    return {'plan': plan, 'mode': 'applied', 'ops': ops}


def _place_duration(busy, task, duration, deadline, now):
    """给一段时长在忙闲图里找最早空位（重排用；不做拆分）。"""
    for day in busy.window_days:
        if datetime.combine(day, busy.day_start) >= deadline:
            break
        cap_left = _floor15(busy.day_cap_minutes(day) - busy.ai_minutes[day])
        if cap_left < MIN_BLOCK:
            continue
        for gs, ge in busy.free_gaps(day):
            lo = _align15_up(max(gs, now if day == now.date() else gs))
            hi_limit = min(ge, deadline)
            want = _floor15(min(duration, (hi_limit - lo).total_seconds() / 60, cap_left))
            if want < min(MIN_BLOCK, duration):
                continue
            start, end = lo, lo + timedelta(minutes=want)
            busy.add_block(day, start, end, with_buffer=True)
            return start, end
    return None


# ── 任务创建后的自动安排（POST /tasks 挂钩 & capture 用） ─────────────────

def auto_schedule(user_id, tasks, *, profile, capture_id=None, plan=None, now=None):
    """按 automation_mode 分流：suggest=生成即应用；manual=落 proposed 方案。
    plan 参数（capture 流程传入）时 suggest 分支复用该 plan——一次录入的
    任务创建与排程同属一个方案，撤销一次全补偿。
    返回 {'plan', 'mode', 'ops', 'unscheduled', 'reason'}；无 ops 时 plan 为 None。"""
    now = now or datetime.now()
    result = generate_ops(user_id, tasks, profile=profile, now=now)
    ops, unscheduled, reason = result['ops'], result['unscheduled'], result['reason']
    if not ops:
        return {'plan': None, 'mode': 'none', 'ops': [], 'unscheduled': unscheduled,
                'reason': reason or '当前无需安排执行时间'}
    if profile.automation_mode == 'suggest':
        if plan is None:
            plan = create_plan(user_id, trigger='capture' if capture_id else 'task',
                               capture_id=capture_id, reason=reason)
        plan.diff_json = _op_baselines(ops)
        apply_ops(user_id, plan, ops, now=now)
        plan.status = 'applied'
        plan.applied_at = now
        db.session.flush()
        return {'plan': plan, 'mode': 'applied', 'ops': ops,
                'unscheduled': unscheduled, 'reason': reason}
    embed_baselines(user_id, ops)
    plan = save_proposed(user_id, ops, trigger='capture' if capture_id else 'task',
                         capture_id=capture_id, reason=reason)
    return {'plan': plan, 'mode': 'proposed', 'ops': ops,
            'unscheduled': unscheduled, 'reason': reason}

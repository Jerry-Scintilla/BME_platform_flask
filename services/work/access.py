"""内部工作台·授权与访问判定（设计方案 §5）。

四层判定链：资格（组织身份）→ 协作授权（WorkAccessGrant）→ 对象关系（事项参与）
→ 动作/状态允许。原则：逐请求实时计算、默认拒绝、不跨请求缓存；
列表/计数/检索必须在查询阶段过滤（filter_items_query），不能先查全量再前端隐藏。

授权与组织身份分离（§3.3）：本模块不写回 UserModel.role，不用职位/sort_rank/
徽标/admin_tag 判权。有效期覆盖任期判断——任职到期即使后台未执行卸任，
以其为来源的授权也立即失效（A05）；membership 来源绑定授权时组快照，
归属行原地改组即失效（A06）。
"""
from datetime import date

from sqlalchemy import and_, or_

from exts import db
from models import (ClubMembership, ClubOfficer, WorkAccessGrant, WorkItem,
                    WorkItemParticipant, WorkWorkspace)

GRANT_ROLES = ('member', 'coordinator', 'governance')
GRANT_SOURCES = ('officer', 'membership', 'direct')


class WorkApiError(Exception):
    """work 域统一业务异常：蓝图 errorhandler 转 {code, message, data: None}。

    404 = 不存在或无权（统一形态防存在性探测）；403 = 已知对象但无此动作权限；
    409 = 状态/版本/幂等冲突；413/415 = 文件超限/类型拒绝。
    """

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def int_or_400(raw, field, default=None):
    """查询参数整数收敛（#5）：非法值统一 400 而不是 ValueError→500。

    raw 为空（None/''）时返回 default；调用方自行夹取上下界。"""
    if raw is None or raw == '':
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise WorkApiError(400, f'{field} 须为整数')


class ItemAccess:
    """一次对象级访问判定结果。

    via ∈ author（草稿/受限事项作者）/ workspace（工作区范围）/ participant（被邀参与）。
    workspace_role 为经工作区授权进入时的岗位（member/coordinator），其余场景为 None。
    """

    __slots__ = ('via', 'workspace_role')

    def __init__(self, via, workspace_role=None):
        self.via = via
        self.workspace_role = workspace_role

    @property
    def is_coordinator(self):
        return self.workspace_role == 'coordinator'

    def to_dict(self):
        return {'via': self.via, 'workspace_role': self.workspace_role}


# ── 资格（组织身份事实，§5.1）──────────────────────────────────

def _active_officer(user, today=None):
    """在任干事行：status=active 且 term_start≤today≤term_end（term_end 空=在任）。
    到期任职不能再提供权限，不等定时卸任任务。"""
    today = today or date.today()
    row = ClubOfficer.query.filter_by(user_id=user.id, status='active').first()
    if row and row.term_start <= today and (row.term_end is None or today <= row.term_end):
        return row
    return None


def eligibility(user):
    """两类资格（§5.1）：有效在任干事；或当前有有效组归属且已被明确开通
    （至少一条有效协作授权）的组内工作人员。资格只表示身份事实，
    不单独放开任何工作区。返回 None=无资格。"""
    officer = _active_officer(user)
    if officer:
        return {'kind': 'officer', 'officer_id': officer.id, 'group_id': officer.group_id}
    mships = ClubMembership.query.filter_by(user_id=user.id).all()
    if mships and grants_for(user):
        return {'kind': 'member', 'group_ids': [m.group_id for m in mships]}
    return None


def participation_eligible(user):
    """参与关系不越过有效协作资格（§12.1）：邀请协作者/转交目标须满足
    在任干事，或（有效组归属 ∧ 至少一条有效授权）。"""
    if _active_officer(user):
        return True
    if not ClubMembership.query.filter_by(user_id=user.id).first():
        return False
    return bool(grants_for(user))


def participation_eligible_bulk(users):
    """批量协作资格判定（语义与 participation_eligible 同源，N+1 修正）。

    一次 in_ 预取任职/归属/授权行，返回 {user_id: bool}；传入可为空，
    缺失用户不在返回映射中（调用方按 False 处理）。"""
    from models import WorkAccessGrant
    today = date.today()
    uids = [u.id for u in users if u is not None]
    if not uids:
        return {}
    officer_uids = set()
    for o in ClubOfficer.query.filter(
            ClubOfficer.user_id.in_(uids), ClubOfficer.status == 'active').all():
        if o.term_start <= today and (o.term_end is None or today <= o.term_end):
            officer_uids.add(o.user_id)
    member_uids = {m.user_id for m in ClubMembership.query.filter(
        ClubMembership.user_id.in_(uids)).all()}
    grant_uids = set()
    for g in WorkAccessGrant.query.filter(
            WorkAccessGrant.user_id.in_(uids),
            WorkAccessGrant.status == 'active').all():
        if g.user_id in officer_uids or g.user_id in grant_uids:
            continue                       # 已判有效（或已计入），跳过重复判定
        if _grant_effective(g):
            grant_uids.add(g.user_id)
    return {uid: (uid in officer_uids) or (uid in member_uids and uid in grant_uids)
            for uid in uids}


def grants_effective_bulk(grants):
    """批量授权有效性判定（语义与 grant_status_reason 同源，N+1 修正）。

    一次 in_ 预取 officer/membership 来源行，返回 {grant.id: bool}。"""
    officer_ids = {g.source_id for g in grants
                   if g.source_type == 'officer' and g.source_id}
    mship_ids = {g.source_id for g in grants
                 if g.source_type == 'membership' and g.source_id}
    officers = {o.id: o for o in ClubOfficer.query.filter(
        ClubOfficer.id.in_(list(officer_ids))).all()} if officer_ids else {}
    mships = {m.id: m for m in ClubMembership.query.filter(
        ClubMembership.id.in_(list(mship_ids))).all()} if mship_ids else {}
    today = date.today()
    out = {}
    for g in grants:
        if g.status != 'active':
            out[g.id] = False
            continue
        if g.valid_from and today < g.valid_from:
            out[g.id] = False
            continue
        if g.valid_until and today > g.valid_until:
            out[g.id] = False
            continue
        if g.source_type == 'officer':
            row = officers.get(g.source_id)
            out[g.id] = bool(
                row and row.user_id == g.user_id and row.status == 'active'
                and row.term_start <= today
                and (row.term_end is None or today <= row.term_end))
        elif g.source_type == 'membership':
            row = mships.get(g.source_id)
            out[g.id] = bool(row and row.user_id == g.user_id
                             and row.group_id == g.group_id_snapshot)
        elif g.source_type == 'direct':
            out[g.id] = g.role == 'governance'
        else:
            out[g.id] = False
    return out


# ── 授权有效性（A05/A06 核心）────────────────────────────────

def _grants_raw(user):
    return WorkAccessGrant.query.filter_by(user_id=user.id, status='active').all()


def grant_status_reason(g):
    """单条授权当前是否有效 + 失效原因（治理列表展示与判定共用的单一真相源）。"""
    if g.status != 'active':
        return False, '已撤销'
    today = date.today()
    if g.valid_from and today < g.valid_from:
        return False, '尚未生效'
    if g.valid_until and today > g.valid_until:
        return False, '已过有效期'
    if g.source_type == 'officer':
        row = ClubOfficer.query.get(g.source_id) if g.source_id else None
        if not row or row.user_id != g.user_id:
            return False, '任职来源行不存在'
        if row.status != 'active':
            return False, '任职已卸任'
        if not (row.term_start <= today and (row.term_end is None or today <= row.term_end)):
            return False, '任期已结束'
        return True, ''
    if g.source_type == 'membership':
        row = ClubMembership.query.get(g.source_id) if g.source_id else None
        # A06：归属行原地改组后与授权时快照不匹配 → 失效，权限不跟随漂移
        if not row or row.user_id != g.user_id:
            return False, '归属来源行不存在'
        if row.group_id != g.group_id_snapshot:
            return False, '组归属已调整（授权绑定原组）'
        return True, ''
    if g.source_type == 'direct':
        if g.role != 'governance':
            return False, 'direct 来源仅限治理岗位'
        return True, ''
    return False, '来源类型未知'


def _grant_effective(g):
    """单条授权有效性（判定语义见 grant_status_reason，二者同源）。"""
    return grant_status_reason(g)[0]


def grants_for(user):
    """全部有效授权（含 governance）。每请求实时计算。"""
    return [g for g in _grants_raw(user) if _grant_effective(g)]


def is_governance(user):
    """治理门禁：超管 ∨ 有效 governance 授权。治理身份不自动获得正文读取权（§5.4）。"""
    if user is None:
        return False
    if user.is_admin():
        return True
    return any(g.role == 'governance' for g in grants_for(user))


def workspace_access(user):
    """有效工作区访问映射 {workspace_id: role}。仅 active 工作区；
    governance 授权不进本映射（治理≠阅读）；coordinator 优先于 member。"""
    result = {}
    for g in _grants_raw(user):
        if g.role == 'governance' or g.workspace_id is None:
            continue
        if not _grant_effective(g):
            continue
        ws = WorkWorkspace.query.get(g.workspace_id)
        if not ws or ws.status != 'active':
            continue
        if result.get(ws.id) != 'coordinator':
            result[ws.id] = g.role
    return result


def subtree_workspaces(user):
    """本人持有 subtree 授权的工作区集合 {workspace_id}（跨组方案 §4.1）。
    仅 coordinator 授权可带 subtree；返回的是「被授权的组」自身工作区 id。"""
    out = set()
    for g in _grants_raw(user):
        if g.role != 'coordinator' or not g.subtree or g.workspace_id is None:
            continue
        if not _grant_effective(g):
            continue
        ws = WorkWorkspace.query.get(g.workspace_id)
        if ws and ws.status == 'active':
            out.add(ws.id)
    return out


def summary_access(user):
    """摘要级可见工作区 {workspace_id: role}（跨组方案 §4.1）：本人工作区 ∪
    subtree 授权组的**子孙组**工作区。摘要=标题/负责人/状态/截止（无正文）；
    受限事项（participants 档）在摘要层只显示占位不显示标题。"""
    from models import ClubGroup
    result = dict(workspace_access(user))
    subtree_roots = subtree_workspaces(user)
    if not subtree_roots:
        return result
    ws_rows = WorkWorkspace.query.filter(
        WorkWorkspace.id.in_(list(subtree_roots)), WorkWorkspace.status == 'active').all()
    root_group_ids = {w.club_group_id for w in ws_rows if w.club_group_id}
    if not root_group_ids:
        return result
    # 沿 parent_id 下行收集子孙组（应用层遍历，组数量级小）
    descendants = set()
    frontier = set(root_group_ids)
    while frontier:
        children = ClubGroup.query.filter(
            ClubGroup.parent_id.in_(list(frontier)),
            ClubGroup.status == 'active').all()
        next_frontier = set()
        for c in children:
            if c.id not in descendants:
                descendants.add(c.id)
                next_frontier.add(c.id)
        frontier = next_frontier - descendants
    if not descendants:
        return result
    child_ws = WorkWorkspace.query.filter(
        WorkWorkspace.club_group_id.in_(list(descendants)),
        WorkWorkspace.status == 'active').all()
    for w in child_ws:
        result.setdefault(w.id, 'summary')      # 本人授权优先于 summary 级
    return result


def filter_items_summary_query(query, user):
    """摘要列表过滤（跨组方案 §4.1）：workspace 档（含子树子孙组）∪ 参与档占位
    ∪ 本人草稿。与正文层不同：子孙组的 workspace 档事项可见「摘要行」，
    participants 档返回 restricted 占位（不查标题）。"""
    ws_map = summary_access(user)
    conds = []
    if ws_map:
        conds.append(WorkItem.workspace_id.in_(list(ws_map.keys())))
    conds.append(and_(
        WorkItem.id.in_(
            db.session.query(WorkItemParticipant.item_id).filter(
                WorkItemParticipant.user_id == user.id,
                WorkItemParticipant.removed_at.is_(None))),
        WorkItem.status != 'draft',
        WorkItem.workspace_id.in_(_active_ws_ids_subquery()),
    ))
    conds.append(and_(
        WorkItem.created_by == user.id,
        WorkItem.status == 'draft',
        WorkItem.workspace_id.in_(_active_ws_ids_subquery()),
    ))
    return query.filter(or_(*conds))


# ── 对象级判定与查询过滤（§5.3）───────────────────────────────

def _active_ws_ids_subquery():
    return db.session.query(WorkWorkspace.id).filter(WorkWorkspace.status == 'active')


def can_read_item(user, item):
    """对象级读取判定，返回 ItemAccess 或 None。

    草稿仅作者可见；workspace 档=有效工作区授权；participants 档=作者∨有效参与行
    （协调员无天然阅读权，§5.3）；工作区停用一律关闸（D04）。调用方对
    「不存在」与「无权」统一回 404，不泄露存在性。"""
    if item is None:
        return None
    ws = WorkWorkspace.query.get(item.workspace_id)
    if not ws or ws.status != 'active':
        return None
    if item.status == 'draft':
        if item.created_by == user.id:
            return ItemAccess('author')
        return None
    if item.visibility == 'workspace':
        ws_map = workspace_access(user)
        if item.workspace_id in ws_map:
            return ItemAccess('workspace', ws_map[item.workspace_id])
        # 被邀参与本事项的跨组人员同样可读（参与关系是事项级授权，§5.3；
        # 与 filter_items_query 的参与分支口径一致）
        p = WorkItemParticipant.query.filter_by(item_id=item.id, user_id=user.id).first()
        if p and p.removed_at is None:
            return ItemAccess('participant')
        return None
    # participants 档：作者 ∨ 有效参与行（协调员无天然阅读权）
    if item.created_by == user.id:
        return ItemAccess('author')
    p = WorkItemParticipant.query.filter_by(item_id=item.id, user_id=user.id).first()
    if p and p.removed_at is None:
        return ItemAccess('participant')
    return None


def filter_items_query(query, user):
    """列表/计数/检索共用的查询阶段过滤（§5.3）。

    可见 = 有效工作区授权（workspace 档）∨ 有效参与行（任意档，非草稿）
    ∨ 本人草稿；后两支同样要求工作区 active。total/分页必须从同一过滤后
    query 派生，计数不外露未授权条目。"""
    ws_map = workspace_access(user)
    conds = []
    if ws_map:
        conds.append(WorkItem.workspace_id.in_(list(ws_map.keys())))
    conds.append(and_(
        WorkItem.id.in_(
            db.session.query(WorkItemParticipant.item_id).filter(
                WorkItemParticipant.user_id == user.id,
                WorkItemParticipant.removed_at.is_(None))),
        WorkItem.status != 'draft',
        WorkItem.workspace_id.in_(_active_ws_ids_subquery()),
    ))
    conds.append(and_(
        WorkItem.created_by == user.id,
        WorkItem.status == 'draft',
        WorkItem.workspace_id.in_(_active_ws_ids_subquery()),
    ))
    return query.filter(or_(*conds))


# ── 蓝图门禁（每端点第一行调用，失败抛 WorkApiError）──────────

def require_governance(user):
    """治理端点门禁：超管 ∨ governance 授权（§5.4）。"""
    if user is None:
        raise WorkApiError(401, '用户未认证')
    if not is_governance(user):
        raise WorkApiError(403, '需要内部协作治理权限')


def require_workspace(user, ws_id, roles=None):
    """工作区级门禁：不存在/停用/无授权统一 404；有授权但岗位不足 403。"""
    if user is None:
        raise WorkApiError(401, '用户未认证')
    try:
        ws_id = int(ws_id)
    except (TypeError, ValueError):
        raise WorkApiError(404, '工作区不存在')
    ws = WorkWorkspace.query.get(ws_id)
    if not ws or ws.status != 'active':
        raise WorkApiError(404, '工作区不存在')
    role = workspace_access(user).get(ws_id)
    if role is None:
        raise WorkApiError(404, '工作区不存在')
    if roles and role not in roles:
        raise WorkApiError(403, '该操作需要协调员权限')
    return ws


def require_read(user, item):
    """事项读取门禁：返回 (item, ItemAccess)；不存在 ∨ 无权统一 404（§13）。"""
    if user is None:
        raise WorkApiError(401, '用户未认证')
    if item is None:
        raise WorkApiError(404, '事项不存在')
    item_access = can_read_item(user, item)
    if item_access is None:
        raise WorkApiError(404, '事项不存在')
    return item, item_access

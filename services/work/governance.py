"""内部工作台·治理域服务层（设计方案 §5.4；#32 从蓝图抽离）。

蓝图只留门禁 + commit + 序列化；授权校验、待办摘要计数、候选人聚合、
工作区清单等治理逻辑集中在本模块（与用户端 services/work 分层对齐）。
全部函数只查询/校验不自行 commit——写路径与蓝图同事务。
"""
from datetime import datetime, timedelta

from exts import db
from models import (UserModel, ClubGroup, ClubMembership, ClubOfficer,
                    WorkAccessGrant, WorkItem, WorkResponseRequest, WorkTask,
                    WorkTransferRequest, WorkWorkspace)
from services.work import access
from services.work.access import WorkApiError

# 任务活跃状态集（逾期/到期统计范围；done/cancelled 不计）
ACTIVE_TASK_STATUSES = ('todo', 'in_progress', 'blocked', 'review')


# ── 待办摘要计数（/work/me 的 todo 段，§6.1）─────────────────

def todo_counts(user):
    """我的待办摘要计数（§6.1：未读与待办分开；全部经访问过滤，撤权即归零）。

    pending_responses/pending_transfers 与 /me/todos 列表同口径（#34：计数也走
    filter_items_query，撤权后徽标数不大于列表条数）。"""
    now = datetime.now()
    uid = user.id
    pending_responses = access.filter_items_query(
        db.session.query(WorkResponseRequest)
        .join(WorkItem, WorkItem.id == WorkResponseRequest.item_id)
        .filter(WorkResponseRequest.responder_user_id == uid,
                WorkResponseRequest.status == 'pending'), user).count()
    pending_transfers = access.filter_items_query(
        db.session.query(WorkTransferRequest)
        .join(WorkItem, WorkItem.id == WorkTransferRequest.item_id)
        .filter(WorkTransferRequest.to_user_id == uid,
                WorkTransferRequest.status == 'pending'), user).count()

    def _my_tasks():
        q = (db.session.query(WorkTask).join(WorkItem, WorkItem.id == WorkTask.item_id)
             .filter(WorkItem.status.in_(ACTIVE_TASK_STATUSES),
                     WorkTask.assignee_user_id == uid))
        return access.filter_items_query(q, user)

    to_review = access.filter_items_query(
        db.session.query(WorkTask).join(WorkItem, WorkItem.id == WorkTask.item_id)
        .filter(WorkItem.status == 'review', WorkTask.reviewer_user_id == uid), user).count()
    mine = _my_tasks()
    due_soon = mine.filter(WorkTask.due_at.isnot(None), WorkTask.due_at >= now,
                           WorkTask.due_at < now + timedelta(hours=24)).count()
    overdue = mine.filter(WorkTask.due_at.isnot(None), WorkTask.due_at < now).count()
    return {
        "pending_responses": pending_responses,
        "pending_transfers": pending_transfers,
        "to_review": to_review,
        "due_soon": due_soon,
        "overdue": overdue,
    }


# ── 参与邀请候选人与回应人选择源（§5.3）──────────────────────

def list_candidates(user):
    """全部有效授权持有人 ∪ 在任干事（§5.3 无权查看者不出现在可发送提及列表中）。

    门禁为资格判定（#43：eligibility 有效即可调——纯在任干事可被邀为参与者，
    需要选回应人；治理身份放行）。授权有效性批量判定（#33）。"""
    if access.eligibility(user) is None and not access.is_governance(user):
        raise WorkApiError(403, '需要内部协作资格')
    from datetime import date
    today = date.today()
    uids = set()
    grants = WorkAccessGrant.query.filter_by(status='active').all()
    if grants:
        effective = access.grants_effective_bulk(grants)
        uids.update(g.user_id for g in grants if effective.get(g.id))
    for o in ClubOfficer.query.filter_by(status='active').all():
        if o.term_start <= today and (o.term_end is None or today <= o.term_end):
            uids.add(o.user_id)
    rows = []
    if uids:
        names = {u.id: u.username for u in
                 UserModel.query.filter(UserModel.id.in_(list(uids))).all()}
        # 身份标注（X1）：主要组 + 在任职位——邀请/转交/点名/交付的候选下拉可用性前提
        from models import ClubGroup, ClubMembership
        groups = {g.id: g.name for g in ClubGroup.query.all()}
        primary = {m.user_id: groups.get(m.group_id)
                   for m in ClubMembership.query.filter_by(slot='primary').all()}
        titles = {}
        for o in ClubOfficer.query.filter_by(status='active').all():
            titles.setdefault(o.user_id, o.title)
        rows = [{'user_id': uid, 'username': names.get(uid),
                 'group_name': primary.get(uid), 'title': titles.get(uid)}
                for uid in sorted(uids)]
    return rows


# ── 工作区清单（治理视图，§4.2）──────────────────────────────

def list_workspaces():
    """全部工作区 + 组状态与活跃授权计数（一次 in_ 预取，#33）。"""
    rows = WorkWorkspace.query.order_by(WorkWorkspace.id).all()
    gids = [r.club_group_id for r in rows]
    groups = {g.id: g for g in ClubGroup.query.filter(ClubGroup.id.in_(gids)).all()} \
        if gids else {}
    items = []
    for r in rows:
        g = groups.get(r.club_group_id)
        items.append({
            'id': r.id, 'club_group_id': r.club_group_id, 'scope': r.scope,
            'group_name': (g.name if g else None) if r.scope == 'group' else '社团工作区',
            'status': r.status, 'role': None,
            'group_status': g.status if g else None,
            'active_grants': WorkAccessGrant.query.filter_by(
                workspace_id=r.id, status='active').count(),
        })
    return items


# ── 授权开通校验（§5.2/§5.4，蓝图 POST /governance/grants）──

def _parse_date_field(raw, field):
    """日期参数：YYYY-MM-DD 或 None；非法抛 400（服务层统一 WorkApiError）。"""
    if raw is None:
        return None
    try:
        return datetime.strptime(str(raw), "%Y-%m-%d").date()
    except ValueError:
        raise WorkApiError(400, f"{field} 格式应为 YYYY-MM-DD")


def validate_grant_payload(actor, data):
    """治理开通授权的集中校验（§5.2/§5.4）：返回字段 dict，非法抛 WorkApiError。

    规则：governance 岗位仅超管可授（治理不能造治理）；member/coordinator
    必须指定工作区；来源行必须存在且属于该用户；membership 来源强制写组快照；
    来源组与工作区组绑定校验（#7 收紧：membership 必须同组；officer 挂组须
    同组，社团级职位 group_id 空=放行任意组）。"""
    uid = access.int_or_400(data.get("user_id"), "user_id")
    target = UserModel.query.get(uid)
    if not target:
        raise WorkApiError(404, "用户不存在")
    # R0 核验门槛（2026-10-02 收紧批）：工作区授权（member/coordinator/governance）
    # 属权限挂载——目标账号须已核验（函数内导入，保持本模块零 flask 依赖的顶层干净）
    from services.identity import gates as identity_gates
    _reason = identity_gates.verify_reject_reason(target, 'appoint')
    if _reason:
        raise WorkApiError(403, _reason)

    role = data.get("role")
    if role not in access.GRANT_ROLES:
        raise WorkApiError(400, f"role 仅支持 {'/'.join(access.GRANT_ROLES)}")

    grant_reason = (str(data.get("grant_reason") or "").strip())
    if not grant_reason or len(grant_reason) > 200:
        raise WorkApiError(400, "授权原因必填且 ≤200 字")

    source_type = data.get("source_type")
    if source_type not in access.GRANT_SOURCES:
        raise WorkApiError(400, f"source_type 仅支持 {'/'.join(access.GRANT_SOURCES)}")

    workspace_id = None
    group_snapshot = None
    source_id = None
    if role == 'governance':
        if not actor.is_admin():
            raise WorkApiError(403, "治理岗位授权仅超级管理员可以开通")
        if data.get("workspace_id") not in (None, ''):
            raise WorkApiError(400, "治理授权为全局，不能指定工作区")
        if source_type != 'direct':
            raise WorkApiError(400, "治理授权来源须为 direct")
    else:
        workspace_id = access.int_or_400(data.get("workspace_id"), "workspace_id")
        ws = WorkWorkspace.query.get(workspace_id)
        if not ws:
            raise WorkApiError(404, "工作区不存在")
        g = ClubGroup.query.get(ws.club_group_id)
        if not g or g.status != 'active':
            raise WorkApiError(400, "工作区所属组已归档")

        if source_type == 'officer':
            source_id = access.int_or_400(data.get("source_id"), "source_id")
            off = ClubOfficer.query.get(source_id)
            if not off or off.user_id != uid:
                raise WorkApiError(400, "任职来源行不存在或不属于该用户")
            # #7 收紧：挂组任职只能授本组工作区；社团级职位（group_id 空，
            # 如社长）不挂组，放行任意组工作区
            if off.group_id is not None and off.group_id != ws.club_group_id:
                raise WorkApiError(400, "任职来源挂靠组与工作区组不一致")
        elif source_type == 'membership':
            source_id = access.int_or_400(data.get("source_id"), "source_id")
            m = ClubMembership.query.get(source_id)
            if not m or m.user_id != uid:
                raise WorkApiError(400, "归属来源行不存在或不属于该用户")
            # #7 收紧：归属必须在工作区所属组（跨组协作走事项参与者邀请，§5.2）
            if m.group_id != ws.club_group_id:
                raise WorkApiError(400, "归属来源与工作区组不一致；跨组协作请用事项参与者邀请")
            group_snapshot = m.group_id          # A06：授权时组快照，防归属行原地改组带权漂移
        else:
            raise WorkApiError(400, "direct 来源仅限治理岗位授权")

    valid_from = _parse_date_field(data.get("valid_from"), "valid_from")
    valid_until = _parse_date_field(data.get("valid_until"), "valid_until")
    if valid_from and valid_until and valid_until < valid_from:
        raise WorkApiError(400, "valid_until 不能早于 valid_from")

    # 子树汇总（跨组方案 §4.1，X1）：仅 coordinator 可勾，摘要级（无正文）
    subtree = bool(data.get("subtree")) and role == 'coordinator'

    dup = WorkAccessGrant.query.filter_by(
        user_id=uid, role=role, workspace_id=workspace_id,
        source_type=source_type, source_id=source_id, status='active').first()
    if dup:
        raise WorkApiError(409, "该用户已存在同样的有效授权")

    return {
        "user_id": uid, "role": role, "workspace_id": workspace_id,
        "source_type": source_type, "source_id": source_id,
        "group_id_snapshot": group_snapshot,
        "subtree": subtree,
        "valid_from": valid_from, "valid_until": valid_until,
        "grant_reason": grant_reason,
    }

"""内部工作台蓝图（/work，设计方案 §11.2/§13，feature/work-collab M1）。

用户端 API：资格探测（me，无资格回 200 空形——前端据此隐藏入口，不报错）、
可读工作区列表。事项/回复/任务命令随 M2/M3 迁入本蓝图；私有文件在
work_files.py（M4）。

治理接口挂 /work/governance/*——刻意避开 /admin/ 前缀：request_guard 对
/admin/ 一律超管硬门禁，而设计方案 §5.4 允许治理人员为非超管成员，故本蓝图
自带门禁 access.require_governance（超管 ∨ governance 授权）。管理端 App
首期由超管登录使用（登录模型所限），后端为未来非超管治理保留演进空间。

错误约定（§13）：不存在 ∨ 无权统一 404（防存在性探测）；状态/版本/幂等冲突
409；权限不足（对象已知）403。响应统一 {code, message, data}。
"""
from datetime import datetime, timedelta

from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required

from exts import db
from models import (UserModel, ClubGroup, ClubMembership, ClubOfficer,
                    WorkAccessGrant, WorkItem, WorkResponseRequest, WorkTask,
                    WorkTransferRequest, WorkWorkspace)
from . import _current_user, audit_log
from .notification import create_notification
from services.work import access, items as items_service, tasks as tasks_service, \
    integrations as integrations_service
from services.work.access import WorkApiError

bp = Blueprint("work", __name__, url_prefix="/work")

# 任务活跃状态集（逾期/到期统计范围；done/cancelled 不计）
ACTIVE_TASK_STATUSES = ('todo', 'in_progress', 'blocked', 'review')


@bp.errorhandler(WorkApiError)
def _handle_work_api_error(err):
    return jsonify({"code": err.code, "message": err.message, "data": None}), err.code


def _ws_dict(ws, group_name, role=None):
    return {
        "id": ws.id, "club_group_id": ws.club_group_id, "group_name": group_name,
        "status": ws.status, "role": role,
    }


def _todo_counts(user):
    """我的待办摘要计数（§6.1：未读与待办分开；全部经访问过滤，撤权即归零）。"""
    now = datetime.now()
    uid = user.id
    pending_responses = WorkResponseRequest.query.filter_by(
        responder_user_id=uid, status='pending').count()
    pending_transfers = WorkTransferRequest.query.filter_by(
        to_user_id=uid, status='pending').count()

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


# ────────────────────────────────
# 资格探测与工作区（用户端）
# ────────────────────────────────

@bp.route("/me", methods=["GET"])
@jwt_required()
def work_me():
    """资格与工作台摘要探测端点：无资格也回 200 空形（前端隐藏入口的依据，
    不把「无权限」当错误抛给普通学员）。"""
    user = _current_user()
    if not user:
        return jsonify({"code": 401, "message": "用户未认证"}), 401
    ws_map = access.workspace_access(user)
    workspaces = []
    if ws_map:
        rows = WorkWorkspace.query.filter(WorkWorkspace.id.in_(list(ws_map))).all()
        gids = [r.club_group_id for r in rows]
        gnames = {g.id: g.name for g in ClubGroup.query.filter(ClubGroup.id.in_(gids)).all()} if gids else {}
        workspaces = [_ws_dict(r, gnames.get(r.club_group_id), ws_map[r.id]) for r in rows]
    return jsonify({"code": 200, "message": "ok", "data": {
        "eligibility": access.eligibility(user),
        "is_governance": access.is_governance(user),
        "workspaces": workspaces,
        "todo": _todo_counts(user),
    }})


@bp.route("/workspaces", methods=["GET"])
@jwt_required()
def list_my_workspaces():
    """我可进入的工作区（按范围过滤，不用前端筛选代替，§13）。"""
    user = _current_user()
    if not user:
        return jsonify({"code": 401, "message": "用户未认证"}), 401
    ws_map = access.workspace_access(user)
    workspaces = []
    if ws_map:
        rows = WorkWorkspace.query.filter(WorkWorkspace.id.in_(list(ws_map))).all()
        gids = [r.club_group_id for r in rows]
        gnames = {g.id: g.name for g in ClubGroup.query.filter(ClubGroup.id.in_(gids)).all()} if gids else {}
        workspaces = [_ws_dict(r, gnames.get(r.club_group_id), ws_map[r.id]) for r in rows]
    return jsonify({"code": 200, "message": "ok", "data": {"workspaces": workspaces}})


# ────────────────────────────────
# 事项与回复（M2）
# ────────────────────────────────

@bp.route("/candidates", methods=["GET"])
@jwt_required()
def list_candidates():
    """参与邀请候选人与回应人选择源：全部有效授权持有人 ∪ 在任干事
    （§5.3 无权查看者不出现在可发送提及列表中——无资格者不返回）。"""
    from datetime import date as _date
    user = _require_user()
    if not access.grants_for(user) and not access.is_governance(user):
        raise WorkApiError(403, '需要内部协作权限')
    today = _date.today()
    uids = set()
    for g in WorkAccessGrant.query.filter_by(status='active').all():
        if access._grant_effective(g):
            uids.add(g.user_id)
    for o in ClubOfficer.query.filter_by(status='active').all():
        if o.term_start <= today and (o.term_end is None or today <= o.term_end):
            uids.add(o.user_id)
    rows = []
    if uids:
        names = {u.id: u.username for u in
                 UserModel.query.filter(UserModel.id.in_(list(uids))).all()}
        rows = [{'user_id': uid, 'username': names.get(uid)} for uid in sorted(uids)]
    return jsonify({"code": 200, "message": "ok", "data": {"candidates": rows}})


def _require_user():
    user = _current_user()
    if not user:
        raise WorkApiError(401, '用户未认证')
    return user


@bp.route("/items", methods=["GET"])
@jwt_required()
def list_items():
    """事项列表：kind/status/workspace/mine/q 筛选；列表与计数同一授权条件（§13）。"""
    user = _require_user()
    data = items_service.list_items(
        user,
        kind=request.args.get("kind"),
        status=request.args.get("status"),
        workspace_id=request.args.get("workspace_id"),
        mine=request.args.get("mine") in ('1', 'true'),
        q=request.args.get("q"),
        page=request.args.get("page", 1),
        page_size=request.args.get("page_size", 20),
    )
    return jsonify({"code": 200, "message": "ok", "data": data})


@bp.route("/items", methods=["POST"])
@jwt_required()
def create_item():
    """创建话题/任务草稿（幂等键去重；发布走 commands）。"""
    user = _require_user()
    item, created = items_service.create_item(user, request.get_json(silent=True) or {})
    db.session.commit()
    if not created:
        return jsonify({"code": 200, "message": "事项已存在（幂等返回）",
                        "data": {"id": item.id, "status": item.status}})
    detail = items_service.get_item_detail(user, item.id)
    return jsonify({"code": 200, "message": "已创建草稿", "data": detail})


@bp.route("/items/<int:item_id>", methods=["GET"])
@jwt_required()
def get_item(item_id):
    """事项详情（含 allowed_actions 与业务关联安全投影；统一 404 防探测）。"""
    user = _require_user()
    data = items_service.get_item_detail(user, item_id)
    data["business_links"] = integrations_service.project_for(user, item_id)
    return jsonify({"code": 200, "message": "ok", "data": data})


@bp.route("/items/<int:item_id>", methods=["PATCH"])
@jwt_required()
def patch_item(item_id):
    """编辑标题/正文/可见范围（expected_version 乐观锁，冲突 409）。"""
    user = _require_user()
    item = items_service.patch_item(user, item_id, request.get_json(silent=True) or {})
    db.session.commit()
    return jsonify({"code": 200, "message": "已更新",
                    "data": {"id": item.id, "version": item.version}})


@bp.route("/items/<int:item_id>/replies", methods=["GET"])
@jwt_required()
def list_replies(item_id):
    """回复时间线：按服务器序号游标分页（after_seq），断线增量拉取（§7.5）。"""
    user = _require_user()
    data = items_service.list_replies(user, item_id,
                                      after_seq=request.args.get("after_seq", 0),
                                      limit=request.args.get("limit", 50))
    return jsonify({"code": 200, "message": "ok", "data": data})


@bp.route("/items/<int:item_id>/replies", methods=["POST"])
@jwt_required()
def create_reply(item_id):
    """留言/回信：幂等发送（client_request_id），同事务落事件与通知（B01）。"""
    user = _require_user()
    reply, created = items_service.create_reply(user, item_id,
                                                request.get_json(silent=True) or {})
    db.session.commit()
    if not created:
        return jsonify({"code": 200, "message": "回复已存在（幂等返回）",
                        "data": {"id": reply.id, "seq": reply.seq}})
    return jsonify({"code": 200, "message": "已发送", "data": {"id": reply.id, "seq": reply.seq}})


@bp.route("/items/<int:item_id>/commands", methods=["POST"])
@jwt_required()
def item_commands(item_id):
    """命令白名单（§13）：话题 publish/close/reopen + 任务命令表（M3）。
    全部带 expected_version 条件校验，冲突 409 version_conflict。"""
    user = _require_user()
    payload = request.get_json(silent=True) or {}
    command = payload.get("command")
    if command == "promote":
        item, _task, created = tasks_service.promote(user, item_id, payload)
        db.session.commit()
        return jsonify({"code": 200,
                        "message": "已转为任务" if created else "已是任务（幂等返回）",
                        "data": {"id": item.id, "kind": item.kind,
                                 "status": item.status, "version": item.version}})
    item = (WorkItem.query.filter_by(id=item_id).first())
    if item is not None and item.kind == "task":
        item = tasks_service.command_task(user, item_id, command, payload)
    else:
        item = items_service.command_topic(user, item_id, command, payload)
    db.session.commit()
    return jsonify({"code": 200, "message": "已执行",
                    "data": {"id": item.id, "status": item.status, "version": item.version}})


@bp.route("/items/<int:item_id>/participants", methods=["POST"])
@jwt_required()
def add_participant(item_id):
    """邀请协作者（协调员∨作者；目标须有协作资格，§5.2）。"""
    user = _require_user()
    items_service.add_participant(user, item_id, request.get_json(silent=True) or {})
    db.session.commit()
    return jsonify({"code": 200, "message": "已加入参与者"})


@bp.route("/items/<int:item_id>/participants/<int:target_id>", methods=["DELETE"])
@jwt_required()
def remove_participant(item_id, target_id):
    user = _require_user()
    payload = request.get_json(silent=True) or {}
    items_service.remove_participant(user, item_id, target_id, payload.get("reason"))
    db.session.commit()
    return jsonify({"code": 200, "message": "已移除参与者"})


@bp.route("/items/<int:item_id>/read", methods=["POST"])
@jwt_required()
def advance_read(item_id):
    """推进本人已读位置（只进不退，不得越过可见最新回复，§13）。"""
    user = _require_user()
    payload = request.get_json(silent=True) or {}
    row = items_service.advance_read(user, item_id, payload.get("last_read_seq"))
    db.session.commit()
    return jsonify({"code": 200, "message": "ok", "data": {"last_read_seq": row.last_read_seq}})


@bp.route("/items/<int:item_id>/events", methods=["GET"])
@jwt_required()
def list_events(item_id):
    """工作时间线（治理类事件按可见范围过滤，§13）。"""
    user = _require_user()
    data = items_service.list_events(user, item_id,
                                     page=request.args.get("page", 1),
                                     page_size=request.args.get("page_size", 50))
    return jsonify({"code": 200, "message": "ok", "data": data})


@bp.route("/items/<int:item_id>/transfers", methods=["POST"])
@jwt_required()
def create_transfer(item_id):
    """发起负责人转交（§8.4）：同事项仅一个待确认；目标须有协作资格。"""
    user = _require_user()
    req = tasks_service.create_transfer(user, item_id, request.get_json(silent=True) or {})
    db.session.commit()
    return jsonify({"code": 200, "message": "转交已发起，等待对方确认",
                    "data": {"transfer_id": req.id, "expires_at":
                             req.expires_at.strftime('%Y-%m-%d %H:%M')}})


@bp.route("/transfers/<int:transfer_id>/<action>", methods=["POST"])
@jwt_required()
def decide_transfer(transfer_id, action):
    """接受/拒绝/撤回转交（accept|reject|withdraw；接受=原子替换负责人，B03）。"""
    user = _require_user()
    if action not in ("accept", "reject", "withdraw"):
        return jsonify({"code": 400, "message": "action 仅支持 accept/reject/withdraw"}), 400
    req = tasks_service.decide_transfer(user, transfer_id, action)
    db.session.commit()
    messages = {"accept": "已接手，你现在是该任务的负责人",
                "reject": "已拒绝转交，原负责人保持不变",
                "withdraw": "已撤回转交"}
    return jsonify({"code": 200, "message": messages[action],
                    "data": {"transfer_id": req.id, "status": req.status}})


@bp.route("/items/<int:item_id>/links", methods=["POST"])
@jwt_required()
def create_business_link(item_id):
    """建立业务关联（§11.3 三规则：只读投影，不授原业务权限、不改原业务状态）。"""
    user = _require_user()
    integrations_service.create_link(user, item_id, request.get_json(silent=True) or {})
    db.session.commit()
    return jsonify({"code": 200, "message": "已建立关联"})


@bp.route("/me/todos", methods=["GET"])
@jwt_required()
def my_todos():
    """我的待办四桶（§6.1）：待接手/待回复/待验收/到期（含逾期标记）。"""
    user = _require_user()
    return jsonify({"code": 200, "message": "ok", "data": tasks_service.my_todos(user)})


# ────────────────────────────────
# 治理：工作区管理（/work/governance/workspaces）
# ────────────────────────────────

def _parse_date(raw, field):
    if raw is None:
        return None, None
    try:
        return datetime.strptime(str(raw), "%Y-%m-%d").date(), None
    except ValueError:
        return None, (jsonify({"code": 400, "message": f"{field} 格式应为 YYYY-MM-DD"}), 400)


@bp.route("/governance/workspaces", methods=["GET"])
@jwt_required()
def governance_list_workspaces():
    user = _current_user()
    access.require_governance(user)
    rows = WorkWorkspace.query.order_by(WorkWorkspace.id).all()
    gids = [r.club_group_id for r in rows]
    groups = {g.id: g for g in ClubGroup.query.filter(ClubGroup.id.in_(gids)).all()} if gids else {}
    items = []
    for r in rows:
        g = groups.get(r.club_group_id)
        items.append({
            **_ws_dict(r, g.name if g else None),
            "group_status": g.status if g else None,
            "active_grants": WorkAccessGrant.query.filter_by(
                workspace_id=r.id, status='active').count(),
        })
    return jsonify({"code": 200, "message": "ok", "data": {"workspaces": items}})


@bp.route("/governance/workspaces", methods=["POST"])
@jwt_required()
@audit_log(operation="开通组工作区")
def governance_create_workspace():
    """为启用组别开通工作区（一组最多一个，§4.2）。body {club_group_id}"""
    user = _current_user()
    access.require_governance(user)
    data = request.get_json(silent=True) or {}
    try:
        gid = int(data.get("club_group_id"))
    except (TypeError, ValueError):
        return jsonify({"code": 400, "message": "缺少 club_group_id"}), 400
    g = ClubGroup.query.get(gid)
    if not g:
        return jsonify({"code": 404, "message": "组不存在"}), 404
    if g.status != 'active':
        return jsonify({"code": 400, "message": "组已归档，不能开通工作区"}), 400
    if WorkWorkspace.query.filter_by(club_group_id=gid).first():
        return jsonify({"code": 409, "message": "该组已开通工作区"}), 409
    ws = WorkWorkspace(club_group_id=gid, status='active')
    db.session.add(ws)
    db.session.commit()
    return jsonify({"code": 200, "message": f"已为「{g.name}」开通工作区",
                    "data": _ws_dict(ws, g.name)})


@bp.route("/governance/workspaces/<int:wid>/status", methods=["POST"])
@jwt_required()
@audit_log(operation="调整工作区状态")
def governance_set_workspace_status(wid):
    """启停工作区（D04 预案）：disabled=入口关闸，数据与授权记录保留。body {status}"""
    user = _current_user()
    access.require_governance(user)
    ws = WorkWorkspace.query.get(wid)
    if not ws:
        return jsonify({"code": 404, "message": "工作区不存在"}), 404
    data = request.get_json(silent=True) or {}
    status = data.get("status")
    if status not in ('active', 'disabled'):
        return jsonify({"code": 400, "message": "status 仅支持 active/disabled"}), 400
    old = ws.status
    ws.status = status
    db.session.commit()
    g = ClubGroup.query.get(ws.club_group_id)
    return jsonify({"code": 200, "message": f"工作区已{'启用' if status == 'active' else '停用'}（数据与授权记录保留）",
                    "data": {**_ws_dict(ws, g.name if g else None), "prev_status": old}})


# ────────────────────────────────
# 治理：授权（/work/governance/grants）
# ────────────────────────────────

def _grant_dict(g, user_map, ws_rows, gnames):
    ws = ws_rows.get(g.workspace_id)
    effective, reason = access.grant_status_reason(g)
    return {
        "id": g.id,
        "user_id": g.user_id,
        "username": user_map.get(g.user_id),
        "role": g.role,
        "workspace_id": g.workspace_id,
        "workspace_group_name": gnames.get(ws.club_group_id) if ws else None,
        "source_type": g.source_type,
        "source_id": g.source_id,
        "group_id_snapshot": g.group_id_snapshot,
        "valid_from": g.valid_from.isoformat() if g.valid_from else None,
        "valid_until": g.valid_until.isoformat() if g.valid_until else None,
        "status": g.status,
        "revoke_reason": g.revoke_reason,
        "granted_by": g.granted_by,
        "grant_reason": g.grant_reason,
        "effective": effective,
        "ineffective_reason": reason or None,
        "created_at": g.created_at.strftime('%Y-%m-%d %H:%M') if g.created_at else None,
    }


@bp.route("/governance/grants", methods=["GET"])
@jwt_required()
def governance_list_grants():
    """授权清单（分页）：status=active/revoked/all（默认 all），
    可按 user_id / workspace_id 过滤。含实时有效性判定与失效原因。"""
    user = _current_user()
    access.require_governance(user)
    try:
        page = max(1, int(request.args.get("page", 1)))
        page_size = min(100, max(1, int(request.args.get("page_size", 20))))
    except ValueError:
        page, page_size = 1, 20
    status = request.args.get("status", "all")

    query = WorkAccessGrant.query
    if status in ('active', 'revoked'):
        query = query.filter(WorkAccessGrant.status == status)
    if request.args.get("user_id"):
        query = query.filter(WorkAccessGrant.user_id == int(request.args["user_id"]))
    if request.args.get("workspace_id"):
        query = query.filter(WorkAccessGrant.workspace_id == int(request.args["workspace_id"]))
    total = query.count()
    rows = (query.order_by(WorkAccessGrant.status, WorkAccessGrant.id.desc())
            .offset((page - 1) * page_size).limit(page_size).all())

    uids = list({r.user_id for r in rows} | {r.granted_by for r in rows})
    user_map = {u.id: u.username for u in UserModel.query.filter(UserModel.id.in_(uids)).all()} if uids else {}
    ws_ids = list({r.workspace_id for r in rows if r.workspace_id})
    ws_rows = {w.id: w for w in WorkWorkspace.query.filter(WorkWorkspace.id.in_(ws_ids)).all()} if ws_ids else {}
    gids = [w.club_group_id for w in ws_rows.values()]
    gnames = {g.id: g.name for g in ClubGroup.query.filter(ClubGroup.id.in_(gids)).all()} if gids else {}

    return jsonify({"code": 200, "message": "ok", "data": {
        "grants": [_grant_dict(r, user_map, ws_rows, gnames) for r in rows],
        "total": total, "page": page, "page_size": page_size,
    }})


def _validate_grant_payload(actor, data):
    """治理开通授权的集中校验（§5.2/§5.4）：返回 (字段dict, 错误响应)。

    规则：governance 岗位仅超管可授（治理不能造治理）；member/coordinator
    必须指定工作区；来源行必须存在且属于该用户；membership 来源强制写组快照。"""
    try:
        uid = int(data.get("user_id"))
    except (TypeError, ValueError):
        return None, (jsonify({"code": 400, "message": "缺少 user_id"}), 400)
    target = UserModel.query.get(uid)
    if not target:
        return None, (jsonify({"code": 404, "message": "用户不存在"}), 404)

    role = data.get("role")
    if role not in access.GRANT_ROLES:
        return None, (jsonify({"code": 400,
                               "message": f"role 仅支持 {'/'.join(access.GRANT_ROLES)}"}), 400)

    grant_reason = (str(data.get("grant_reason") or "").strip())
    if not grant_reason or len(grant_reason) > 200:
        return None, (jsonify({"code": 400, "message": "授权原因必填且 ≤200 字"}), 400)

    source_type = data.get("source_type")
    if source_type not in access.GRANT_SOURCES:
        return None, (jsonify({"code": 400,
                               "message": f"source_type 仅支持 {'/'.join(access.GRANT_SOURCES)}"}), 400)

    workspace_id = None
    group_snapshot = None
    source_id = None
    if role == 'governance':
        if not actor.is_admin():
            return None, (jsonify({"code": 403, "message": "治理岗位授权仅超级管理员可以开通"}), 403)
        if data.get("workspace_id") not in (None, ''):
            return None, (jsonify({"code": 400, "message": "治理授权为全局，不能指定工作区"}), 400)
        if source_type != 'direct':
            return None, (jsonify({"code": 400, "message": "治理授权来源须为 direct"}), 400)
    else:
        try:
            workspace_id = int(data.get("workspace_id"))
        except (TypeError, ValueError):
            return None, (jsonify({"code": 400, "message": "缺少 workspace_id"}), 400)
        ws = WorkWorkspace.query.get(workspace_id)
        if not ws:
            return None, (jsonify({"code": 404, "message": "工作区不存在"}), 404)
        g = ClubGroup.query.get(ws.club_group_id)
        if not g or g.status != 'active':
            return None, (jsonify({"code": 400, "message": "工作区所属组已归档"}), 400)

        if source_type == 'officer':
            try:
                source_id = int(data.get("source_id"))
            except (TypeError, ValueError):
                return None, (jsonify({"code": 400, "message": "officer 来源须提供 source_id"}), 400)
            off = ClubOfficer.query.get(source_id)
            if not off or off.user_id != uid:
                return None, (jsonify({"code": 400, "message": "任职来源行不存在或不属于该用户"}), 400)
        elif source_type == 'membership':
            try:
                source_id = int(data.get("source_id"))
            except (TypeError, ValueError):
                return None, (jsonify({"code": 400, "message": "membership 来源须提供 source_id"}), 400)
            m = ClubMembership.query.get(source_id)
            if not m or m.user_id != uid:
                return None, (jsonify({"code": 400, "message": "归属来源行不存在或不属于该用户"}), 400)
            group_snapshot = m.group_id          # A06：授权时组快照，防归属行原地改组带权漂移
        else:
            return None, (jsonify({"code": 400, "message": "direct 来源仅限治理岗位授权"}), 400)

    valid_from, err = _parse_date(data.get("valid_from"), "valid_from")
    if err:
        return None, err
    valid_until, err = _parse_date(data.get("valid_until"), "valid_until")
    if err:
        return None, err
    if valid_from and valid_until and valid_until < valid_from:
        return None, (jsonify({"code": 400, "message": "valid_until 不能早于 valid_from"}), 400)

    dup = WorkAccessGrant.query.filter_by(
        user_id=uid, role=role, workspace_id=workspace_id,
        source_type=source_type, source_id=source_id, status='active').first()
    if dup:
        return None, (jsonify({"code": 409, "message": "该用户已存在同样的有效授权"}), 409)

    return {
        "user_id": uid, "role": role, "workspace_id": workspace_id,
        "source_type": source_type, "source_id": source_id,
        "group_id_snapshot": group_snapshot,
        "valid_from": valid_from, "valid_until": valid_until,
        "grant_reason": grant_reason,
    }, None


@bp.route("/governance/grants", methods=["POST"])
@jwt_required()
@audit_log(operation="开通内部协作授权")
def governance_create_grant():
    """开通授权（§5.4）：依据现有任职/组归属 + 明确范围、有效期与事由。"""
    user = _current_user()
    access.require_governance(user)
    data = request.get_json(silent=True) or {}
    fields, err = _validate_grant_payload(user, data)
    if err:
        return err
    grant = WorkAccessGrant(granted_by=user.id, status='active', **fields)
    db.session.add(grant)
    # 同事务通知被授权人（最小信息，不含工作区敏感内容）
    create_notification(grant.user_id, "内部工作台",
                        "你的内部工作台协作权限已开通，可从服务台进入查看", category='work')
    db.session.commit()
    return jsonify({"code": 200, "message": "授权已开通",
                    "data": _grant_dict(grant, {grant.user_id: UserModel.query.get(grant.user_id).username},
                                        {grant.workspace_id: WorkWorkspace.query.get(grant.workspace_id)}
                                        if grant.workspace_id else {}, {})})


@bp.route("/governance/grants/<int:gid>/revoke", methods=["POST"])
@jwt_required()
@audit_log(operation="撤销内部协作授权")
def governance_revoke_grant(gid):
    """撤销授权：即时生效（下一请求即失效，A05）；governance 岗位撤销仅超管。"""
    user = _current_user()
    access.require_governance(user)
    grant = WorkAccessGrant.query.get(gid)
    if not grant:
        return jsonify({"code": 404, "message": "授权不存在"}), 404
    if grant.status != 'active':
        return jsonify({"code": 409, "message": "该授权已撤销"}), 409
    if grant.role == 'governance' and not user.is_admin():
        return jsonify({"code": 403, "message": "治理岗位撤销仅超级管理员可以操作"}), 403
    data = request.get_json(silent=True) or {}
    reason = (str(data.get("reason") or "").strip())
    if not reason or len(reason) > 200:
        return jsonify({"code": 400, "message": "撤销原因必填且 ≤200 字"}), 400
    grant.status = 'revoked'
    grant.revoke_reason = reason
    create_notification(grant.user_id, "内部工作台",
                        "你的内部工作台协作权限已被撤销", category='work')
    db.session.commit()
    return jsonify({"code": 200, "message": "授权已撤销（下一请求即失效）"})


# ────────────────────────────────
# 治理：初始化与能力自述
# ────────────────────────────────

@bp.route("/governance/me", methods=["GET"])
@jwt_required()
def governance_me():
    user = _current_user()
    access.require_governance(user)
    return jsonify({"code": 200, "message": "ok", "data": {
        "is_governance": True, "is_super_admin": user.is_admin(),
    }})


@bp.route("/governance/takeover-queue", methods=["GET"])
@jwt_required()
def governance_takeover_queue():
    """需接管事项（D01 标记段）：活跃任务的负责人已失去协作资格。
    撤权即时生效、历史操作者不变；接手由协调员通过 reassign/转交完成。"""
    user = _current_user()
    access.require_governance(user)
    rows = tasks_service.takeover_candidates()
    names = {}
    uids = list({r["assignee_user_id"] for r in rows if r["assignee_user_id"]})
    if uids:
        names = {u.id: u.username for u in UserModel.query.filter(UserModel.id.in_(uids)).all()}
    for r in rows:
        r["assignee_name"] = names.get(r["assignee_user_id"])
    return jsonify({"code": 200, "message": "ok", "data": {"items": rows}})


@bp.route("/governance/handover", methods=["GET"])
@jwt_required()
def governance_handover():
    """交接清单（§5.4）：按成员汇总未完成任务/待验收/待回复/失效授权。"""
    user = _current_user()
    access.require_governance(user)
    target_id = request.args.get("user_id")
    if not target_id:
        return jsonify({"code": 400, "message": "缺少 user_id"}), 400
    target = UserModel.query.get(int(target_id))
    if not target:
        return jsonify({"code": 404, "message": "用户不存在"}), 404
    data = integrations_service.handover_overview(target.id)
    data["user"] = {"id": target.id, "username": target.username}
    return jsonify({"code": 200, "message": "ok", "data": data})


@bp.route("/governance/emergency-access", methods=["POST"])
@jwt_required()
@audit_log(operation="协作治理紧急介入")
def governance_emergency_access():
    """紧急介入读取受限事项（§5.4）：理由必填，emergency_access 事件留痕。"""
    user = _current_user()
    access.require_governance(user)
    payload = request.get_json(silent=True) or {}
    try:
        item_id = int(payload.get("item_id"))
    except (TypeError, ValueError):
        return jsonify({"code": 400, "message": "缺少 item_id"}), 400
    detail = integrations_service.emergency_access(user, item_id, payload.get("reason"))
    db.session.commit()
    return jsonify({"code": 200, "message": "已介入并留痕", "data": detail})


@bp.route("/governance/bootstrap", methods=["POST"])
@jwt_required()
@audit_log(operation="初始化协作治理")
def governance_bootstrap():
    """首次开通治理人员（§5.4）：仅超管；任命第一位（或补充）治理人员。
    body {user_id, grant_reason}"""
    user = _current_user()
    if not user.is_admin():
        return jsonify({"code": 403, "message": "初始化仅超级管理员可以操作"}), 403
    data = request.get_json(silent=True) or {}
    try:
        uid = int(data.get("user_id"))
    except (TypeError, ValueError):
        return jsonify({"code": 400, "message": "缺少 user_id"}), 400
    target = UserModel.query.get(uid)
    if not target:
        return jsonify({"code": 404, "message": "用户不存在"}), 404
    reason = (str(data.get("grant_reason") or "").strip())
    if not reason or len(reason) > 200:
        return jsonify({"code": 400, "message": "授权原因必填且 ≤200 字"}), 400
    dup = WorkAccessGrant.query.filter_by(
        user_id=uid, role='governance', status='active').first()
    if dup:
        return jsonify({"code": 409, "message": "该用户已是治理人员"}), 409
    grant = WorkAccessGrant(
        user_id=uid, role='governance', workspace_id=None,
        source_type='direct', granted_by=user.id, grant_reason=reason, status='active')
    db.session.add(grant)
    create_notification(uid, "内部工作台",
                        "你已被任命为内部工作台授权治理人员", category='work')
    db.session.commit()
    return jsonify({"code": 200, "message": f"已任命 {target.username} 为治理人员",
                    "data": {"grant_id": grant.id}})

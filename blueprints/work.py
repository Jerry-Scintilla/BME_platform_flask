"""内部工作台蓝图（/work，设计方案 §11.2/§13，feature/work-collab M1）。

用户端 API：资格探测（me，无资格回 200 空形——前端据此隐藏入口，不报错）、
可读工作区列表。事项/回复/任务命令随 M2/M3 迁入本蓝图；私有文件在
work_files.py（M4）。治理域逻辑在 services/work/governance.py（#32 分层），
蓝图只留门禁 + commit + 序列化。

治理接口挂 /work/governance/*——刻意避开 /admin/ 前缀：request_guard 对
/admin/ 一律超管硬门禁，而设计方案 §5.4 允许治理人员为非超管成员，故本蓝图
自带门禁 access.require_governance（超管 ∨ governance 授权）。管理端 App
首期由超管登录使用（登录模型所限），后端为未来非超管治理保留演进空间。

错误约定（§13）：不存在 ∨ 无权统一 404（防存在性探测）；状态/版本/幂等冲突
409；权限不足（对象已知）403。响应统一 {code, message, data}。
"""
from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required

from exts import db
from models import UserModel, ClubGroup, WorkAccessGrant, WorkItem, WorkWorkspace
from . import _current_user, audit_log
from .notification import create_notification
from services.work import access, governance as governance_service, handoffs as handoffs_service, \
    boards as boards_service, projections as projections_service, \
    items as items_service, tasks as tasks_service, integrations as integrations_service, \
    provisioning as provisioning_service
from services.work.access import WorkApiError

bp = Blueprint("work", __name__, url_prefix="/work")


@bp.errorhandler(WorkApiError)
def _handle_work_api_error(err):
    return jsonify({"code": err.code, "message": err.message, "data": None}), err.code


def _ws_dict(ws, group_name, role=None):
    return {
        "id": ws.id, "club_group_id": ws.club_group_id, "group_name": group_name,
        "status": ws.status, "role": role, "auto_grant": bool(ws.auto_grant),
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
    subtree_roots = access.subtree_workspaces(user)
    workspaces = []
    if ws_map:
        rows = WorkWorkspace.query.filter(WorkWorkspace.id.in_(list(ws_map))).all()
        gids = [r.club_group_id for r in rows]
        gnames = {g.id: g.name for g in ClubGroup.query.filter(ClubGroup.id.in_(gids)).all()} if gids else {}
        for r in rows:
            d = _ws_dict(r, gnames.get(r.club_group_id), ws_map[r.id])
            d["subtree"] = r.id in subtree_roots      # 子组汇总入口显隐（X1）
            workspaces.append(d)
    # 跨组交付目标目录（组名本就公开于组织架构页；不含社团区/停用区）
    target_rows = WorkWorkspace.query.filter_by(status='active', scope='group').all()
    t_gids = [r.club_group_id for r in target_rows if r.club_group_id]
    t_gnames = {g.id: g.name for g in ClubGroup.query.filter(ClubGroup.id.in_(t_gids)).all()} if t_gids else {}
    available_targets = [{"ws_id": r.id, "group_name": t_gnames.get(r.club_group_id)}
                         for r in target_rows if r.club_group_id]
    return jsonify({"code": 200, "message": "ok", "data": {
        "eligibility": access.eligibility(user),
        "is_governance": access.is_governance(user),
        "workspaces": workspaces,
        "available_targets": available_targets,
        "todo": governance_service.todo_counts(user),
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
    （§5.3 无权查看者不出现在可发送提及列表中——无资格者不返回；
    门禁为资格判定：纯在任干事可被邀为参与者，#43）。"""
    user = _require_user()
    rows = governance_service.list_candidates(user)
    return jsonify({"code": 200, "message": "ok", "data": {"candidates": rows}})


def _require_user():
    user = _current_user()
    if not user:
        raise WorkApiError(401, '用户未认证')
    return user


@bp.route("/items", methods=["GET"])
@jwt_required()
def list_items():
    """事项列表：kind/status/workspace/mine/q 筛选；列表与计数同一授权条件（§13）。
    rollup=subtree 时走摘要层（跨组方案 §4.1：仅标题/状态/负责人/截止，无正文）。"""
    user = _require_user()
    if request.args.get("rollup") == "subtree":
        if not access.subtree_workspaces(user):
            return jsonify({"code": 403, "message": "需要子组汇总授权"}), 403
        data = items_service.summary_list_items(
            user, q=request.args.get("q"),
            page=request.args.get("page", 1),
            page_size=request.args.get("page_size", 20))
        return jsonify({"code": 200, "message": "ok", "data": data})
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


@bp.route("/items/<int:item_id>/links", methods=["DELETE"])
@jwt_required()
def remove_business_link(item_id):
    """移除业务关联（作者∨协调员）。"""
    user = _require_user()
    integrations_service.remove_link(user, item_id, request.get_json(silent=True) or {})
    db.session.commit()
    return jsonify({"code": 200, "message": "已移除关联"})


@bp.route("/items/<int:item_id>/handoffs", methods=["POST"])
@jwt_required()
def create_handoff(item_id):
    """发起跨组交付（跨组方案 §4.2）：源组协调员∨作者；用途自由填写。"""
    user = _require_user()
    req = handoffs_service.create_handoff(user, item_id, request.get_json(silent=True) or {})
    db.session.commit()
    return jsonify({"code": 200, "message": "交付已发起，等待对方组接单",
                    "data": {"handoff_id": req.id}})


@bp.route("/handoffs/<int:handoff_id>/<action>", methods=["POST"])
@jwt_required()
def decide_handoff(handoff_id, action):
    """接单/拒绝/撤回跨组交付（accept=同事务在目标组建关联任务）。"""
    user = _require_user()
    if action not in ("accept", "decline", "withdraw"):
        return jsonify({"code": 400, "message": "action 仅支持 accept/decline/withdraw"}), 400
    req = handoffs_service.decide_handoff(user, handoff_id, action,
                                          request.get_json(silent=True) or {})
    db.session.commit()
    messages = {"accept": "已接单，任务已建到本组工作区",
                "decline": "已拒绝，源组会收到通知",
                "withdraw": "已撤回交付"}
    return jsonify({"code": 200, "message": messages[action],
                    "data": {"handoff_id": req.id, "status": req.status,
                             "accepted_item_id": req.accepted_item_id}})


@bp.route("/objects/board", methods=["GET"])
@jwt_required()
def objects_board():
    """工作区看板：按关联对象聚合本组事项态势（X2 通用架构，组成员可见）。"""
    user = _require_user()
    data = boards_service.workspace_board(user, request.args.get("ws"))
    return jsonify({"code": 200, "message": "ok", "data": data})


@bp.route("/objects/claim", methods=["POST"])
@jwt_required()
def objects_claim():
    """认领/取消认领对象维护责任（协调员；仅 stewardable 类型；幂等）。"""
    user = _require_user()
    payload = request.get_json(silent=True) or {}
    row, changed = boards_service.claim_object(user, payload)
    db.session.commit()
    action = payload.get("action", "claim")
    msg = ("已认领" if changed else "已认领（幂等）") if action == "claim" else (
        "已取消认领" if changed else "本就未认领")
    return jsonify({"code": 200, "message": msg})


@bp.route("/objects/types", methods=["GET"])
@jwt_required()
def objects_types():
    """可关联类型目录（注册表驱动：label + stewardable，供前端下拉）。"""
    user = _require_user()
    return jsonify({"code": 200, "message": "ok", "data": {
        "types": [{"source_type": t, "label": p.label, "stewardable": p.stewardable}
                  for t, p in projections_service.PROVIDERS.items()]}})


@bp.route("/me/todos", methods=["GET"])
@jwt_required()
def my_todos():
    """我的待办四桶（§6.1）：待接手/待回复/待验收/到期（含逾期标记）。"""
    user = _require_user()
    return jsonify({"code": 200, "message": "ok", "data": tasks_service.my_todos(user)})


# ────────────────────────────────
# 治理：工作区管理（/work/governance/workspaces）
# ────────────────────────────────

@bp.route("/governance/workspaces", methods=["GET"])
@jwt_required()
def governance_list_workspaces():
    user = _current_user()
    access.require_governance(user)
    items = governance_service.list_workspaces()
    return jsonify({"code": 200, "message": "ok", "data": {"workspaces": items}})


@bp.route("/governance/workspaces", methods=["POST"])
@jwt_required()
@audit_log(operation="开通组工作区")
def governance_create_workspace():
    """为启用组别开通工作区（一组最多一个，§4.2）。body {club_group_id, auto_grant?=true}
    auto_grant=true（默认）时建区即按现任归属/任职批量补授（授权自动化方案 §3.2）。"""
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
    ws = WorkWorkspace(club_group_id=gid, status='active',
                       auto_grant=bool(data.get("auto_grant", True)))
    db.session.add(ws)
    db.session.flush()          # 拿 ws.id 供补授
    counts = {}
    if ws.auto_grant:
        counts = provisioning_service.backfill_workspace(ws, user.id, event='开通工作区')
    db.session.commit()
    return jsonify({"code": 200,
                    "message": f"已为「{g.name}」开通工作区" + (
                        f"（自动授权 {counts.get('granted', 0)} 人）" if ws.auto_grant else ""),
                    "data": {**_ws_dict(ws, g.name), "backfill": counts}})


@bp.route("/governance/workspaces/<int:wid>/auto-grant", methods=["POST"])
@jwt_required()
@audit_log(operation="调整工作区自动授权")
def governance_set_auto_grant(wid):
    """开/关入职自动授（授权自动化方案 §3.4）。body {enabled: bool}
    关=只停新增（存量自动行不动）；开=按现任组织行批量补授。"""
    user = _current_user()
    access.require_governance(user)
    ws = WorkWorkspace.query.get(wid)
    if not ws:
        return jsonify({"code": 404, "message": "工作区不存在"}), 404
    if ws.scope != 'group':
        return jsonify({"code": 400, "message": "仅组工作区支持自动授"}), 400
    data = request.get_json(silent=True) or {}
    enabled = data.get("enabled")
    if not isinstance(enabled, bool):
        return jsonify({"code": 400, "message": "enabled 必须为布尔值"}), 400
    old = ws.auto_grant
    ws.auto_grant = enabled
    counts = {}
    if enabled and not old:
        counts = provisioning_service.backfill_workspace(ws, user.id, event='开启自动授')
    db.session.commit()
    g = ClubGroup.query.get(ws.club_group_id)
    msg = (f"已开启「{g.name if g else ''}」入职自动授"
           + (f"（补授 {counts.get('granted', 0)} 人）" if counts else "")
           ) if enabled and not old else (
           f"已关闭自动授（存量授权保留）" if not enabled else "自动授本已开启")
    return jsonify({"code": 200, "message": msg,
                    "data": {**_ws_dict(ws, g.name if g else None), "backfill": counts}})


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
        "origin": g.origin,
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
    """授权清单（分页）：status=active/revoked/vetoed/all（默认 all），
    可按 user_id / workspace_id 过滤。含实时有效性判定与失效原因。"""
    user = _current_user()
    access.require_governance(user)
    # int 参数统一 400（#5：恶意参数不再 ValueError→500）
    page = max(1, access.int_or_400(request.args.get("page"), "page", default=1))
    page_size = min(100, max(1, access.int_or_400(
        request.args.get("page_size"), "page_size", default=20)))
    status = request.args.get("status", "all")

    query = WorkAccessGrant.query
    if status in ('active', 'revoked', 'vetoed'):
        query = query.filter(WorkAccessGrant.status == status)
    if request.args.get("user_id"):
        query = query.filter(WorkAccessGrant.user_id == access.int_or_400(
            request.args["user_id"], "user_id"))
    if request.args.get("workspace_id"):
        query = query.filter(WorkAccessGrant.workspace_id == access.int_or_400(
            request.args["workspace_id"], "workspace_id"))
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


@bp.route("/governance/grants", methods=["POST"])
@jwt_required()
@audit_log(operation="开通内部协作授权")
def governance_create_grant():
    """开通授权（§5.4）：依据现有任职/组归属 + 明确范围、有效期与事由。
    校验在 governance_service.validate_grant_payload（含 #7 来源组绑定）。"""
    user = _current_user()
    access.require_governance(user)
    data = request.get_json(silent=True) or {}
    fields = governance_service.validate_grant_payload(user, data)
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
    """撤销授权：即时生效（下一请求即失效，A05）；governance 岗位撤销仅超管。
    body {reason, veto?=bool}——veto=true 撤销并否决：该用户本工作区不再自动重授
    （授权自动化方案 §3.3，防退社再入社自动复活）。"""
    user = _current_user()
    access.require_governance(user)
    grant = WorkAccessGrant.query.get(gid)
    if not grant:
        return jsonify({"code": 404, "message": "授权不存在"}), 404
    if grant.status != 'active':
        return jsonify({"code": 409, "message": "该授权已撤销"}), 404
    if grant.role == 'governance' and not user.is_admin():
        return jsonify({"code": 403, "message": "治理岗位撤销仅超级管理员可以操作"}), 403
    data = request.get_json(silent=True) or {}
    reason = (str(data.get("reason") or "").strip())
    if not reason or len(reason) > 200:
        return jsonify({"code": 400, "message": "撤销原因必填且 ≤200 字"}), 400
    veto = bool(data.get("veto"))
    grant.status = 'vetoed' if veto else 'revoked'
    grant.revoke_reason = reason
    # 否决联动：同 (user, workspace) 其余在授自动行一并撤销（人被否决，不是某一行被否决）
    linked = 0
    if veto and grant.workspace_id:
        for g2 in WorkAccessGrant.query.filter(
                WorkAccessGrant.user_id == grant.user_id,
                WorkAccessGrant.workspace_id == grant.workspace_id,
                WorkAccessGrant.status == 'active',
                WorkAccessGrant.id != grant.id).all():
            g2.status = 'revoked'
            g2.revoke_reason = f"否决联动：{reason}"
            linked += 1
    create_notification(grant.user_id, "内部工作台",
                        "你的内部工作台协作权限已被撤销", category='work')
    db.session.commit()
    return jsonify({"code": 200, "message": (
        "已撤销并否决：该用户本工作区不会再自动获得授权" if veto
        else "授权已撤销（下一请求即失效）") + (f"；联动撤销自动授权 {linked} 条" if linked else "")})


@bp.route("/governance/grants/<int:gid>/unveto", methods=["POST"])
@jwt_required()
@audit_log(operation="解除授权否决")
def governance_unveto_grant(gid):
    """解除否决（授权自动化方案 §3.3）：vetoed → revoked，此后组织事实命中会再自动授；
    解除后立即按当前组织事实重算一次（人还在组里则当场恢复权限）。"""
    user = _current_user()
    access.require_governance(user)
    grant = WorkAccessGrant.query.get(gid)
    if not grant:
        return jsonify({"code": 404, "message": "授权不存在"}), 404
    if grant.status != 'vetoed':
        return jsonify({"code": 409, "message": "该授权不处于否决状态"}), 409
    grant.status = 'revoked'
    grant.revoke_reason = (grant.revoke_reason or '') + '；已解除否决'
    action = None
    if grant.workspace_id:
        action = provisioning_service.sync_user_workspace(
            grant.user_id, grant.workspace_id, user.id, event='解除否决')
    db.session.commit()
    return jsonify({"code": 200, "message": "已解除否决" + (
        "，并按当前组织事实重算" if action in ('granted', 'changed') else "（当前无组织事实命中，不自动恢复）")})


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
    # 所属工作区组名（管理端接管队列列）：批量解析，不在 service 逐行查
    gnames = {}
    ws_ids = list({r["workspace_id"] for r in rows if r.get("workspace_id")})
    if ws_ids:
        wss = {w.id: w for w in WorkWorkspace.query.filter(WorkWorkspace.id.in_(ws_ids)).all()}
        gmap = {g.id: g.name for g in ClubGroup.query.filter(
            ClubGroup.id.in_([w.club_group_id for w in wss.values()])).all()}
        gnames = {wid: gmap.get(w.club_group_id) for wid, w in wss.items()}
    for r in rows:
        r["assignee_name"] = names.get(r["assignee_user_id"])
        r["group_name"] = gnames.get(r.get("workspace_id"))
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
    target = UserModel.query.get(access.int_or_400(target_id, "user_id"))
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

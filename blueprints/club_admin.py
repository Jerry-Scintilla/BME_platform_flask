"""社团身份体系·后台配置蓝图（设计方案 §4.2）：组别 / 职位 / 归属 三组资源 + 组工作台端点。

组别 club_group：树状 CRUD。改名自由（id 键控零迁移）；挪父校验无环 + 深度 ≤4；
归档条件 = 无 active 任职 + 无归属（含 secondary）+ 无 active 子组；删除仅零引用。
职位 club_position：规则字段 CRUD（含 org_slot 类别 club=社团职务/group=组内职位，
migrate_70）。收紧规则时校验存量 active 任职不违例；退役条件 = 无 active 任职；删除仅零引用。
归属 club_membership：单人两槽编辑（primary/secondary 互斥同组禁止、组须 active）+ 批量导入（逐项回报）。
组工作台（2026-10-02 改版）：GET groups/<id>/detail 组维度聚合（组长槽/分管/成员表），
POST groups/<id>/leader 原子换组长（卸旧任新 + 可选同步主要归属，补设计方案 §3.5）。

权限与 officers 一致：system_management；写操作过 audit_log。
"""
from datetime import date

from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required

from exts import db
from models import UserModel, ClubGroup, ClubPosition, ClubOfficer, ClubMembership
from services.club_rules import (ORG_SLOTS, ORG_SLOT_GROUP, is_club_position,
                                 is_group_position, position_org_slot,
                                 default_leader_position_id)
from services.work import provisioning
from . import check_permission, audit_log, _current_user
from .officers import (_parse_term_start, _validate_appointment, _officer_dict)
from services.work.events import record_org_event

bp = Blueprint("club_admin", __name__, url_prefix="/admin/club")

MAX_DEPTH = 4
GROUP_RULES = ('forbidden', 'optional', 'required')


# ────────────────────────────────
# 组别
# ────────────────────────────────

def _depth_of(group):
    """沿 parent 上行算深度（L1=1）。环由挪入校验兜底，这里加步数保险。"""
    d, cur, guard = 1, group, 0
    while cur.parent_id and guard < 50:
        cur = ClubGroup.query.get(cur.parent_id)
        if not cur:
            break
        d += 1
        guard += 1
    return d


def _subtree_height(group):
    children = ClubGroup.query.filter_by(parent_id=group.id).all()
    return 1 + max((_subtree_height(c) for c in children), default=0)


def _group_refs(gid):
    return {
        "children": ClubGroup.query.filter_by(parent_id=gid).count(),
        "officers": ClubOfficer.query.filter_by(group_id=gid).count(),
        "members": ClubMembership.query.filter_by(group_id=gid).count(),
    }


def _group_dict(g):
    return {
        "id": g.id, "name": g.name, "parent_id": g.parent_id,
        "sort_order": g.sort_order, "status": g.status,
        "description": g.description or "",
        "refs": _group_refs(g.id),
    }


@bp.route("/groups", methods=["GET"])
@jwt_required()
@check_permission('system_management')
def list_groups():
    """组树平铺（前端按 parent_id 组树；archived 一并返回供管理，架构页另行过滤）。"""
    rows = ClubGroup.query.order_by(ClubGroup.sort_order, ClubGroup.id).all()
    return jsonify({"code": 200, "message": "获取组列表成功",
                    "data": {"groups": [_group_dict(g) for g in rows]}})


@bp.route("/groups", methods=["POST"])
@jwt_required()
@check_permission('system_management')
@audit_log(operation="新建社团组别")
def create_group():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name or len(name) > 50:
        return jsonify({"code": 400, "message": "组名必填且 ≤50 字"}), 400
    if ClubGroup.query.filter_by(name=name).first():
        return jsonify({"code": 409, "message": f"组名「{name}」已存在（全树唯一）"}), 409

    parent = None
    if data.get("parent_id"):
        parent = ClubGroup.query.get(int(data["parent_id"]))
        if not parent:
            return jsonify({"code": 404, "message": "父组不存在"}), 404
        if parent.status != 'active':
            return jsonify({"code": 400, "message": "父组已归档，不能在其下建组"}), 400
        if _depth_of(parent) >= MAX_DEPTH:
            return jsonify({"code": 400, "message": f"组层级上限 {MAX_DEPTH} 级"}), 400

    g = ClubGroup(name=name, parent_id=parent.id if parent else None,
                  sort_order=int(data.get("sort_order") or 0))
    db.session.add(g)
    db.session.commit()
    return jsonify({"code": 200, "message": f"已创建组「{name}」", "data": _group_dict(g)})


@bp.route("/groups/<int:gid>", methods=["PUT"])
@jwt_required()
@check_permission('system_management')
@audit_log(operation="编辑社团组别")
def update_group(gid):
    """改名 / 挪父 / 调序。挪父校验：无环、新位置深度装得下整个子树、父组 active。"""
    g = ClubGroup.query.get(gid)
    if not g:
        return jsonify({"code": 404, "message": "组不存在"}), 404
    data = request.get_json(silent=True) or {}

    if "name" in data:
        name = (data.get("name") or "").strip()
        if not name or len(name) > 50:
            return jsonify({"code": 400, "message": "组名必填且 ≤50 字"}), 400
        dup = ClubGroup.query.filter(ClubGroup.name == name, ClubGroup.id != gid).first()
        if dup:
            return jsonify({"code": 409, "message": f"组名「{name}」已存在（全树唯一）"}), 409
        g.name = name

    if "parent_id" in data:
        raw = data.get("parent_id")
        if raw is None:
            g.parent_id = None
        else:
            parent = ClubGroup.query.get(int(raw))
            if not parent:
                return jsonify({"code": 404, "message": "父组不存在"}), 404
            if parent.id == g.id:
                return jsonify({"code": 400, "message": "不能挂到自己名下"}), 400
            if parent.status != 'active':
                return jsonify({"code": 400, "message": "父组已归档"}), 400
            # 环检查：新父的上行链不能经过自己
            cur, guard = parent, 0
            while cur and guard < 50:
                if cur.id == g.id:
                    return jsonify({"code": 400, "message": "不能挂到自己的子孙组下（成环）"}), 400
                cur = ClubGroup.query.get(cur.parent_id) if cur.parent_id else None
                guard += 1
            if _depth_of(parent) + _subtree_height(g) - 1 > MAX_DEPTH:
                return jsonify({"code": 400, "message": f"挪动后超组层级上限 {MAX_DEPTH} 级"}), 400
            g.parent_id = parent.id

    if "sort_order" in data:
        try:
            g.sort_order = int(data.get("sort_order") or 0)
        except (TypeError, ValueError):
            return jsonify({"code": 400, "message": "sort_order 须为整数"}), 400

    # 小组介绍（组织页组态展示位）：≤500 字，空串=清空（回落占位文案）
    if "description" in data:
        desc = str(data.get("description") or "").strip()
        if len(desc) > 500:
            return jsonify({"code": 400, "message": "小组介绍至多 500 字"}), 400
        g.description = desc or None

    db.session.commit()
    return jsonify({"code": 200, "message": "组信息已更新", "data": _group_dict(g)})


@bp.route("/groups/<int:gid>/archive", methods=["POST"])
@jwt_required()
@check_permission('system_management')
@audit_log(operation="归档社团组别")
def archive_group(gid):
    """归档：架构页与选择器隐藏，行保留供历史徽标/档案解析。前置=清空在任与归属。"""
    g = ClubGroup.query.get(gid)
    if not g:
        return jsonify({"code": 404, "message": "组不存在"}), 404
    if g.status != 'active':
        return jsonify({"code": 400, "message": "该组已归档"}), 400
    refs = _group_refs(g.id)
    if refs["children"]:
        return jsonify({"code": 409, "message": "组下还有子组，请先处理子组"}), 409
    active_officers = ClubOfficer.query.filter_by(group_id=gid, status='active').count()
    if active_officers:
        return jsonify({"code": 409, "message": f"组内还有 {active_officers} 名在任干事，请先卸任"}), 409
    if refs["members"]:
        return jsonify({"code": 409, "message": f"组内还有 {refs['members']} 名成员归属，请先迁出"}), 409
    # 内部工作台（方案 §5.4）：组工作区仍有未完成事项时提醒先处理/移交
    from models import WorkItem, WorkWorkspace
    ws = WorkWorkspace.query.filter_by(club_group_id=gid).first()
    if ws:
        open_items = WorkItem.query.filter(
            WorkItem.workspace_id == ws.id,
            WorkItem.status.in_(('draft', 'open', 'todo', 'in_progress', 'blocked', 'review'))).count()
        if open_items:
            return jsonify({"code": 409,
                            "message": f"该组工作区还有 {open_items} 项未完成工作事项，请先完结或移交再归档"}), 409
    g.status = 'archived'
    current = _current_user()
    record_org_event('group_archived', to_group_id=gid,
                     operator_id=current.id if current else None,
                     detail={'name': g.name})
    db.session.commit()
    return jsonify({"code": 200, "message": f"已归档「{g.name}」（记录保留）", "data": _group_dict(g)})


@bp.route("/groups/<int:gid>", methods=["DELETE"])
@jwt_required()
@check_permission('system_management')
@audit_log(operation="删除社团组别")
def delete_group(gid):
    """删除仅零引用（ended 任职/历史归属都算引用——历史档案要能解析组名）。"""
    g = ClubGroup.query.get(gid)
    if not g:
        return jsonify({"code": 404, "message": "组不存在"}), 404
    refs = _group_refs(g.id)
    if any(refs.values()):
        return jsonify({"code": 409, "message": "组仍有引用（子组/任职/归属），请改用归档",
                        "data": refs}), 409
    db.session.delete(g)
    db.session.commit()
    return jsonify({"code": 200, "message": f"已删除组「{g.name}」"})


# ────────────────────────────────
# 职位
# ────────────────────────────────

def _pos_dict(p):
    return {
        "id": p.id, "name": p.name, "org_slot": position_org_slot(p),
        "sort_rank": p.sort_rank,
        "badge_tier": p.badge_tier, "badge_with_group": bool(p.badge_with_group),
        "group_rule": p.group_rule, "per_group_limit": p.per_group_limit,
        "global_limit": p.global_limit, "status": p.status,
        "active_count": ClubOfficer.query.filter_by(title_id=p.id, status='active').count(),
    }


def _parse_position_payload(data, current=None):
    """校验职位字段，返回 (字段 dict, 错误响应)。current 用于收紧规则时排除自身场景无需。"""
    fields = {}
    if "name" in data or current is None:
        name = (data.get("name") or "").strip()
        if not name or len(name) > 30:
            return None, (jsonify({"code": 400, "message": "职位名必填且 ≤30 字"}), 400)
        dup = ClubPosition.query.filter(ClubPosition.name == name).first()
        if dup and (current is None or dup.id != current.id):
            return None, (jsonify({"code": 409, "message": f"职位「{name}」已存在"}), 409)
        fields["name"] = name
    for key, cast in (("sort_rank", int), ("badge_tier", int),
                      ("per_group_limit", int), ("global_limit", int)):
        if key in data:
            try:
                fields[key] = cast(data.get(key))
            except (TypeError, ValueError):
                return None, (jsonify({"code": 400, "message": f"{key} 须为整数"}), 400)
    if "badge_tier" in fields and fields["badge_tier"] not in (1, 2, 3):
        return None, (jsonify({"code": 400, "message": "badge_tier 仅支持 1/2/3"}), 400)
    for key in ("sort_rank", "per_group_limit", "global_limit"):
        if key in fields and fields[key] < 0:
            return None, (jsonify({"code": 400, "message": f"{key} 不能为负"}), 400)
    if "group_rule" in data:
        if data["group_rule"] not in GROUP_RULES:
            return None, (jsonify({"code": 400,
                                   "message": f"group_rule 仅支持 {'/'.join(GROUP_RULES)}"}), 400)
        fields["group_rule"] = data["group_rule"]
    if "org_slot" in data:
        if data["org_slot"] not in ORG_SLOTS:
            return None, (jsonify({"code": 400,
                                   "message": f"org_slot 仅支持 {'/'.join(ORG_SLOTS)}（缺省按 sort_rank 派生）"}), 400)
        fields["org_slot"] = data["org_slot"]
    if "badge_with_group" in data:
        fields["badge_with_group"] = bool(data["badge_with_group"])
    # 组内职位（组长类）天然挂在组上，与 forbidden 自相矛盾
    merged_rule = fields.get("group_rule", current.group_rule if current else 'optional')
    merged_slot = fields.get("org_slot", current.org_slot if current else None)
    if merged_slot == ORG_SLOT_GROUP and merged_rule == 'forbidden':
        return None, (jsonify({"code": 400,
                               "message": "组内职位不能设为不挂组（forbidden）"}), 400)
    return fields, None


def _check_tightening(p):
    """收紧规则后校验存量 active 任职不违例，返回错误响应或 None。"""
    actives = ClubOfficer.query.filter_by(title_id=p.id, status='active').all()
    for o in actives:
        if p.group_rule == 'forbidden' and o.group_id:
            return jsonify({"code": 409, "message": f"存量在任 #{o.id} 已挂组，与 forbidden 冲突，请先调整任职"}), 409
        if p.group_rule == 'required' and not o.group_id:
            return jsonify({"code": 409, "message": f"存量在任 #{o.id} 未挂组，与 required 冲突，请先调整任职"}), 409
    if p.global_limit:
        if len(actives) > p.global_limit:
            return jsonify({"code": 409,
                            "message": f"在任 {len(actives)} 人超 global_limit={p.global_limit}，请先卸任"}), 409
    if p.per_group_limit:
        from collections import Counter
        per = Counter(o.group_id for o in actives)
        bad = {g: n for g, n in per.items() if n > p.per_group_limit}
        if bad:
            return jsonify({"code": 409,
                            "message": f"存在同组在任超 per_group_limit={p.per_group_limit} 的组，请先卸任"}), 409
    return None


@bp.route("/positions", methods=["GET"])
@jwt_required()
@check_permission('system_management')
def list_positions():
    rows = ClubPosition.query.order_by(ClubPosition.sort_rank, ClubPosition.id).all()
    return jsonify({"code": 200, "message": "获取职位列表成功",
                    "data": {"positions": [_pos_dict(p) for p in rows]}})


@bp.route("/positions", methods=["POST"])
@jwt_required()
@check_permission('system_management')
@audit_log(operation="新建社团职位")
def create_position():
    data = request.get_json(silent=True) or {}
    fields, err = _parse_position_payload(data)
    if err:
        return err
    p = ClubPosition(**{
        "name": fields["name"],
        "org_slot": fields.get("org_slot"),          # 缺省 None=按 sort_rank 派生
        "sort_rank": fields.get("sort_rank", 99),
        "badge_tier": fields.get("badge_tier", 3),
        "badge_with_group": fields.get("badge_with_group", False),
        "group_rule": fields.get("group_rule", 'optional'),
        "per_group_limit": fields.get("per_group_limit", 1),
        "global_limit": fields.get("global_limit", 0),
    })
    db.session.add(p)
    db.session.commit()
    return jsonify({"code": 200, "message": f"已创建职位「{p.name}」", "data": _pos_dict(p)})


@bp.route("/positions/<int:pid>", methods=["PUT"])
@jwt_required()
@check_permission('system_management')
@audit_log(operation="编辑社团职位")
def update_position(pid):
    p = ClubPosition.query.get(pid)
    if not p:
        return jsonify({"code": 404, "message": "职位不存在"}), 404
    data = request.get_json(silent=True) or {}
    fields, err = _parse_position_payload(data, current=p)
    if err:
        return err
    for k, v in fields.items():
        setattr(p, k, v)
    tightening = _check_tightening(p)
    if tightening:
        db.session.rollback()
        return tightening
    db.session.commit()
    return jsonify({"code": 200, "message": "职位信息已更新", "data": _pos_dict(p)})


@bp.route("/positions/<int:pid>/retire", methods=["POST"])
@jwt_required()
@check_permission('system_management')
@audit_log(operation="退役社团职位")
def retire_position(pid):
    """退役：不再可任命；前置=清空在任。存量 ended 任职行不受影响（历史档案仍解析）。"""
    p = ClubPosition.query.get(pid)
    if not p:
        return jsonify({"code": 404, "message": "职位不存在"}), 404
    if p.status != 'active':
        return jsonify({"code": 400, "message": "该职位已退役"}), 400
    active = ClubOfficer.query.filter_by(title_id=pid, status='active').count()
    if active:
        return jsonify({"code": 409, "message": f"还有 {active} 名在任，请先卸任"}), 409
    p.status = 'retired'
    db.session.commit()
    return jsonify({"code": 200, "message": f"已退役「{p.name}」（不可再任命）", "data": _pos_dict(p)})


@bp.route("/positions/<int:pid>", methods=["DELETE"])
@jwt_required()
@check_permission('system_management')
@audit_log(operation="删除社团职位")
def delete_position(pid):
    p = ClubPosition.query.get(pid)
    if not p:
        return jsonify({"code": 404, "message": "职位不存在"}), 404
    refs = ClubOfficer.query.filter_by(title_id=pid).count()
    if refs:
        return jsonify({"code": 409, "message": "该职位仍有任职记录（含历史），请改用退役"}), 409
    db.session.delete(p)
    db.session.commit()
    return jsonify({"code": 200, "message": f"已删除职位「{p.name}」"})


# ────────────────────────────────
# 归属
# ────────────────────────────────

def _resolve_slot_group(raw, field):
    """slot 值 → ClubGroup 或 None（null=清除）；校验存在且 active。"""
    if raw is None:
        return None
    g = ClubGroup.query.get(int(raw))
    if not g:
        return jsonify({"code": 404, "message": f"{field} 组不存在"}), 404
    if g.status != 'active':
        return jsonify({"code": 400, "message": f"{field} 组已归档"}), 400
    return g


@bp.route("/membership/<int:user_id>", methods=["GET"])
@jwt_required()
@check_permission('system_management')
def get_membership(user_id):
    user = UserModel.query.get(user_id)
    if not user:
        return jsonify({"code": 404, "message": "用户不存在"}), 404
    rows = ClubMembership.query.filter_by(user_id=user_id).all()
    data = {"primary": None, "secondary": None}
    for m in rows:
        g = ClubGroup.query.get(m.group_id)
        # membership_id：归属行 id（内部工作台授权的来源行依据；id 仍为组 id，兼容旧 UI）
        data[m.slot] = ({"id": g.id, "name": g.name, "membership_id": m.id}
                        if g else None)
    return jsonify({"code": 200, "message": "获取归属成功", "data": data})


def _apply_membership_slots(user_id, slots, operator_id, event='归属变更'):
    """槽位写入核心（set_membership 与组工作台 leader 端点共用）。

    slots 只含要动的槽：{slot: ClubGroup|None}（None=清除该槽）。含两槽互斥校验
    （传入槽之间 + 与未传入的现有槽）、覆盖写、joined_at 刷新、组织事件留痕、
    授权联动（§3.2）；只写 session 不 commit，由调用方提交。
    校验不过抛 ValueError（中文消息）；返回 org_changes（空列表=无变化）。"""
    if ("primary" in slots and "secondary" in slots
            and slots["primary"] and slots["secondary"]
            and slots["primary"].id == slots["secondary"].id):
        raise ValueError("主要组与次要组不能是同一个组")
    existing = {m.slot: m for m in ClubMembership.query.filter_by(user_id=user_id).all()}
    other_names = {"primary": "次要组", "secondary": "主要组"}
    for field, g in slots.items():
        other = "secondary" if field == "primary" else "primary"
        if (g and other in existing and existing[other]
                and existing[other].group_id == g.id and other not in slots):
            raise ValueError(f"与现有{other_names[field]}相同，不能同组")

    # 组织变更留痕（内部工作台 §3.3）：归属原地覆盖无历史，变更前后同事务落事件
    org_changes = []
    for field, g in slots.items():
        row = existing.get(field)
        old_gid = row.group_id if row else None
        new_gid = g.id if g else None
        if old_gid != new_gid:
            org_changes.append({'slot': field, 'membership_id': row.id if row else None,
                                'from_group_id': old_gid, 'to_group_id': new_gid})

    for field, g in slots.items():
        row = existing.get(field)
        if g is None:
            if row:
                db.session.delete(row)
        elif row:
            row.group_id = g.id
            row.joined_at = date.today()
        else:
            db.session.add(ClubMembership(user_id=user_id, group_id=g.id,
                                          slot=field, joined_at=date.today()))
    if org_changes:
        first = org_changes[0]
        record_org_event('membership_set', user_id=user_id,
                         membership_id=first['membership_id'],
                         from_group_id=first['from_group_id'],
                         to_group_id=first['to_group_id'],
                         operator_id=operator_id,
                         detail={'changes': org_changes})
        # 授权自动化：归属变更即派生/迁移授权（旧组新组各重算，同事务，方案 §3.2）
        gids = [c[k] for c in org_changes for k in ('from_group_id', 'to_group_id')]
        provisioning.sync_after_membership(user_id, gids, operator_id, event=event)
    return org_changes


@bp.route("/membership/<int:user_id>", methods=["PUT"])
@jwt_required()
@check_permission('system_management')
@audit_log(operation="设置社团归属")
def set_membership(user_id):
    """单人两槽编辑：body {primary?: group_id|null, secondary?: group_id|null}。
    只动传入的槽；两槽同组拒绝；组须 active。"""
    user = UserModel.query.get(user_id)
    if not user:
        return jsonify({"code": 404, "message": "用户不存在"}), 404
    data = request.get_json(silent=True) or {}

    slots = {}
    for field in ("primary", "secondary"):
        if field not in data:
            continue
        res = _resolve_slot_group(data.get(field), field)
        if res is None:
            slots[field] = None
        elif isinstance(res, tuple):          # 错误响应
            return res
        else:
            slots[field] = res

    try:
        _apply_membership_slots(user_id, slots,
                                _current_user().id if _current_user() else None)
    except ValueError as e:
        return jsonify({"code": 400, "message": str(e)}), 400
    db.session.commit()
    return jsonify({"code": 200, "message": f"已更新 {user.username} 的组归属"})


@bp.route("/membership/batch", methods=["POST"])
@jwt_required()
@check_permission('system_management')
@audit_log(operation="批量设置社团归属")
def batch_membership():
    """批量导入（预留接口）：items [{user_id|username, primary?, secondary?}]，逐项回报。"""
    data = request.get_json(silent=True) or {}
    items = data.get("items")
    if not isinstance(items, list) or not items:
        return jsonify({"code": 400, "message": "缺少 items 数组"}), 400
    if len(items) > 200:
        return jsonify({"code": 400, "message": "单批至多 200 条"}), 400

    results, ok, rejected = [], 0, 0
    org_changes = []          # 组织变更留痕（内部工作台 §3.3）：批量逐项收集，一次落事件
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            results.append({"index": idx, "ok": False, "reason": "条目格式错误"})
            rejected += 1
            continue
        if item.get("user_id"):
            user = UserModel.query.get(int(item["user_id"]))
        elif item.get("username"):
            user = UserModel.query.filter_by(username=(item["username"] or "").strip()).first()
        else:
            user = None
        if not user:
            results.append({"index": idx, "ok": False, "reason": "用户不存在"})
            rejected += 1
            continue

        payload = {}
        if "primary" in item:
            payload["primary"] = item["primary"]
        if "secondary" in item:
            payload["secondary"] = item["secondary"]
        # 复用单人逻辑：内部走请求上下文外的直接调用
        slots = {}
        bad = None
        for field in ("primary", "secondary"):
            if field not in payload:
                continue
            res = _resolve_slot_group(payload.get(field), field)
            if res is None:
                slots[field] = None
            elif isinstance(res, tuple):
                bad = res
                break
            else:
                slots[field] = res
        if bad:
            results.append({"index": idx, "ok": False, "reason": bad[0].get_json()["message"],
                            "username": user.username})
            rejected += 1
            continue
        if ("primary" in slots and "secondary" in slots and slots["primary"] and slots["secondary"]
                and slots["primary"].id == slots["secondary"].id):
            results.append({"index": idx, "ok": False, "reason": "两槽不能同组",
                            "username": user.username})
            rejected += 1
            continue

        existing = {m.slot: m for m in ClubMembership.query.filter_by(user_id=user.id).all()}
        item_changes = []
        for field, g in slots.items():
            row = existing.get(field)
            old_gid = row.group_id if row else None
            new_gid = g.id if g else None
            if old_gid != new_gid:
                item_changes.append({'slot': field, 'membership_id': row.id if row else None,
                                     'from_group_id': old_gid, 'to_group_id': new_gid})
            if g is None:
                if row:
                    db.session.delete(row)
            elif row:
                row.group_id = g.id
                row.joined_at = date.today()
            else:
                db.session.add(ClubMembership(user_id=user.id, group_id=g.id,
                                              slot=field, joined_at=date.today()))
        if item_changes:
            org_changes.append({'user_id': user.id, 'changes': item_changes})
        results.append({"index": idx, "ok": True, "username": user.username})
        ok += 1

    if org_changes:
        current = _current_user()
        record_org_event('membership_batch',
                         operator_id=current.id if current else None,
                         detail={'items': org_changes})
        # 授权自动化：批量归属逐人重算（同事务）
        for item in org_changes:
            gids = [c[k] for c in item['changes'] for k in ('from_group_id', 'to_group_id')]
            provisioning.sync_after_membership(item['user_id'], gids,
                                               current.id if current else None, event='批量归属')
    db.session.commit()
    return jsonify({"code": 200, "message": f"批量归属完成：{ok} 成功 / {rejected} 拒绝",
                    "data": {"results": results, "ok": ok, "rejected": rejected}})


# ────────────────────────
# 组工作台（2026-10-02 改版：组为中心管理）
# ────────────────────────

def _member_row(u, m, slot, title, officer_id=0, is_leader=False):
    """组工作台成员行：m 为 ClubMembership 行（组长无归属行时为 None，joined_at 空）。"""
    from .media import public_avatar_url
    return {
        "user_id": u.id, "username": u.username,
        "avatar": public_avatar_url(u.avatar_url) if u.avatar_url else None,
        "slot": slot, "title": title, "is_leader": is_leader,
        "officer_id": officer_id,
        "joined_at": m.joined_at.isoformat() if (m and m.joined_at) else None,
    }


@bp.route("/groups/<int:gid>/detail", methods=["GET"])
@jwt_required()
@check_permission('system_management')
def group_detail(gid):
    """组工作台聚合：组信息 + 组长类职位槽位（含空缺）+ 分管位 + 成员表（组长置顶）。
    归档组可读（管理口径，操作由前端禁用）。"""
    g = ClubGroup.query.get(gid)
    if not g:
        return jsonify({"code": 404, "message": "组不存在"}), 404

    positions = {p.id: p for p in ClubPosition.query.filter_by(status='active').all()}
    officers = ClubOfficer.query.filter_by(group_id=gid, status='active').all()
    memberships = ClubMembership.query.filter_by(group_id=gid).all()

    user_ids = ({o.user_id for o in officers} | {m.user_id for m in memberships})
    users = ({u.id: u for u in UserModel.query.filter(UserModel.id.in_(user_ids)).all()}
             if user_ids else {})

    # 组长类槽位：org_slot=group 的 active 职位按 rank 升序，附该组在任者（含空缺）
    officers_by_title = {}
    for o in officers:
        officers_by_title.setdefault(o.title_id, []).append(o)
    group_positions = sorted((p for p in positions.values() if is_group_position(p)),
                             key=lambda p: (p.sort_rank, p.id))
    leader_slots = []
    leader_holders = []        # (position, officer 行) rank 序，供成员表置顶
    for p in group_positions:
        holders = sorted(officers_by_title.get(p.id, []), key=lambda o: o.id)
        leader_slots.append({
            "position": {"id": p.id, "name": p.name, "org_slot": ORG_SLOT_GROUP,
                         "sort_rank": p.sort_rank, "badge_tier": p.badge_tier,
                         "per_group_limit": p.per_group_limit},
            "officers": [_officer_dict(o, users.get(o.user_id), positions) for o in holders],
            "vacant": not holders,
        })
        leader_holders.extend((p, o) for o in holders)

    # 分管位：社团职务（club 类）挂本组的在任行
    overseers = [_officer_dict(o, users.get(o.user_id), positions)
                 for o in sorted(officers, key=lambda o: (
                     positions[o.title_id].sort_rank if o.title_id in positions else 99, o.id))
                 if o.title_id in positions and is_club_position(positions[o.title_id])]

    # 成员表：组长类在任者置顶（无归属行也补位，与组织页口径一致）+ 归属行（primary 前）
    title_by_user = {}
    for o in ClubOfficer.query.filter_by(status='active').all():
        pos = positions.get(o.title_id)
        if pos and o.user_id not in title_by_user:
            title_by_user[o.user_id] = pos.name
    member_rows = []
    seen = set()
    for p, o in leader_holders:
        u = users.get(o.user_id)
        if not u or u.id in seen:
            continue
        seen.add(u.id)
        member_rows.append(_member_row(u, None, "primary", p.name, o.id, True))
    for m in sorted(memberships, key=lambda m: (0 if m.slot == 'primary' else 1, m.id)):
        u = users.get(m.user_id)
        if not u or u.id in seen:
            continue
        seen.add(u.id)
        member_rows.append(_member_row(u, m, m.slot, title_by_user.get(u.id)))

    counts = {
        "primary": sum(1 for m in memberships if m.slot == 'primary'),
        "secondary": sum(1 for m in memberships if m.slot == 'secondary'),
        "total": len(member_rows),
    }
    return jsonify({"code": 200, "message": "获取组详情成功", "data": {
        "group": _group_dict(g),
        "leader_slots": leader_slots,
        "overseers": overseers,
        "members": member_rows,
        "counts": counts,
        "default_position_id": default_leader_position_id(),
    }})


@bp.route("/groups/<int:gid>/leader", methods=["POST"])
@jwt_required()
@check_permission('system_management')
@audit_log(operation="组工作台任命组长")
def appoint_group_leader(gid):
    """原子换组长：body {user_id, position_id?, term_start?, sync_primary?=true, end_reason?}。

    position_id 缺省=组内职位 sort_rank 最小者（组长类主职）；先卸该 (职位,组) 现任
    （end_reason 默认「组长更替」，留痕照旧）再任新，校验复用 officers 语义
    （现任行豁免——本人连任即任期重置）；sync_primary=true 时同步把主要归属迁到
    本组（设计方案 §3.5 欠账；次要槽已占本组时自动让位清除）。"""
    g = ClubGroup.query.get(gid)
    if not g:
        return jsonify({"code": 404, "message": "组不存在"}), 404
    if g.status != 'active':
        return jsonify({"code": 400, "message": "组已归档，不能任命组长"}), 400
    data = request.get_json(silent=True) or {}
    user = UserModel.query.get(int(data.get("user_id") or 0))
    if not user:
        return jsonify({"code": 404, "message": "用户不存在"}), 404

    # 职位解析：显式传入须组内职位；缺省取组长类主职
    if data.get("position_id"):
        pos = ClubPosition.query.get(int(data["position_id"]))
        if not pos or pos.status != 'active':
            return jsonify({"code": 404, "message": "职位不存在或已退役"}), 404
        if not is_group_position(pos):
            return jsonify({"code": 400, "message": f"{pos.name} 是社团职务，不是组内职位"}), 400
    else:
        pid = default_leader_position_id()
        if not pid:
            return jsonify({"code": 400, "message": "尚未定义组内职位，请先在「职位定义」创建"}), 400
        pos = ClubPosition.query.get(pid)
    if pos.group_rule == 'forbidden':
        return jsonify({"code": 400, "message": f"{pos.name} 配置为不挂组，与组长任命冲突"}), 400

    term_start, terr = _parse_term_start(data.get("term_start"))
    if terr:
        return jsonify({"code": 400, "message": terr}), 400

    current = _current_user()
    # D5 留白入口（P2-8）：任职属人员业务写——merged 账号不再新增任职（与 appoint_officer 同款）
    from services.identity import enforcement as _enf
    _ok, _why = _enf.guard_business_write(user, 'club_officer')
    if not _ok:
        return jsonify({"code": 409, "message": _why}), 409

    incumbents = (ClubOfficer.query.filter_by(
        title_id=pos.id, group_id=gid, status='active').order_by(ClubOfficer.id).all())

    # 校验先行（现任行豁免：即将被卸任/本人连任），失败不落任何变更
    verr = _validate_appointment(user.id, pos, g, exclude_ids=[i.id for i in incumbents])
    if verr:
        return jsonify({"code": verr[0], "message": verr[1]}), verr[0]

    # 卸现任（状态化留痕）→ 授权联动逐个重算
    ended_dict = None
    for inc in incumbents:
        inc.status = 'ended'
        inc.term_end = date.today()
        inc.end_reason = (str(data.get("end_reason") or "").strip() or '组长更替')
        inc.ended_by = current.id if current else None
        record_org_event('officer_ended', user_id=inc.user_id, officer_id=inc.id,
                         from_group_id=gid, operator_id=current.id if current else None,
                         detail={'term_end': str(inc.term_end), 'reason': inc.end_reason})
        provisioning.sync_after_officer(inc.user_id, gid, None,
                                        current.id if current else None, event='组长更替')
        if ended_dict is None:
            ended_dict = _officer_dict(inc, UserModel.query.get(inc.user_id), {pos.id: pos})

    officer = ClubOfficer(
        user_id=user.id,
        title_id=pos.id, title=pos.name,
        group_id=gid, department=g.name,
        term_start=term_start, status='active',
        appointed_by=current.id if current else None,
    )
    db.session.add(officer)
    db.session.flush()          # 拿 id 供组织事件留痕
    record_org_event('officer_appointed', user_id=user.id, officer_id=officer.id,
                     to_group_id=gid, operator_id=current.id if current else None,
                     detail={'title': pos.name, 'via': 'group_workspace'})
    provisioning.sync_after_officer(user.id, None, gid,
                                    current.id if current else None, event='组长更替')

    # §3.5 便利默认：同步把主要归属迁到本组（次要槽撞本组时让位清除）
    primary_moved = False
    if data.get("sync_primary", True):
        existing = {m.slot: m for m in ClubMembership.query.filter_by(user_id=user.id).all()}
        if existing.get("primary") is None or existing["primary"].group_id != gid:
            slots = {"primary": g}
            if existing.get("secondary") and existing["secondary"].group_id == gid:
                slots["secondary"] = None
            try:
                primary_moved = bool(_apply_membership_slots(
                    user.id, slots, current.id if current else None, event='组长就任'))
            except ValueError as e:
                db.session.rollback()
                return jsonify({"code": 400,
                                "message": f"同步主要归属失败：{e}"}), 400

    db.session.commit()
    return jsonify({"code": 200, "message": f"已任命 {user.username} 为 {pos.name} · {g.name}",
                    "data": {"leader": _officer_dict(officer, user, {pos.id: pos}),
                             "ended": ended_dict, "primary_moved": primary_moved}})

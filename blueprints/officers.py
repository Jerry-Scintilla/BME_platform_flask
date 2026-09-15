"""社团干事身份蓝图（v1.2 职位/组别解耦版，设计方案 docs/社团身份体系-设计方案.md）。

admin 侧 CRUD：任命 / 编辑 / 卸任（状态化不删行）/ 批量导入（逐项回报）。
校验不再硬编码：全部读 ClubPosition 规则字段——
  group_rule（forbidden 职位禁挂组 / required 必挂组）
  per_group_limit（同组同时在任上限，0=不限）
  global_limit（全社同时在任上限，0=不限）
  一人至多 1 条 active 任职（原「兼两组」由 club_membership 两槽承接，「组员」头衔已退役）
兼容：入参 title/department 传名（旧 admin UI）或 title_id/group_id 传 id 均可；
出参双份（title/title_id、department/group_id）；title/department 列为双写冗余，Phase C 退役。

user 侧不设端点：user_index / profile/<id> 回包直接附任职与归属（见 user.py），
社区 feed 的 author_badge 见 community.py——三者都 import 本文件的查询助手。
"""
from datetime import date, datetime

from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required

from exts import db
from models import UserModel, ClubOfficer, ClubPosition, ClubGroup, ClubMembership
from . import check_permission, audit_log, _current_user

bp = Blueprint("officers", __name__, url_prefix="/admin/officers")

# 管理层阈值（组织架构页顶部区判定，与 organization.py 同源）
MANAGEMENT_RANK_MAX = 9

from .media import public_avatar_url as _avatar_url     # 新链路 /media/，旧值兜底 /data/avatars/


# ────────────────────────────────
# 解析助手（名/id 双轨入参）
# ────────────────────────────────

def _resolve_position(data):
    """title_id 优先，否则按 title 名精确匹配 active 职位。返回 (职位, 错误响应)。"""
    if data.get("title_id"):
        pos = ClubPosition.query.get(int(data["title_id"]))
        if pos and pos.status == 'active':
            return pos, None
        return None, (jsonify({"code": 404, "message": "职位不存在或已退役"}), 404)
    name = (data.get("title") or "").strip()
    if not name:
        return None, (jsonify({"code": 400, "message": "缺少职位"}), 400)
    pos = ClubPosition.query.filter_by(name=name, status='active').first()
    if not pos:
        return None, (jsonify({"code": 404, "message": f"职位「{name}」不存在或已退役"}), 404)
    return pos, None


def _resolve_group(data):
    """group_id 优先，否则按 department 名匹配 active 组。返回 (组|None, 错误响应)。
    None（含入参 null/空串）= 不挂组——是否合法交给 group_rule 判断。"""
    raw_id, raw_name = data.get("group_id"), data.get("department")
    if raw_id:
        g = ClubGroup.query.get(int(raw_id))
        if not g or g.status != 'active':
            return None, (jsonify({"code": 404, "message": "组不存在或已归档"}), 404)
        return g, None
    if raw_name is not None:
        name = str(raw_name).strip()
        if not name:
            return None, None
        if len(name) > 50:
            return None, (jsonify({"code": 400, "message": "组名过长（≤50 字）"}), 400)
        g = ClubGroup.query.filter_by(name=name, status='active').first()
        if not g:
            return None, (jsonify({"code": 404, "message": f"组「{name}」不存在（组树见后台配置）"}), 404)
        return g, None
    return None, None


# ────────────────────────────────
# 校验（任命与编辑共用）
# ────────────────────────────────

def _validate_appointment(user_id, pos, group, exclude_id=None):
    """读职位规则校验，通过返回 None，否则返回 (错误码, 中文原因)。"""
    # group_rule
    if pos.group_rule == 'forbidden' and group:
        return 400, f"{pos.name}不挂组"
    if pos.group_rule == 'required' and not group:
        return 400, f"{pos.name}必须归属一个组"

    # 一人至多 1 条 active
    mine = ClubOfficer.query.filter(
        ClubOfficer.user_id == user_id,
        ClubOfficer.status == 'active',
    )
    if exclude_id:
        mine = mine.filter(ClubOfficer.id != exclude_id)
    if mine.first():
        return 409, "该成员已有在任职位（一人至多一职）"

    # per_group_limit：同职位同组同时在任
    if pos.per_group_limit:
        q1 = ClubOfficer.query.filter(
            ClubOfficer.title_id == pos.id,
            ClubOfficer.group_id == (group.id if group else None),
            ClubOfficer.status == 'active',
        )
        if exclude_id:
            q1 = q1.filter(ClubOfficer.id != exclude_id)
        dup = q1.first()
        if dup:
            holder = UserModel.query.get(dup.user_id)
            holder_name = holder.username if holder else f"#{dup.user_id}"
            where = f"{group.name}·" if group else ""
            return 409, f"{where}{pos.name} 已由 {holder_name} 在任，请先卸任再任命"

    # global_limit：全社同时在任
    if pos.global_limit:
        q2 = ClubOfficer.query.filter(
            ClubOfficer.title_id == pos.id,
            ClubOfficer.status == 'active',
        )
        if exclude_id:
            q2 = q2.filter(ClubOfficer.id != exclude_id)
        if q2.count() >= pos.global_limit:
            return 409, f"{pos.name} 编制已满（{pos.global_limit} 名）"

    return None


def _parse_term_start(raw, fallback=None):
    """任期起解析，非法返回 (None, 错误信息)。"""
    if not raw:
        return (fallback or date.today()), None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date(), None
    except (TypeError, ValueError):
        return None, "任期起格式应为 YYYY-MM-DD"


# ────────────────────────────────
# 查询助手（user.py / community.py / organization.py 复用）
# ────────────────────────────────

def _rank_map():
    return {p.id: p for p in ClubPosition.query.all()}


def officers_by_user(user_ids):
    """批量查 active 任职 {uid: [行]}，按职位 rank + 任期起排序（主职在前）。空入参返回 {}。"""
    ids = [i for i in {u for u in user_ids if u} if i is not None]
    if not ids:
        return {}
    rows = ClubOfficer.query.filter(
        ClubOfficer.user_id.in_(ids),
        ClubOfficer.status == 'active',
    ).all()
    ranks = _rank_map()
    result = {}
    for r in rows:
        result.setdefault(r.user_id, []).append(r)
    for rows_of_user in result.values():
        rows_of_user.sort(key=lambda r: (
            ranks[r.title_id].sort_rank if r.title_id in ranks else 99,
            r.term_start or date.min, r.id))
    return result


def badge_map(user_ids):
    """社区 feed 徽章：{uid: {text, tier}}。
    优先级 = 在任职位（text=职位名[·组名]，tier=badge_tier，组段由 badge_with_group 决定）；
    无任职 → 主要组组名（tier 3）；未分组无键。"""
    result = {}
    ranks = _rank_map()
    for uid, rows in officers_by_user(user_ids).items():
        r = rows[0]
        pos = ranks.get(r.title_id)
        if not pos:
            continue
        text = pos.name
        if pos.badge_with_group and r.group_id:
            g = ClubGroup.query.get(r.group_id)
            if g:
                text = f"{pos.name} · {g.name}"
        result[uid] = {"text": text, "tier": pos.badge_tier}
    missing = [u for u in user_ids if u and u not in result]
    if missing:
        prim = {m.user_id: m for m in ClubMembership.query.filter(
            ClubMembership.user_id.in_(missing), ClubMembership.slot == 'primary').all()}
        gids = {m.group_id for m in prim.values()}
        gnames = {g.id: g.name for g in ClubGroup.query.filter(ClubGroup.id.in_(gids)).all()} if gids else {}
        for uid in missing:
            m = prim.get(uid)
            if m and m.group_id in gnames:
                result[uid] = {"text": gnames[m.group_id], "tier": 3}
    return result


# 兼容别名（过渡期社区 feed 未切结构前仍取纯文本）
def primary_title_map(user_ids):
    return {uid: b["text"] for uid, b in badge_map(user_ids).items()}


def public_officers(user_id):
    """单用户 active 任职的公开形态（user_index / profile 回包附字段）。"""
    return [
        {"title": r.title, "department": r.department,
         "term_start": r.term_start.isoformat() if r.term_start else None}
        for r in officers_by_user([user_id]).get(user_id, [])
    ]


def public_groups(user_id):
    """单用户组归属公开形态：{primary: 组名|None, secondary: 组名|None}。"""
    rows = ClubMembership.query.filter_by(user_id=user_id).all()
    gids = {m.group_id for m in rows}
    gnames = {g.id: g.name for g in ClubGroup.query.filter(ClubGroup.id.in_(gids)).all()} if gids else {}
    data = {"primary": None, "secondary": None}
    for m in rows:
        data[m.slot] = gnames.get(m.group_id)
    return data


def _officer_dict(o, user=None):
    """admin 列表行形态（含治理字段；名/id 双份）"""
    return {
        "id": o.id,
        "user_id": o.user_id,
        "username": user.username if user else "已注销用户",
        "avatar": _avatar_url(user.avatar_url) if user else "",
        "title": o.title,
        "title_id": o.title_id,
        "department": o.department,
        "group_id": o.group_id,
        "term_start": o.term_start.isoformat() if o.term_start else None,
        "term_end": o.term_end.isoformat() if o.term_end else None,
        "status": o.status,
        "end_reason": o.end_reason,
        "created_at": o.created_at.isoformat() if o.created_at else None,
    }


# ────────────────────────────────
# admin 端点
# ────────────────────────────────

@bp.route("", methods=["GET"])
@jwt_required()
@check_permission('system_management')
def list_officers():
    """任职列表：分页 + status(active/ended/all) + 关键词（姓名/职位/组）"""
    try:
        page = max(1, int(request.args.get("page", 1)))
        per_page = min(100, max(1, int(request.args.get("per_page", 20))))
    except ValueError:
        page, per_page = 1, 20
    status = request.args.get("status", "all")          # active / ended / all
    q = (request.args.get("q") or "").strip()

    query = ClubOfficer.query
    if status == 'active':
        query = query.filter(ClubOfficer.status == 'active')
    elif status == 'ended':
        query = query.filter(ClubOfficer.status == 'ended')

    # 关键词：先按职位/组过滤行，姓名命中通过 user_ids 二段过滤（数据量小，两段查询够用）
    if q:
        users = UserModel.query.filter(UserModel.username.contains(q)).all()
        hit_ids = [u.id for u in users]
        from sqlalchemy import or_
        conds = [ClubOfficer.title.contains(q), ClubOfficer.department.contains(q)]
        if hit_ids:
            conds.append(ClubOfficer.user_id.in_(hit_ids))
        query = query.filter(or_(*conds))

    rows = query.all()
    # 在任在前、职位 rank 序、新任命在前（数据量小，内存排序后分页）
    ranks = _rank_map()
    rows.sort(key=lambda r: (
        r.status != 'active',
        ranks[r.title_id].sort_rank if r.title_id in ranks else 99,
        -(r.created_at.timestamp() if r.created_at else 0),
        -r.id,
    ))
    total = len(rows)
    rows = rows[(page - 1) * per_page:page * per_page]

    # 用户信息一次取齐，避免 N+1
    user_map = {}
    ids = list({r.user_id for r in rows})
    if ids:
        user_map = {u.id: u for u in UserModel.query.filter(UserModel.id.in_(ids)).all()}

    return jsonify({
        "code": 200,
        "message": "获取任职列表成功",
        "data": {
            "officers": [_officer_dict(r, user_map.get(r.user_id)) for r in rows],
            "total": total,
            "page": page,
            "per_page": per_page,
        },
    })


@bp.route("", methods=["POST"])
@jwt_required()
@check_permission('system_management')
@audit_log(operation="任命社团干事")
def appoint_officer():
    """任命：body {user_id, title|title_id, department|group_id?, term_start?(默认今天)}"""
    data = request.get_json(silent=True) or {}
    if not data.get("user_id"):
        return jsonify({"code": 400, "message": "缺少 user_id"}), 400
    user = UserModel.query.get(int(data["user_id"]))
    if not user:
        return jsonify({"code": 404, "message": "用户不存在"}), 404

    pos, err = _resolve_position(data)
    if err:
        return err
    group, err = _resolve_group(data)
    if err:
        return err

    verr = _validate_appointment(user.id, pos, group)
    if verr:
        return jsonify({"code": verr[0], "message": verr[1]}), verr[0]

    term_start, terr = _parse_term_start(data.get("term_start"))
    if terr:
        return jsonify({"code": 400, "message": terr}), 400

    current = _current_user()
    officer = ClubOfficer(
        user_id=user.id,
        title_id=pos.id, title=pos.name,
        group_id=group.id if group else None,
        department=group.name if group else None,
        term_start=term_start,
        status='active',
        appointed_by=current.id if current else None,
    )
    db.session.add(officer)
    db.session.commit()
    return jsonify({"code": 200, "message": f"已任命 {user.username} 为 {pos.name}",
                    "data": _officer_dict(officer, user)})


@bp.route("/batch", methods=["POST"])
@jwt_required()
@check_permission('system_management')
@audit_log(operation="批量任命社团干事")
def batch_appoint():
    """批量导入：body {items: [{user_id|username, title|title_id, department|group_id?, term_start?}]}
    逐项校验逐项回报；批内互查（同批任命先行 flush 后续可读）。"""
    data = request.get_json(silent=True) or {}
    items = data.get("items")
    if not isinstance(items, list) or not items:
        return jsonify({"code": 400, "message": "缺少 items 数组"}), 400
    if len(items) > 200:
        return jsonify({"code": 400, "message": "单批至多 200 条"}), 400

    current = _current_user()
    results = []
    added = rejected = 0

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
            reason = f"用户 {item.get('user_id') or '「' + str(item.get('username')) + '」'} 不存在"
            results.append({"index": idx, "ok": False, "reason": reason})
            rejected += 1
            continue

        pos, err = _resolve_position(item)
        if err:
            results.append({"index": idx, "ok": False,
                            "reason": err[0].get_json()["message"], "username": user.username})
            rejected += 1
            continue
        group, err = _resolve_group(item)
        if err:
            results.append({"index": idx, "ok": False,
                            "reason": err[0].get_json()["message"], "username": user.username})
            rejected += 1
            continue

        verr = _validate_appointment(user.id, pos, group)
        if verr:
            results.append({"index": idx, "ok": False, "reason": verr[1], "username": user.username})
            rejected += 1
            continue

        term_start, terr = _parse_term_start(item.get("term_start"))
        if terr:
            results.append({"index": idx, "ok": False, "reason": terr, "username": user.username})
            rejected += 1
            continue

        row = ClubOfficer(
            user_id=user.id,
            title_id=pos.id, title=pos.name,
            group_id=group.id if group else None,
            department=group.name if group else None,
            term_start=term_start, status='active',
            appointed_by=current.id if current else None,
        )
        db.session.add(row)
        db.session.flush()          # 拿 id，同时让同批后续项能读到
        results.append({"index": idx, "ok": True, "officer_id": row.id, "username": user.username,
                        "title": pos.name, "department": group.name if group else None})
        added += 1

    db.session.commit()
    return jsonify({"code": 200, "message": f"批量任命完成：{added} 成功 / {rejected} 拒绝",
                    "data": {"results": results, "added": added, "rejected": rejected}})


@bp.route("/<int:officer_id>", methods=["PUT"])
@jwt_required()
@check_permission('system_management')
@audit_log(operation="编辑社团干事任职")
def edit_officer(officer_id):
    """编辑：active 行可改职位/组/任期起（重跑规则校验）；ended 行仅许修正 term_end/end_reason。"""
    officer = ClubOfficer.query.get(officer_id)
    if not officer:
        return jsonify({"code": 404, "message": "任职记录不存在"}), 404
    data = request.get_json(silent=True) or {}

    if officer.status == 'active':
        # 入参只认一种轨道：显式 id 优先；传名则不带旧 id（否则 id 解析会盖掉改名入参）；
        # 都不传回落当前值（组转原组名解析）。department 显式 null = 清空（不挂组职位）。
        if "title_id" in data:
            title_key = {"title_id": data["title_id"]}
        elif "title" in data:
            title_key = {"title": data["title"]}
        else:
            title_key = {"title": officer.title}
        if "group_id" in data:
            group_key = {"group_id": data["group_id"]}
        elif "department" in data:
            group_key = {"department": data["department"]}
        elif officer.group_id:
            group_key = {"group_id": officer.group_id}
        else:
            group_key = {}
        merged = {**title_key, **group_key}
        pos, err = _resolve_position(merged)
        if err:
            return err
        group, err = _resolve_group(merged)
        if err:
            return err

        verr = _validate_appointment(officer.user_id, pos, group, exclude_id=officer.id)
        if verr:
            return jsonify({"code": verr[0], "message": verr[1]}), verr[0]

        term_start, terr = _parse_term_start(data.get("term_start"), fallback=officer.term_start)
        if terr:
            return jsonify({"code": 400, "message": terr}), 400

        officer.title_id, officer.title = pos.id, pos.name
        officer.group_id = group.id if group else None
        officer.department = group.name if group else None
        officer.term_start = term_start
    else:
        # ended 行：受控修正，只动卸任留痕两字段
        if data.get("term_end"):
            try:
                new_end = datetime.strptime(data["term_end"], "%Y-%m-%d").date()
            except (TypeError, ValueError):
                return jsonify({"code": 400, "message": "任期止格式应为 YYYY-MM-DD"}), 400
            if new_end < officer.term_start:
                return jsonify({"code": 400, "message": "任期止不能早于任期起"}), 400
            officer.term_end = new_end
        if "end_reason" in data:
            officer.end_reason = (str(data.get("end_reason") or "").strip() or None)

    db.session.commit()
    user = UserModel.query.get(officer.user_id)
    return jsonify({"code": 200, "message": "任职信息已更新", "data": _officer_dict(officer, user)})


@bp.route("/<int:officer_id>/end", methods=["POST"])
@jwt_required()
@check_permission('system_management')
@audit_log(operation="卸任社团干事")
def end_officer(officer_id):
    """卸任（状态化不删行）：body {term_end?(默认今天), end_reason?(选填 ≤200 字)}"""
    officer = ClubOfficer.query.get(officer_id)
    if not officer:
        return jsonify({"code": 404, "message": "任职记录不存在"}), 404
    if officer.status != 'active':
        return jsonify({"code": 400, "message": "该任职已卸任"}), 400

    data = request.get_json(silent=True) or {}
    term_end = date.today()
    if data.get("term_end"):
        try:
            term_end = datetime.strptime(data["term_end"], "%Y-%m-%d").date()
        except (TypeError, ValueError):
            return jsonify({"code": 400, "message": "卸任日期格式应为 YYYY-MM-DD"}), 400
    if term_end < officer.term_start:
        return jsonify({"code": 400, "message": "卸任日期不能早于任期起"}), 400

    reason = (str(data.get("end_reason") or "").strip() or None)
    if reason and len(reason) > 200:
        return jsonify({"code": 400, "message": "卸任原因至多 200 字"}), 400

    current = _current_user()
    officer.status = 'ended'
    officer.term_end = term_end
    officer.end_reason = reason
    officer.ended_by = current.id if current else None
    db.session.commit()
    user = UserModel.query.get(officer.user_id)
    return jsonify({"code": 200, "message": "已卸任（记录保留）", "data": _officer_dict(officer, user)})

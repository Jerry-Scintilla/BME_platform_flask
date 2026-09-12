"""社团干事身份蓝图（功能扩展轮 §四，轻量任职档案——不挂任何权限）。

admin 侧 CRUD：任命 / 编辑 / 卸任（状态化不删行）/ 批量导入（预留，逐项回报）。
user 侧不设端点：user_index / profile/<id> 回包直接附任职（见 user.py），
社区 feed 的 author_badge 见 community.py——三者都 import 本文件的查询助手。

应用层约束（任命/编辑共用）：
  R1 同 (department, title) 至多 1 条 active —— 社长全局唯一、每组一个组长、职位不重复任命
     （组员豁免：普通组员一组可多人，R1 不适用）
  R2 同一社员至多 2 条 active —— 最多兼两组身份（组员照算）
  R3 同一社员的 active 行组不重复 —— 同部门兼两职无意义
  R4 社长必须无 department；组长/组员必须挂组；其余管理职位选填
"""
from datetime import date, datetime

from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required

from exts import db
from models import UserModel, ClubOfficer
from . import check_permission, audit_log, _current_user

bp = Blueprint("officers", __name__, url_prefix="/admin/officers")

# 职位白名单（前后端同源；管理职位固定枚举；分组体系=组长+组员[普通组员归属，无头衔语义]，扩枚举=改常量）
TITLE_MANAGEMENT = ("社长", "副社长", "团支书", "副团支书")
TITLE_GROUP = ("组长", "组员")
TITLE_CHOICES = TITLE_MANAGEMENT + TITLE_GROUP

# 主职排序（列表排序用）：管理职位在前，组长次之，组员殿后
TITLE_RANK = {t: i for i, t in enumerate(TITLE_CHOICES)}


# ────────────────────────────────────────
# 查询助手（user.py / community.py 复用）
# ────────────────────────────────────────

def officers_by_user(user_ids):
    """批量查 active 任职 {uid: [行]}，按职级 rank + 任期起排序（主职在前）。空入参返回 {}。"""
    ids = [i for i in {u for u in user_ids if u} if i is not None]
    if not ids:
        return {}
    rows = ClubOfficer.query.filter(
        ClubOfficer.user_id.in_(ids),
        ClubOfficer.status == 'active',
    ).all()
    result = {}
    for r in rows:
        result.setdefault(r.user_id, []).append(r)
    for rows_of_user in result.values():
        rows_of_user.sort(key=lambda r: (TITLE_RANK.get(r.title, 99), r.term_start or date.min, r.id))
    return result


def primary_title_map(user_ids):
    """{uid: 主职 title}——社区 feed 徽章用；无任职/仅有组员行的 uid 不含键
    （徽章只认头衔：管理层+组长；组员是归属不是头衔，不进徽章）。"""
    result = {}
    for uid, rows in officers_by_user(user_ids).items():
        titled = [r for r in rows if r.title != '组员']
        if titled:
            result[uid] = titled[0].title
    return result


def public_officers(user_id):
    """单用户 active 任职的公开形态（user_index / profile 回包附字段）。"""
    return [
        {"title": r.title, "department": r.department,
         "term_start": r.term_start.isoformat() if r.term_start else None}
        for r in officers_by_user([user_id]).get(user_id, [])
    ]


def _avatar_url(avatar_url):
    """头像完整 URL（与 gratitude/discussion 同语义：相对路径按当前页 origin 解析）"""
    if not avatar_url:
        return ""
    if avatar_url.startswith('http://') or avatar_url.startswith('https://'):
        return avatar_url
    return f"/data/avatars/{avatar_url}"


def _officer_dict(o, user=None):
    """admin 列表行形态（含治理字段）"""
    return {
        "id": o.id,
        "user_id": o.user_id,
        "username": user.username if user else "已注销用户",
        "avatar": _avatar_url(user.avatar_url) if user else "",
        "title": o.title,
        "department": o.department,
        "term_start": o.term_start.isoformat() if o.term_start else None,
        "term_end": o.term_end.isoformat() if o.term_end else None,
        "status": o.status,
        "end_reason": o.end_reason,
        "created_at": o.created_at.isoformat() if o.created_at else None,
    }


# ────────────────────────────────────────
# 校验（任命与编辑共用；pending 为同事务内先行 add 的行，供批量导入批内互查）
# ────────────────────────────────────────

def _validate_appointment(user_id, title, department, exclude_id=None):
    """R1-R4 校验，通过返回 None，否则返回 (错误码, 中文原因)。
    批量导入场景调用前已 flush 同批先行行，本查询（同会话）天然批内互查。"""
    if title not in TITLE_CHOICES:
        return 400, f"职位仅支持 {'/'.join(TITLE_CHOICES)}"
    if department is not None:
        department = department.strip() or None
    # R4：挂组约束
    if title == '社长' and department:
        return 400, "社长统领全局，不挂组"
    if title in ('组长', '组员') and not department:
        return 400, f"{title}必须归属一个组"
    if department and len(department) > 50:
        return 400, "组名过长（≤50 字）"

    # R1：同 (department, title) 至多 1 条 active（== None 自动转 IS NULL）。
    # 组员豁免——一组可有多名组员；组长仍每组一个。
    if title != '组员':
        q1 = ClubOfficer.query.filter(
            ClubOfficer.title == title,
            ClubOfficer.department == department,
            ClubOfficer.status == 'active',
        )
        if exclude_id:
            q1 = q1.filter(ClubOfficer.id != exclude_id)
        dup = q1.first()
        if dup:
            holder = UserModel.query.get(dup.user_id)
            holder_name = holder.username if holder else f"#{dup.user_id}"
            where = f"{department}·" if department else ""
            return 409, f"{where}{title} 已由 {holder_name} 在任，请先卸任再任命"

    # R2/R3：同一社员 active ≤2 且组不重复
    mine = [m for m in ClubOfficer.query.filter(
        ClubOfficer.user_id == user_id,
        ClubOfficer.status == 'active',
    ).all() if m.id != exclude_id]
    if len(mine) >= 2:
        return 409, "该社员已兼两组身份（上限 2）"
    if any(m.department == department for m in mine):
        return 409, f"该社员在{department or '管理层'}已有任职，同组不可兼两职"

    return None


# ────────────────────────────────────────
# admin 端点
# ────────────────────────────────────────

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

    # 在任在前、职级序、新任命在前
    rank_case = db.case(TITLE_RANK, value=ClubOfficer.title, else_=99)
    query = query.order_by(
        (ClubOfficer.status != 'active'),
        rank_case,
        ClubOfficer.created_at.desc(),
        ClubOfficer.id.desc(),
    )
    total = query.count()
    rows = query.offset((page - 1) * per_page).limit(per_page).all()

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
    """任命：body {user_id, title, department?, term_start?(默认今天)}"""
    data = request.get_json(silent=True) or {}
    user_id = data.get("user_id")
    title = (data.get("title") or "").strip()
    department = (data.get("department") or "").strip() or None

    if not user_id:
        return jsonify({"code": 400, "message": "缺少 user_id"}), 400
    user = UserModel.query.get(int(user_id))
    if not user:
        return jsonify({"code": 404, "message": "用户不存在"}), 404

    err = _validate_appointment(user.id, title, department)
    if err:
        return jsonify({"code": err[0], "message": err[1]}), err[0]

    term_start = date.today()
    if data.get("term_start"):
        try:
            term_start = datetime.strptime(data["term_start"], "%Y-%m-%d").date()
        except (TypeError, ValueError):
            return jsonify({"code": 400, "message": "任期起格式应为 YYYY-MM-DD"}), 400

    current = _current_user()
    officer = ClubOfficer(
        user_id=user.id,
        title=title,
        department=department,
        term_start=term_start,
        status='active',
        appointed_by=current.id if current else None,
    )
    db.session.add(officer)
    db.session.commit()
    return jsonify({"code": 200, "message": f"已任命 {user.username} 为 {department + '·' if department else ''}{title}",
                    "data": _officer_dict(officer, user)})


@bp.route("/batch", methods=["POST"])
@jwt_required()
@check_permission('system_management')
@audit_log(operation="批量任命社团干事")
def batch_appoint():
    """批量导入（预留接口，本期无前端 UI）：body {items: [{user_id|username, title, department?, term_start?}]}
    逐项校验逐项回报（对齐批量加成员先例）；批内互查（同批两条同职位只过第一条）。"""
    data = request.get_json(silent=True) or {}
    items = data.get("items")
    if not isinstance(items, list) or not items:
        return jsonify({"code": 400, "message": "缺少 items 数组"}), 400
    if len(items) > 200:
        return jsonify({"code": 400, "message": "单批至多 200 条"}), 400

    current = _current_user()
    results = []
    added_rows = []          # 本批已 add 未 commit 的行，供后续项 R1/R2/R3 互查
    added = rejected = 0

    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            results.append({"index": idx, "ok": False, "reason": "条目格式错误"})
            rejected += 1
            continue

        # 用户解析：user_id 优先，否则按 username 精确匹配
        user = None
        if item.get("user_id"):
            user = UserModel.query.get(int(item["user_id"]))
            if not user:
                results.append({"index": idx, "ok": False, "reason": f"用户 {item['user_id']} 不存在"})
                rejected += 1
                continue
        elif item.get("username"):
            user = UserModel.query.filter_by(username=(item["username"] or "").strip()).first()
            if not user:
                results.append({"index": idx, "ok": False, "reason": f"用户「{item['username']}」不存在"})
                rejected += 1
                continue
        else:
            results.append({"index": idx, "ok": False, "reason": "缺少 user_id 或 username"})
            rejected += 1
            continue

        title = (item.get("title") or "").strip()
        department = (item.get("department") or "").strip() or None

        err = _validate_appointment(user.id, title, department)
        if err:
            results.append({"index": idx, "ok": False, "reason": err[1], "username": user.username})
            rejected += 1
            continue

        term_start = date.today()
        if item.get("term_start"):
            try:
                term_start = datetime.strptime(item["term_start"], "%Y-%m-%d").date()
            except (TypeError, ValueError):
                results.append({"index": idx, "ok": False, "reason": "任期起格式应为 YYYY-MM-DD", "username": user.username})
                rejected += 1
                continue

        row = ClubOfficer(
            user_id=user.id, title=title, department=department,
            term_start=term_start, status='active',
            appointed_by=current.id if current else None,
        )
        db.session.add(row)
        db.session.flush()          # 拿 id，同时让同批后续项能读到
        added_rows.append(row)
        results.append({"index": idx, "ok": True, "officer_id": row.id, "username": user.username,
                        "title": title, "department": department})
        added += 1

    db.session.commit()
    return jsonify({"code": 200, "message": f"批量任命完成：{added} 成功 / {rejected} 拒绝",
                    "data": {"results": results, "added": added, "rejected": rejected}})


@bp.route("/<int:officer_id>", methods=["PUT"])
@jwt_required()
@check_permission('system_management')
@audit_log(operation="编辑社团干事任职")
def edit_officer(officer_id):
    """编辑：active 行可改 title/department/term_start（重跑 R1-R4）；ended 行仅许修正 term_end/end_reason。"""
    officer = ClubOfficer.query.get(officer_id)
    if not officer:
        return jsonify({"code": 404, "message": "任职记录不存在"}), 404
    data = request.get_json(silent=True) or {}

    if officer.status == 'active':
        title = (data.get("title") or officer.title).strip()
        # department 键缺失=保留原值；显式传 null/空串=清空（社长）
        if "department" in data:
            raw = data.get("department")
            department = (str(raw).strip() or None) if raw is not None else None
        else:
            department = officer.department
        term_start = officer.term_start
        if data.get("term_start"):
            try:
                term_start = datetime.strptime(data["term_start"], "%Y-%m-%d").date()
            except (TypeError, ValueError):
                return jsonify({"code": 400, "message": "任期起格式应为 YYYY-MM-DD"}), 400

        err = _validate_appointment(officer.user_id, title, department, exclude_id=officer.id)
        if err:
            return jsonify({"code": err[0], "message": err[1]}), err[0]

        officer.title = title
        officer.department = department
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

"""组织架构页数据蓝图（用户端，设计方案 §4.1）。

GET /organization：组树（active 组，层级不假设固定）+ 干事（顶部管理层区 rank 阈值判定）+
每组归属成员列表（leader 置顶）+ primary/secondary 计数（含子孙上卷）。
权限口径与 /user 列表一致（登录可见）；数据组装只读，无缓存（社员量级一次 IN 查询足够）。
"""
from flask import Blueprint, jsonify
from flask_jwt_extended import jwt_required

from exts import db
from models import UserModel, ClubGroup, ClubPosition, ClubOfficer, ClubMembership
from .media import public_avatar_url

bp = Blueprint("organization", __name__, url_prefix="/organization")

# 顶部管理层区阈值：sort_rank ≤ 此值的职位进社长 hero + 管理层横排（种子：社长1/副社长2/
# 团支书3/副团支书4；组长10 落组内 leader 位）。后台自建职位按其 sort_rank 自动归区。
MANAGEMENT_RANK_MAX = 9
MAX_DEPTH = 4  # 与 club_admin 校验同源


def _user_card(u):
    return {
        "id": u.id,
        "username": u.username,
        "avatar": public_avatar_url(u.avatar_url) if u and u.avatar_url else None,
    }


@bp.route("", methods=["GET"])
@jwt_required()
def org_chart():
    groups = (ClubGroup.query.filter_by(status='active')
              .order_by(ClubGroup.sort_order, ClubGroup.id).all())
    positions = {p.id: p for p in ClubPosition.query.filter_by(status='active').all()}
    officers = ClubOfficer.query.filter_by(status='active').all()
    memberships = ClubMembership.query.all()

    user_ids = {o.user_id for o in officers} | {m.user_id for m in memberships}
    users = ({u.id: u for u in UserModel.query.filter(UserModel.id.in_(user_ids)).all()}
             if user_ids else {})
    group_names = {g.id: g.name for g in groups}

    # 干事按组归档；管理层（rank ≤ 阈值）单独收集
    officers_by_group = {}
    management = []          # [(sort_rank, card)]
    for o in officers:
        pos = positions.get(o.title_id)
        u = users.get(o.user_id)
        if not pos or not u:
            continue
        if pos.sort_rank <= MANAGEMENT_RANK_MAX:
            management.append((pos.sort_rank, {
                **_user_card(u), "title": pos.name,
                "group": group_names.get(o.group_id),
            }))
        else:
            officers_by_group.setdefault(o.group_id, []).append((pos.sort_rank, u, pos))

    memberships_by_group = {}
    for m in memberships:
        memberships_by_group.setdefault(m.group_id, []).append(m)

    # user → 在任职位名（一人至多一条 active，map 足够）
    title_by_user = {}
    for o in officers:
        pos = positions.get(o.title_id)
        if pos and o.user_id not in title_by_user:
            title_by_user[o.user_id] = pos.name

    children_of = {}
    for g in groups:
        children_of.setdefault(g.parent_id, []).append(g)

    def build_node(g):
        """单组节点：oversee_by（管理层干事）/ leader（组长类）/ members（归属，leader 置顶）/
        counts（primary/secondary 含子孙上卷）/ children 递归。"""
        offs = officers_by_group.get(g.id, [])
        oversee = lead = None
        for sort_rank, u, pos in sorted(offs, key=lambda t: t[0]):
            card = {**_user_card(u), "title": pos.name}
            if sort_rank <= MANAGEMENT_RANK_MAX and oversee is None:
                oversee = card
            elif sort_rank > MANAGEMENT_RANK_MAX and lead is None:
                lead = card

        member_cards = []
        if lead:
            member_cards.append({**lead, "slot": "primary", "is_leader": True})
        rows = sorted(memberships_by_group.get(g.id, []),
                      key=lambda m: (0 if m.slot == 'primary' else 1, m.id))
        for m in rows:
            u = users.get(m.user_id)
            if not u or (lead and u.id == lead["id"]):
                continue
            member_cards.append({
                **_user_card(u), "title": title_by_user.get(u.id),
                "slot": m.slot, "is_leader": False,
            })

        counts = {
            "primary": sum(1 for m in memberships_by_group.get(g.id, []) if m.slot == 'primary'),
            "secondary": sum(1 for m in memberships_by_group.get(g.id, []) if m.slot == 'secondary'),
        }
        children = [build_node(c) for c in children_of.get(g.id, [])]
        for c in children:
            counts["primary"] += c["counts"]["primary"]
            counts["secondary"] += c["counts"]["secondary"]

        return {
            "id": g.id, "name": g.name,
            "oversee_by": oversee, "leader": lead,
            "counts": counts, "members": member_cards, "children": children,
        }

    management.sort(key=lambda t: t[0])
    president = next((m for r, m in management if r == 1), None)
    management_cards = [m for r, m in management if not (president and m["id"] == president["id"])]

    return jsonify({
        "code": 200,
        "message": "获取组织架构成功",
        "data": {
            "president": president,
            "management": management_cards,
            "tree": [build_node(g) for g in children_of.get(None, [])],
        },
    })

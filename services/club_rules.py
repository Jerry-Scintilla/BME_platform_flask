"""社团职位类别判定单源（社团组织管理改版 2026-10-02，设计方案 §9-9 兑现）。

「全社治理职务」（club：社长/副社长/团支书…）与「组内组长类职位」（group：组长）
此前靠 sort_rank<=9 / >=10 数值区间区分，判定散在 organization.py（展示口径）、
provisioning.py（授权口径）两处三常量。现收敛为 club_position.org_slot 显式字段
+ 本模块单源判定：
  显式 org_slot 优先；空/非法回落 sort_rank 派生（<=9→club，>=10→group），
  与历史行为逐位等价——兜底阈值勿动，改判定先跑 scripts/test_work_club_ws.py。
"""
from models import ClubPosition

ORG_SLOT_CLUB = 'club'
ORG_SLOT_GROUP = 'group'
ORG_SLOTS = (ORG_SLOT_CLUB, ORG_SLOT_GROUP)

# org_slot 为空时的 rank 兜底阈值（历史 MANAGEMENT_RANK_MAX=9 / LEADER_RANK_MIN=10 等值）
_FALLBACK_RANK_MAX = 9


def position_org_slot(pos):
    """职位类别：显式 org_slot 优先；空/非法回落 sort_rank 派生。

    pos 为 None（任职行职位缺失）时按组内职位兜底——与 provisioning 历史
    「无职位定义按组长类」口径一致。"""
    if pos is not None and pos.org_slot in ORG_SLOTS:
        return pos.org_slot
    rank = pos.sort_rank if pos is not None else None
    if rank is not None and rank <= _FALLBACK_RANK_MAX:
        return ORG_SLOT_CLUB
    return ORG_SLOT_GROUP


def is_club_position(pos):
    """社团职务（组织页顶部管理层/分管位口径）。"""
    return position_org_slot(pos) == ORG_SLOT_CLUB


def is_group_position(pos):
    """组内职位·组长类（组织页组内 leader 位、工作区 coordinator 派生口径）。"""
    return position_org_slot(pos) == ORG_SLOT_GROUP


def default_leader_position_id():
    """组长缺省职位：org_slot='group' 且 active，按 sort_rank,id 升序首个。

    兜底派生行（org_slot 为空）也参与排序后按类别筛（SQL 表达不了兜底，职位量级小）。"""
    rows = (ClubPosition.query.filter_by(status='active')
            .order_by(ClubPosition.sort_rank, ClubPosition.id).all())
    for p in rows:
        if is_group_position(p):
            return p.id
    return None

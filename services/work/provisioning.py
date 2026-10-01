"""内部工作台·授权自动派生器（2026-10-01 设计方案 §3.2，D1=乙/D2=通知/D3=两槽全授）。

组织事实（club_membership / club_officer）是授权的生产来源：人事写入点与本模块
同事务联动，按「重算」模型维护 (user, workspace) 的 origin='auto' 授权行——
只管自动行（manual/direct 是人签的字，派生器不碰）；veto（status='vetoed'）
按 (user_id, workspace_id) 拦截，退社再入社（新组织行新 id）不会绕过。

角色映射（与组织页 leader 判定同源，organization.py MANAGEMENT_RANK_MAX=9）：
  挂组任职 rank>9（组长类）→ coordinator；rank<=9（分管）→ member；
  membership 归属（primary/secondary 两槽都授，D3）→ member；就高不叠加。
失效三律不变：本模块只负责行的生灭，有效性仍由 access.py 每请求实时判定兜底
（例如任期自然到界无人点卸任——日巡检任务再补重算）。

全部函数只写 session 不 commit——与调用方（蓝图/治理/脚本）同事务。
"""
from datetime import date

from exts import db
from models import (ClubGroup, ClubMembership, ClubOfficer, ClubPosition,
                    WorkAccessGrant, WorkWorkspace)

# 组长类判定阈值：与 blueprints/organization.py 的 MANAGEMENT_RANK_MAX 保持同值
# （organization 是展示口径、本处是授权口径，改任一处须同步另一处）
LEADER_RANK_MIN = 10          # sort_rank >= 10 视为组长类（挂组任职 → coordinator）

# 自动行的授权人（无真人操作时的落款）；reason 前缀供治理页辨识
SYSTEM_OPERATOR_ID = None
AUTO_REASON = '自动派生'


def _ws_for_group(group_id):
    """组 → 启用中的组工作区（一组至多一个；club 级 scope 不参与自动授）。"""
    if not group_id:
        return None
    return (WorkWorkspace.query
            .filter_by(club_group_id=group_id, scope='group', status='active')
            .first())


def _has_veto(user_id, workspace_id):
    return (WorkAccessGrant.query
            .filter_by(user_id=user_id, workspace_id=workspace_id, status='vetoed')
            .first() is not None)


def _active_auto_grant(user_id, workspace_id):
    return (WorkAccessGrant.query
            .filter_by(user_id=user_id, workspace_id=workspace_id,
                       status='active', origin='auto')
            .first())


def _has_active_manual_grant(user_id, workspace_id):
    """治理手动授过的行优先（就高原则的 manual 侧）：有 manual active 即不再生自动行。"""
    return (WorkAccessGrant.query
            .filter_by(user_id=user_id, workspace_id=workspace_id, status='active')
            .filter(WorkAccessGrant.origin != 'auto')
            .first() is not None)


def _officer_in_term(officer, today=None):
    today = today or date.today()
    return (officer.status == 'active'
            and officer.term_start <= today
            and (officer.term_end is None or today <= officer.term_end))


def _desired(user_id, ws):
    """按当前组织事实算 (user, ws) 应有的自动角色。返回 (role, source_type, source_id) 或 None。

    一人至多 1 条 active 任职（模型约束），组长类任职优先于归属；member 角色
    优先挂 membership 来源（任职变动不带走），无归属时回落任职来源。"""
    group_id = ws.club_group_id
    today = date.today()
    ms = (ClubMembership.query
          .filter_by(user_id=user_id, group_id=group_id).first())
    coord = None
    member_off = None
    for o in ClubOfficer.query.filter_by(user_id=user_id, group_id=group_id).all():
        if not _officer_in_term(o, today):
            continue
        pos = ClubPosition.query.get(o.title_id) if o.title_id else None
        rank = pos.sort_rank if pos else LEADER_RANK_MIN    # 无职位定义按组长类兜底
        if rank >= LEADER_RANK_MIN and coord is None:
            coord = o
        elif rank < LEADER_RANK_MIN and member_off is None:
            member_off = o
    if coord is not None:
        return ('coordinator', 'officer', coord.id)
    if ms is not None:
        return ('member', 'membership', ms.id)
    if member_off is not None:
        return ('member', 'officer', member_off.id)
    return None


def _notify_granted(user_id, group_name):
    """自动授权通知（D2）：最小信息一条，告知权限已随组织事实就绪。"""
    from blueprints.notification import create_notification
    create_notification(user_id, "内部工作台",
                        f"你已加入「{group_name}」工作区（随组织归属自动开通）",
                        category='work')


def sync_user_workspace(user_id, workspace_id, operator_id=None, event='同步'):
    """重算 (user, workspace) 的自动授权（幂等，方案 §3.2 全事件收敛到这一个入口）。

    返回动作名（granted/revoked/changed/unchanged/skipped/vetoed/manual_present/none），
    供回填脚本与调用方统计。"""
    ws = WorkWorkspace.query.get(workspace_id)
    if not ws or ws.scope != 'group':
        return 'skipped'
    if not ws.auto_grant or ws.status != 'active':
        return 'skipped'          # 停自动：只停新增，存量自动行不动（方案 §3.2 末行）
    if _has_veto(user_id, workspace_id):
        return 'vetoed'
    # D5 防授权复活（规格 9.4，硬约束不随 enforcement 模式灰度）：merged 账号/
    # 人员级 veto（换主号不得绕过账号级否决）不重建自动授权。
    from services.identity import enforcement as identity_enforcement
    from models import UserModel as _UserModel
    _target = db.session.get(_UserModel, user_id)
    if _target is not None:
        _denied, _why = identity_enforcement.workspace_denied(_target, workspace_id)
        if _denied:
            return 'identity_denied'
    desired = _desired(user_id, ws)
    current = _active_auto_grant(user_id, workspace_id)

    if desired is None:
        if current:
            current.status = 'revoked'
            current.revoke_reason = f'{AUTO_REASON}：组织事实已不在本组（{event}）'
            return 'revoked'
        return 'none'

    role, source_type, source_id = desired
    if current and (current.role == role
                    and current.source_type == source_type
                    and current.source_id == source_id):
        return 'unchanged'

    if _has_active_manual_grant(user_id, workspace_id):
        # manual 行在（治理明确授过）——自动行让位：若有旧自动行则清掉
        if current:
            current.status = 'revoked'
            current.revoke_reason = f'{AUTO_REASON}：与手动授权并存，自动行让位（{event}）'
            return 'revoked'
        return 'manual_present'

    group_name = (ClubGroup.query.get(ws.club_group_id).name
                  if ws.club_group_id else '')
    if current:
        current.status = 'revoked'
        current.revoke_reason = f'{AUTO_REASON}：角色或来源变更（{event}）'
    grant = WorkAccessGrant(
        user_id=user_id, role=role, workspace_id=workspace_id,
        source_type=source_type, source_id=source_id,
        group_id_snapshot=ws.club_group_id if source_type == 'membership' else None,
        status='active', origin='auto',
        granted_by=operator_id if operator_id is not None else 1,   # 无操作人落款超管(id=1)，避免 NOT NULL 违约
        grant_reason=f'{AUTO_REASON}：{event}（{group_name}）',
    )
    db.session.add(grant)
    if not current:
        _notify_granted(user_id, group_name)      # 首次进入才通知，升降级不打扰
    return 'granted' if not current else 'changed'


def sync_after_membership(user_id, group_ids, operator_id=None, event='归属变更'):
    """归属写入点联动：对涉及的（旧/新）组工作区逐个重算。group_ids 含 None 安全。"""
    actions = []
    for gid in set(g for g in group_ids if g):
        ws = _ws_for_group(gid)
        if ws:
            actions.append(sync_user_workspace(user_id, ws.id, operator_id, event))
    return actions


def sync_after_officer(user_id, old_group_id, new_group_id, operator_id=None, event='任职变更'):
    """任职写入点联动：旧组+新组各重算（换组两边都要看；卸任 old=组 new=None）。"""
    return sync_after_membership(user_id, [old_group_id, new_group_id], operator_id, event)


def backfill_workspace(ws, operator_id=None, event='工作区回填'):
    """整组重算（建区/开闸/回填脚本/日巡检共用）：组织事实推导人群 ∪ 现有自动行持有人
    （后者覆盖「人已离开但自动行还在」的漂移）。返回动作计数。"""
    counts = {}
    user_ids = set()
    for m in ClubMembership.query.filter_by(group_id=ws.club_group_id).all():
        user_ids.add(m.user_id)
    today = date.today()
    for o in ClubOfficer.query.filter_by(group_id=ws.club_group_id).all():
        if _officer_in_term(o, today):
            user_ids.add(o.user_id)
    for g in WorkAccessGrant.query.filter_by(
            workspace_id=ws.id, status='active', origin='auto').all():
        user_ids.add(g.user_id)
    for uid in sorted(user_ids):
        action = sync_user_workspace(uid, ws.id, operator_id, event)
        counts[action] = counts.get(action, 0) + 1
    return counts


def backfill_all(operator_id=None):
    """全部启用中的自动授组工作区重算（日巡检/脚本入口）。"""
    result = {}
    for ws in (WorkWorkspace.query
               .filter_by(scope='group', status='active', auto_grant=True).all()):
        result[ws.id] = backfill_workspace(ws, operator_id)
    return result

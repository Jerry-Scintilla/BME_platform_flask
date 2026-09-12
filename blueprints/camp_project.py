"""项目营蓝图（营期升级阶段 3 / 设计方案 v1.3 §3.7-§3.9）。

范围：申报版本流 / 项目志愿与批量回填 / 统一 roster 勾选 API（unit 通用型，首期 project 消费）/
变更场景（H-005 定稿：无锁定环节，running 起变更走管理员通道，原因必填+事件留痕）。

关键规则：
- 申报窗口仅 upcoming（09-12 拍板）；过审即建 CampUnit+ProjectProfile+负责人关系生效+自动入池。
- 一人一营最多 1 个进行中申报 + 最多负责 1 个过审项目（Q-006）。
- 3 上限（CampPolicy.project_limit，负责人自己的计入）：勾选/回填事务内锁 (camp,user) 成员行
  重计数后写入；批量按 user_id 排序加锁防死锁，逐人独立成败（PROJECT_MEMBERSHIP_LIMIT_REACHED）。
- 勾选窗口=营期状态：selecting 自由勾选/回填；running 起普通勾选关闭（CAMP_FORMATION_CLOSED）。
- 成员变更一律行状态化 ended + CampMembershipEvent 只追加，不物理删（历史提交与贡献保留）。
- 错误码沿用方案 §5 稳定契约：CAMP_FORMATION_CLOSED / PROJECT_MEMBERSHIP_LIMIT_REACHED /
  PROJECT_NOT_APPROVED / SELECTION_VERSION_CONFLICT / IDEMPOTENCY_CONFLICT。
"""
import csv
import io
import json
from datetime import datetime

from flask import Blueprint, request, jsonify, Response
from flask_jwt_extended import jwt_required
from sqlalchemy.exc import IntegrityError

from exts import db, redis_client
from models import (
    CampSession, CampPolicy, CampMember,
    CampUnit, ProjectProfile, ProjectApplicationVersion,
    CampUnitMember, CampMembershipEvent, CampProjectPreference,
    UserModel,
)

from . import camp_role, audit_log, _current_user
from .camp import _camp_writable
from .notification import create_notification

bp = Blueprint("camp_project", __name__, url_prefix="/camp")

PROJECT_SOURCE_TYPE = "camp_project"   # 通知溯源类型


# ─────────────────────────────────────────────
# 辅助
# ─────────────────────────────────────────────

def _camp_or_404(sid):
    camp = CampSession.query.get(sid)
    if not camp:
        return None, (jsonify({"code": 404, "message": "营期不存在"}), 404)
    return camp, None


def _unit_or_404(uid):
    unit = CampUnit.query.filter_by(id=uid, unit_type='project').first()
    if not unit:
        return None, None, (jsonify({"code": 404, "message": "项目单元不存在"}), 404)
    camp = CampSession.query.get(unit.camp_session_id)
    if not camp:
        return None, None, (jsonify({"code": 404, "message": "营期不存在"}), 404)
    return unit, camp, None


def _project_limit(camp):
    p = camp.policy
    if p and p.project_limit is not None:
        return p.project_limit
    return 3 if camp.category == 'project' else None


def _active_project_count(sid, user_id):
    """该用户在本营 active 的项目参与数（leader 与 member 均计入；负责人自己的计入，Q-006）。"""
    return (CampUnitMember.query
            .filter(CampUnitMember.user_id == user_id, CampUnitMember.status == 'active')
            .join(CampUnit, CampUnit.id == CampUnitMember.unit_id)
            .filter(CampUnit.camp_session_id == sid, CampUnit.unit_type == 'project')
            .count())


def _lock_member(sid, user_id):
    """锁 (camp,user) 参与者行，串行化同一人的并发 3 上限计数（方案 §3.8）。"""
    return (CampMember.query
            .filter_by(camp_session_id=sid, user_id=user_id)
            .with_for_update().first())


def _is_unit_leader(unit, user):
    return user.is_admin() or CampUnitMember.query.filter_by(
        unit_id=unit.id, user_id=user.id, role='leader', status='active').first() is not None


def _unit_version(unit_id):
    """roster 乐观锁版本：取本单元最新事件 id（每次变更都追加事件，天然单调）。"""
    row = (CampMembershipEvent.query.filter_by(unit_id=unit_id)
           .order_by(CampMembershipEvent.id.desc()).first())
    return row.id if row else 0


def _add_event(camp_id, unit_id, user_id, action, source, operator_id,
               before=None, after=None, reason=None):
    db.session.add(CampMembershipEvent(
        camp_session_id=camp_id, unit_id=unit_id, user_id=user_id,
        action=action, source=source, operator_id=operator_id,
        before=json.dumps(before, ensure_ascii=False) if before else None,
        after=json.dumps(after, ensure_ascii=False) if after else None,
        reason=reason))


def _add_unit_member(unit, user_id, role, source, operator_id, reason=None):
    """写单元成员行 + 事件（不 commit）。调用方保证 3 上限已校验/行锁已持。"""
    row = CampUnitMember.query.filter_by(unit_id=unit.id, user_id=user_id).first()
    if row:
        row.role, row.status, row.ended_at = role, 'active', None
    else:
        row = CampUnitMember(unit_id=unit.id, user_id=user_id, role=role, status='active')
        db.session.add(row)
    _add_event(unit.camp_session_id, unit.id, user_id, 'select', source, operator_id,
               before=None, after={"role": role}, reason=reason)
    return row


def _end_unit_member(unit, row, action, source, operator_id, reason):
    row.status, row.ended_at = 'ended', datetime.now()
    _add_event(unit.camp_session_id, unit.id, row.user_id, action, source, operator_id,
               before={"role": row.role, "status": 'active'},
               after={"role": row.role, "status": 'ended'}, reason=reason)


def _usernames(ids):
    if not ids:
        return {}
    return {u.id: u.username for u in UserModel.query.filter(UserModel.id.in_(ids)).all()}


def _app_dict(a, names=None):
    names = names or _usernames([a.leader_user_id])
    return {
        "id": a.id, "camp_session_id": a.camp_session_id, "unit_id": a.unit_id,
        "version": a.version, "leader_user_id": a.leader_user_id,
        "leader_name": names.get(a.leader_user_id, str(a.leader_user_id)),
        "name": a.name, "background": a.background, "goal": a.goal,
        "required_abilities": a.required_abilities, "recruit_note": a.recruit_note,
        "plan": a.plan, "status": a.status, "reject_reason": a.reject_reason,
        "created_at": a.created_at.isoformat() if a.created_at else None,
    }


def _project_dict(unit, profile, names, member_count=0, my_role=None, my_pref_rank=None):
    return {
        "unit_id": unit.id, "name": unit.name, "status": unit.status,
        "visibility": unit.visibility,
        "leader_user_id": unit.owner_user_id,
        "leader_name": names.get(unit.owner_user_id, str(unit.owner_user_id)),
        "background": profile.background if profile else None,
        "goal": profile.goal if profile else None,
        "required_abilities": profile.required_abilities if profile else None,
        "recruit_note": profile.recruit_note if profile else None,
        "plan": profile.plan if profile else None,
        "member_count": member_count,
        "my_role": my_role,            # leader / member / null（请求者视角）
        "my_pref_rank": my_pref_rank,  # 请求者对本项目的志愿序（1-3，未投 null）
    }


def _notify(user_id, title, content, camp_id, source_id=None, important=False):
    try:
        create_notification(user_id, title, content, category='camp',
                            source_type=PROJECT_SOURCE_TYPE,
                            source_id=source_id or camp_id, camp_session_id=camp_id,
                            is_important=important)
    except Exception:   # 通知失败不阻断主流程
        pass


# ─────────────────────────────────────────────
# 申报流（仅 upcoming；过审建单元+负责人生效+自动入池）
# ─────────────────────────────────────────────

@bp.route("/projects/<int:sid>/mine")
@jwt_required()
def project_mine(sid):
    """我的项目工作台汇总：申报（含版本历史）/我负责的/我参与的/3 上限余量/可否申报。
    只返回请求者本人数据，非成员在申报期（upcoming）也可调用（申报入口卡数据源）。"""
    user = _current_user()
    camp, err = _camp_or_404(sid)
    if err:
        return err
    if camp.category != 'project':
        return jsonify({"code": 400, "message": "该营期不是项目营"}), 400
    apps = (ProjectApplicationVersion.query
            .filter_by(camp_session_id=sid, leader_user_id=user.id)
            .order_by(ProjectApplicationVersion.version.desc()).all())
    my_rows = (CampUnitMember.query
               .filter(CampUnitMember.user_id == user.id, CampUnitMember.status == 'active')
               .join(CampUnit, CampUnit.id == CampUnitMember.unit_id)
               .filter(CampUnit.camp_session_id == sid, CampUnit.unit_type == 'project').all())
    unit_ids = [r.unit_id for r in my_rows]
    units = {u.id: u for u in CampUnit.query.filter(CampUnit.id.in_(unit_ids)).all()} if unit_ids else {}
    names = _usernames({u.owner_user_id for u in units.values()} | {user.id})
    counts = dict(db.session.query(CampUnitMember.unit_id, db.func.count(CampUnitMember.id))
                  .filter(CampUnitMember.unit_id.in_(unit_ids), CampUnitMember.status == 'active')
                  .group_by(CampUnitMember.unit_id).all()) if unit_ids else {}
    leading, joining = [], []
    for r in my_rows:
        u = units.get(r.unit_id)
        if not u:
            continue
        d = _project_dict(u, ProjectProfile.query.filter_by(unit_id=u.id).first(), names,
                          member_count=counts.get(u.id, 0), my_role=r.role)
        (leading if r.role == 'leader' else joining).append(d)
    limit = _project_limit(camp)
    count = _active_project_count(sid, user.id)
    has_pending = any(a.status == 'pending' for a in apps)
    has_leading = any(r.role == 'leader' for r in my_rows)
    return jsonify({"code": 200,
                    "applications": [_app_dict(a, names) for a in apps],
                    "leading": leading, "joining": joining,
                    "project_count": count, "project_limit": limit,
                    "remaining_slots": max(0, (limit - count)) if limit is not None else None,
                    "can_apply": camp.status == 'upcoming' and not has_pending and not has_leading})


@bp.route("/projects/<int:sid>/applications", methods=["POST"])
@jwt_required()
@audit_log(operation="提交项目申报")
def application_submit(sid):
    """负责人提交申报（新版本；退回重提=新行不覆盖）。
    窗口仅 upcoming（09-12 拍板）；一人一营一个进行中申报 + 最多负责 1 个过审项目（Q-006）。"""
    user = _current_user()
    camp, err = _camp_or_404(sid)
    if err:
        return err
    if camp.category != 'project':
        return jsonify({"code": 400, "message": "该营期不是项目营"}), 400
    if not _camp_writable(camp):
        return jsonify({"code": 400, "message": "营期已归档，只读"}), 400
    if camp.status != 'upcoming':
        return jsonify({"code": 400, "message": "申报期已结束（项目申报仅在「待开放」阶段开放）"}), 400
    if user.is_admin():
        return jsonify({"code": 400, "message": "管理员不作为项目负责人申报；如需代管请使用成员账号"}), 400
    d = request.json or {}
    name = (d.get("name") or "").strip()
    if not name or len(name) > 100:
        return jsonify({"code": 400, "message": "请填写项目名（100 字内）"}), 400
    if ProjectApplicationVersion.query.filter_by(
            camp_session_id=sid, leader_user_id=user.id, status='pending').first():
        return jsonify({"code": 409, "message": "已有待审核的申报，请等待管理员处理（退回后可重提新版本）"}), 409
    if CampUnitMember.query.filter(
            CampUnitMember.user_id == user.id, CampUnitMember.role == 'leader',
            CampUnitMember.status == 'active'
    ).join(CampUnit, CampUnit.id == CampUnitMember.unit_id).filter(
            CampUnit.camp_session_id == sid).first():
        return jsonify({"code": 402, "message": "你已负责本营一个项目，不能再申报（一人最多负责 1 个）"}), 402
    latest = (ProjectApplicationVersion.query
              .filter_by(camp_session_id=sid, leader_user_id=user.id)
              .order_by(ProjectApplicationVersion.version.desc()).first())
    row = ProjectApplicationVersion(
        camp_session_id=sid, version=(latest.version + 1) if latest else 1,
        submitted_by=user.id, leader_user_id=user.id, name=name,
        background=d.get("background"), goal=d.get("goal"),
        required_abilities=d.get("required_abilities"),
        recruit_note=d.get("recruit_note"), plan=d.get("plan"))
    db.session.add(row)
    db.session.commit()
    return jsonify({"code": 200, "message": "申报已提交，等待管理员审核",
                    "application": _app_dict(row)})


@bp.route("/projects/<int:sid>/applications")
@jwt_required()
@camp_role()
def application_list(sid):
    """admin：本营全部申报（?status= 过滤，默认 all 按 pending 优先时间倒序）。"""
    camp, err = _camp_or_404(sid)
    if err:
        return err
    if camp.category != 'project':
        return jsonify({"code": 400, "message": "该营期不是项目营"}), 400
    status = request.args.get("status", "all")
    q = ProjectApplicationVersion.query.filter_by(camp_session_id=sid)
    if status != 'all':
        q = q.filter_by(status=status)
    rows = q.order_by(ProjectApplicationVersion.status.desc(),
                      ProjectApplicationVersion.created_at.desc()).all()
    names = _usernames({a.leader_user_id for a in rows})
    return jsonify({"code": 200, "applications": [_app_dict(a, names) for a in rows]})


@bp.route("/projects/<int:sid>/applications/<int:vid>/review", methods=["POST"])
@jwt_required()
@camp_role()
@audit_log(operation="审核项目申报")
def application_review(sid, vid):
    """管理员审核：approve=建 CampUnit+ProjectProfile+负责人关系生效+申报人自动入池；
    reject=记录原因，负责人可重提新版本。幂等：仅 pending 可审。"""
    camp, err = _camp_or_404(sid)
    if err:
        return err
    if camp.category != 'project':
        return jsonify({"code": 400, "message": "该营期不是项目营"}), 400
    if not _camp_writable(camp):
        return jsonify({"code": 400, "message": "营期已归档，只读"}), 400
    app = ProjectApplicationVersion.query.get(vid)
    if not app or app.camp_session_id != sid:
        return jsonify({"code": 404, "message": "申报不存在"}), 404
    if app.status != 'pending':
        return jsonify({"code": 400, "message": "该申报已处理"}), 400
    d = request.json or {}
    action = d.get("action")
    admin = _current_user()
    if action == 'approve':
        if CampUnitMember.query.filter(
                CampUnitMember.user_id == app.leader_user_id, CampUnitMember.role == 'leader',
                CampUnitMember.status == 'active'
        ).join(CampUnit, CampUnit.id == CampUnitMember.unit_id).filter(
                CampUnit.camp_session_id == sid).first():
            app.status, app.reject_reason = 'rejected', '审核时发现该负责人已负责其他项目'
            app.reviewed_by, app.reviewed_at = admin.id, datetime.now()
            db.session.commit()
            return jsonify({"code": 402, "message": "PROJECT_NOT_APPROVED 该负责人已负责本营其他项目，不可再负责新的"}), 402
        if CampUnit.query.filter_by(camp_session_id=sid, unit_type='project',
                                    name=app.name).first():
            return jsonify({"code": 400, "message": "已存在同名项目，请让负责人改名后重提"}), 400
        unit = CampUnit(camp_session_id=sid, unit_type='project',
                        name=app.name, owner_user_id=app.leader_user_id)
        db.session.add(unit)
        try:
            db.session.flush()
        except IntegrityError:
            db.session.rollback()
            return jsonify({"code": 400, "message": "已存在同名项目，请让负责人改名后重提"}), 400
        db.session.add(ProjectProfile(
            unit_id=unit.id, background=app.background, goal=app.goal,
            required_abilities=app.required_abilities, recruit_note=app.recruit_note,
            plan=app.plan, visibility='camp'))
        if not CampMember.query.filter_by(
                camp_session_id=sid, user_id=app.leader_user_id).first():
            db.session.add(CampMember(camp_session_id=sid, user_id=app.leader_user_id,
                                      role='member'))       # 过审自动入池（v1.3 §3.7）
        _add_unit_member(unit, app.leader_user_id, 'leader', 'approve', admin.id)
        app.status, app.unit_id = 'approved', unit.id
        app.reviewed_by, app.reviewed_at = admin.id, datetime.now()
        _notify(app.leader_user_id, "项目申报已通过",
                f"你在「{camp.name}」申报的项目「{app.name}」已通过审核，"
                f"你已成为该项目负责人，可开始在营期工作台管理项目。",
                sid, source_id=unit.id, important=True)
        db.session.commit()
        return jsonify({"code": 200, "message": "已通过：项目已创建，负责人关系生效",
                        "unit_id": unit.id})
    if action == 'reject':
        reason = (d.get("reason") or "").strip()
        if not reason:
            return jsonify({"code": 400, "message": "退回须填写原因（负责人重提时可见）"}), 400
        app.status, app.reject_reason = 'rejected', reason[:500]
        app.reviewed_by, app.reviewed_at = admin.id, datetime.now()
        _notify(app.leader_user_id, "项目申报被退回",
                f"你在「{camp.name}」申报的项目「{app.name}」被退回。原因：{reason}。"
                f"申报期内可修改后重提新版本。", sid, important=True)
        db.session.commit()
        return jsonify({"code": 200, "message": "已退回"})
    return jsonify({"code": 400, "message": "action 仅支持 approve/reject"}), 400


# ─────────────────────────────────────────────
# 组队浏览 / 项目志愿 / 导出 / 批量回填
# ─────────────────────────────────────────────

@bp.route("/projects/<int:sid>/list")
@jwt_required()
def project_list(sid):
    """过审项目列表（营内成员/admin；组队浏览与负责人工作区共用）。
    带请求者视角 my_role / my_pref_rank / member_count。"""
    user = _current_user()
    camp, err = _camp_or_404(sid)
    if err:
        return err
    if camp.category != 'project':
        return jsonify({"code": 400, "message": "该营期不是项目营"}), 400
    is_member = CampMember.query.filter_by(camp_session_id=sid, user_id=user.id).first()
    if not is_member and not user.is_admin():
        return jsonify({"code": 403, "message": "CAMP_PARTICIPANT_REQUIRED 加入营期后可浏览项目"}), 403
    units = (CampUnit.query.filter_by(camp_session_id=sid, unit_type='project')
             .order_by(CampUnit.created_at).all())
    profiles = {p.unit_id: p for p in ProjectProfile.query.filter(
        ProjectProfile.unit_id.in_([u.id for u in units])).all()} if units else {}
    names = _usernames({u.owner_user_id for u in units})
    counts = dict(db.session.query(CampUnitMember.unit_id, db.func.count(CampUnitMember.id))
                  .filter(CampUnitMember.unit_id.in_([u.id for u in units]),
                          CampUnitMember.status == 'active')
                  .group_by(CampUnitMember.unit_id).all()) if units else {}
    my_roles = {r.unit_id: r.role for r in CampUnitMember.query.filter(
        CampUnitMember.user_id == user.id, CampUnitMember.status == 'active',
        CampUnitMember.unit_id.in_([u.id for u in units])).all()} if units else {}
    my_prefs = {p.unit_id: p.rank for p in CampProjectPreference.query.filter_by(
        camp_session_id=sid, student_user_id=user.id).all()}
    return jsonify({"code": 200, "selection_open": camp.status == 'selecting',
                    "projects": [_project_dict(u, profiles.get(u.id), names,
                                               member_count=counts.get(u.id, 0),
                                               my_role=my_roles.get(u.id),
                                               my_pref_rank=my_prefs.get(u.id))
                                 for u in units]})


@bp.route("/projects/<int:sid>/preferences", methods=["POST"])
@jwt_required()
@audit_log(operation="提交项目志愿")
def preference_submit(sid):
    """学员提交项目志愿（单轮 1-3 有序；提交=整组替换，selecting 期内可改）。
    body: {preferences: [{unit_id, note?}]} —— rank 由数组顺序推导（1 起）。"""
    user = _current_user()
    camp, err = _camp_or_404(sid)
    if err:
        return err
    if camp.category != 'project':
        return jsonify({"code": 400, "message": "该营期不是项目营"}), 400
    if not _camp_writable(camp):
        return jsonify({"code": 400, "message": "营期已归档，只读"}), 400
    if camp.status != 'selecting':
        return jsonify({"code": 400, "message": "项目志愿仅在「选择阶段」开放提交"}), 400
    if not CampMember.query.filter_by(camp_session_id=sid, user_id=user.id).first():
        return jsonify({"code": 403, "message": "CAMP_PARTICIPANT_REQUIRED 加入营期后可填报志愿"}), 403
    prefs = (request.json or {}).get("preferences")
    if not isinstance(prefs, list) or not prefs:
        return jsonify({"code": 400, "message": "缺少 preferences 数组（1-3 项，按意愿排序）"}), 400
    if len(prefs) > 3:
        return jsonify({"code": 400, "message": "项目志愿最多 3 个"}), 400
    unit_ids, seen = [], set()
    for it in prefs:
        uid = it.get("unit_id") if isinstance(it, dict) else None
        if not uid or uid in seen:
            return jsonify({"code": 400, "message": "preferences 须为不含重复的 unit_id 列表"}), 400
        seen.add(uid)
        unit_ids.append(uid)
    units = {u.id: u for u in CampUnit.query.filter(
        CampUnit.id.in_(unit_ids), CampUnit.camp_session_id == sid,
        CampUnit.unit_type == 'project').all()}
    for uid in unit_ids:
        u = units.get(uid)
        if not u or u.status == 'terminated':
            return jsonify({"code": 400, "message": f"PROJECT_NOT_APPROVED 项目 {uid} 不存在或已终止"}), 400
    CampProjectPreference.query.filter_by(
        camp_session_id=sid, student_user_id=user.id).delete(synchronize_session=False)
    for i, it in enumerate(prefs):
        db.session.add(CampProjectPreference(
            camp_session_id=sid, student_user_id=user.id,
            unit_id=it["unit_id"], rank=i + 1,
            note=((it.get("note") or "").strip() or None) if isinstance(it, dict) else None))
    db.session.commit()
    return jsonify({"code": 200, "message": f"已提交 {len(prefs)} 个项目志愿"})


@bp.route("/projects/<int:sid>/preferences/mine")
@jwt_required()
def preference_mine(sid):
    user = _current_user()
    camp, err = _camp_or_404(sid)
    if err:
        return err
    rows = (CampProjectPreference.query
            .filter_by(camp_session_id=sid, student_user_id=user.id)
            .order_by(CampProjectPreference.rank).all())
    units = {u.id: u for u in CampUnit.query.filter(
        CampUnit.id.in_([r.unit_id for r in rows])).all()} if rows else {}
    names = _usernames({u.owner_user_id for u in units.values()})
    data = [{"rank": r.rank, "unit_id": r.unit_id, "note": r.note,
             "name": units[r.unit_id].name if r.unit_id in units else None,
             "leader_name": names.get(units[r.unit_id].owner_user_id) if r.unit_id in units else None,
             "status": units[r.unit_id].status if r.unit_id in units else None}
            for r in rows]
    return jsonify({"code": 200, "preferences": data,
                    "editable": camp is not None and camp.status == 'selecting'})


@bp.route("/projects/<int:sid>/export")
@jwt_required()
@camp_role()
@audit_log(operation="导出项目志愿")
def preference_export(sid):
    """导出项目志愿 CSV（utf-8-sig 带 BOM）：老师线下协调用。
    每学员一行；志愿 1-3 填项目名（缺位留空），留言取 rank1，当前已加入项目列出全部。"""
    camp, err = _camp_or_404(sid)
    if err:
        return err
    if camp.category != 'project':
        return jsonify({"code": 400, "message": "该营期不是项目营"}), 400
    members = CampMember.query.filter(
        CampMember.camp_session_id == sid,
        CampMember.role.in_(('member', 'student'))).all()
    prefs = {}
    for r in (CampProjectPreference.query.filter_by(camp_session_id=sid)
              .order_by(CampProjectPreference.rank).all()):
        prefs.setdefault(r.student_user_id, []).append(r)
    unit_ids = {r.unit_id for r in prefs}
    joined = {}
    for um in (CampUnitMember.query
               .filter(CampUnitMember.status == 'active')
               .join(CampUnit, CampUnit.id == CampUnitMember.unit_id)
               .filter(CampUnit.camp_session_id == sid,
                       CampUnit.unit_type == 'project',
                       CampUnitMember.user_id.in_([m.user_id for m in members])).all()):
        joined.setdefault(um.user_id, []).append(um.unit_id)
    unit_ids |= {uid for lst in joined.values() for uid in lst}
    units = {u.id: u for u in CampUnit.query.filter(CampUnit.id.in_(unit_ids)).all()} if unit_ids else {}
    names = _usernames([m.user_id for m in members] + [u.owner_user_id for u in units.values()])

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["学员ID", "学员姓名", "志愿1", "志愿2", "志愿3", "留言", "当前已加入项目"])
    for m in members:
        plist = prefs.get(m.user_id, [])
        row_names = []
        for i in range(3):
            u = units.get(plist[i].unit_id) if i < len(plist) else None
            row_names.append(u.name if u else "")
        note = (plist[0].note or "") if plist else ""
        joined_names = "、".join(
            units[uid].name for uid in joined.get(m.user_id, []) if uid in units)
        w.writerow([m.user_id, names.get(m.user_id, ""),
                    *row_names, note, joined_names])
    resp = Response(buf.getvalue().encode("utf-8-sig"), mimetype="text/csv")
    resp.headers["Content-Disposition"] = f"attachment; filename=camp_{sid}_project_preferences.csv"
    return resp


@bp.route("/projects/<int:sid>/assign/batch", methods=["POST"])
@jwt_required()
@camp_role()
@audit_log(operation="批量回填项目成员")
def assign_batch(sid):
    """老师批量回填线下协调结果：body {items:[{unit_id, user_id},...]}。
    窗口=selecting（开营后变更走管理员通道端点）；逐项校验+行锁+3 上限，逐项独立成败。
    结果 status：assigned / skipped（已在项目内）/ conflict（3 上限）/ error（前置校验失败）。"""
    camp, err = _camp_or_404(sid)
    if err:
        return err
    if camp.category != 'project':
        return jsonify({"code": 400, "message": "该营期不是项目营"}), 400
    if not _camp_writable(camp):
        return jsonify({"code": 400, "message": "营期已归档，只读"}), 400
    if camp.status != 'selecting':
        return jsonify({"code": 400, "message": "CAMP_FORMATION_CLOSED 批量回填仅在「选择阶段」开放，开营后变更请走成员管理"}), 400
    items = (request.json or {}).get("items")
    if not isinstance(items, list) or not items:
        return jsonify({"code": 400, "message": "缺少 items 数组"}), 400
    if len(items) > 500:
        return jsonify({"code": 400, "message": "单次批量上限 500 项"}), 400
    limit = _project_limit(camp)
    admin = _current_user()
    results = []
    norm = [x if isinstance(x, dict) else {} for x in items]
    for it in sorted(norm, key=lambda x: (x.get("user_id") or 0)):   # 按 user 排序加锁防死锁
        uid, unit_id = it.get("user_id"), it.get("unit_id")
        item = {"user_id": uid, "unit_id": unit_id}
        if not uid or not unit_id:
            results.append({**item, "status": "error", "message": "缺少 user_id/unit_id"})
            continue
        unit, camp2, err2 = _unit_or_404(unit_id)
        if err2 or camp2.id != sid:
            results.append({**item, "status": "error", "message": "项目不存在"})
            continue
        if unit.status != 'active':
            results.append({**item, "status": "error",
                            "message": f"项目状态为 {unit.status}，不可加人"})
            continue
        member = _lock_member(sid, uid)
        if not member or member.role not in ('member', 'student'):
            results.append({**item, "status": "error", "message": "该用户不在本营（须先入池）"})
            continue
        if CampUnitMember.query.filter_by(unit_id=unit.id, user_id=uid,
                                          status='active').first():
            results.append({**item, "status": "skipped", "message": "已在该项目中，跳过"})
            continue
        if limit is not None and _active_project_count(sid, uid) >= limit:
            results.append({**item, "status": "conflict",
                            "message": f"PROJECT_MEMBERSHIP_LIMIT_REACHED 该成员已参与 {limit} 个项目（上限含负责的项目）"})
            continue
        _add_unit_member(unit, uid, 'member', 'admin', admin.id)
        _notify(uid, "已加入项目",
                f"老师已将你加入「{camp.name}」项目「{unit.name}」。", sid, source_id=unit.id)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            results.append({**item, "status": "error", "message": "写入冲突（并发回填），请重试"})
            continue
        results.append({**item, "status": "assigned", "message": "已加入"})
    return jsonify({"code": 200, "results": results})


# ─────────────────────────────────────────────
# 统一勾选 roster API（unit 通用型；首期 project 消费，学习营现役流首期不迁，v1.3 §3.8 注）
# ─────────────────────────────────────────────

@bp.route("/units/<int:uid>/selection-roster")
@jwt_required()
def selection_roster(uid):
    """负责人（或 admin）的项目勾选候选名单：本营成员、已选状态、3 上限余量、
    对本项目的志愿序、当前各项目身份；version 供 PUT 乐观锁。"""
    user = _current_user()
    unit, camp, err = _unit_or_404(uid)
    if err:
        return err
    if not _is_unit_leader(unit, user):
        return jsonify({"code": 403, "message": "UNIT_MANAGEMENT_FORBIDDEN 仅项目负责人或管理员可查看名单"}), 403
    members_now = (CampUnitMember.query.filter_by(unit_id=unit.id, status='active')
                   .order_by(CampUnitMember.role.desc(), CampUnitMember.started_at).all())
    member_ids = {m.user_id for m in members_now}
    pool = CampMember.query.filter(
        CampMember.camp_session_id == camp.id,
        CampMember.role.in_(('member', 'student'))).all()
    pool_ids = [m.user_id for m in pool]
    active_rows = (CampUnitMember.query
                   .filter(CampUnitMember.user_id.in_(pool_ids),
                           CampUnitMember.status == 'active')
                   .join(CampUnit, CampUnit.id == CampUnitMember.unit_id)
                   .filter(CampUnit.camp_session_id == camp.id,
                           CampUnit.unit_type == 'project').all()) if pool_ids else []
    cnt = {}
    for r in active_rows:
        cnt[r.user_id] = cnt.get(r.user_id, 0) + 1
    unit_names = {u.id: u.name for u in CampUnit.query.filter(
        CampUnit.id.in_({r.unit_id for r in active_rows})).all()} if active_rows else {}
    prefs = {p.student_user_id: p.rank for p in CampProjectPreference.query.filter_by(
        camp_session_id=camp.id, unit_id=unit.id).all()}
    names = _usernames(pool_ids + list(member_ids) + [unit.owner_user_id])
    limit = _project_limit(camp)
    candidates = []
    for m in pool:
        if m.user_id in member_ids:
            continue
        count = cnt.get(m.user_id, 0)
        candidates.append({
            "user_id": m.user_id, "username": names.get(m.user_id, str(m.user_id)),
            "project_count": count,
            "remaining_slots": max(0, limit - count) if limit is not None else None,
            "my_pref_rank": prefs.get(m.user_id),
            "units": [{"unit_id": r.unit_id, "name": unit_names.get(r.unit_id),
                       "role": r.role} for r in active_rows if r.user_id == m.user_id],
        })
    return jsonify({"code": 200,
                    "unit": {"unit_id": unit.id, "name": unit.name, "status": unit.status,
                             "camp_session_id": camp.id},
                    "selection_open": camp.status == 'selecting' and unit.status == 'active',
                    "version": _unit_version(unit.id),
                    "members": [{"user_id": m.user_id,
                                 "username": names.get(m.user_id, str(m.user_id)),
                                 "role": m.role,
                                 "started_at": m.started_at.isoformat() if m.started_at else None}
                                for m in members_now],
                    "candidates": candidates})


@bp.route("/units/<int:uid>/member-selection", methods=["PUT"])
@jwt_required()
@audit_log(operation="单元成员勾选")
def member_selection(uid):
    """负责人普通勾选（原子整批）：body {expected_version, idempotency_key, operations:[{user_id, op}]}。
    op=add/remove；逐项返回 added/removed/unchanged/conflict/forbidden。
    幂等键重放返回上次结果（redis 24h）；expected_version 不匹配报 SELECTION_VERSION_CONFLICT。
    窗口=selecting 且单元 active（H-005 定稿：开营后普通勾选关闭，变更走管理员通道）。"""
    user = _current_user()
    unit, camp, err = _unit_or_404(uid)
    if err:
        return err
    if not _is_unit_leader(unit, user):
        return jsonify({"code": 403, "message": "UNIT_MANAGEMENT_FORBIDDEN 仅项目负责人或管理员可勾选成员"}), 403
    if not _camp_writable(camp):
        return jsonify({"code": 400, "message": "CAMP_ARCHIVED_READ_ONLY 营期已归档，只读"}), 400
    if camp.status != 'selecting':
        return jsonify({"code": 400, "message": "CAMP_FORMATION_CLOSED 已开营，普通勾选关闭；成员变更请管理员处理"}), 400
    if unit.status != 'active':
        return jsonify({"code": 400, "message": f"项目状态为 {unit.status}，不可勾选"}), 400
    d = request.json or {}
    ops = d.get("operations")
    idem = (d.get("idempotency_key") or "").strip()
    expected = d.get("expected_version")
    if not isinstance(ops, list) or not ops:
        return jsonify({"code": 400, "message": "缺少 operations 数组"}), 400
    if not idem:
        return jsonify({"code": 400, "message": "缺少 idempotency_key"}), 400
    if not isinstance(expected, int):
        return jsonify({"code": 400, "message": "缺少 expected_version（整型，取 roster.version）"}), 400
    idem_key = f"proj:msel:{unit.id}:{idem}"
    try:
        cached = redis_client.get(idem_key)
        if cached:
            return jsonify(json.loads(cached))          # 幂等重放：原样返回上次结果
    except Exception:
        cached = None                                    # redis 不可用降级：仍有乐观锁保护
    if expected != _unit_version(unit.id):
        return jsonify({"code": 409, "message": "SELECTION_VERSION_CONFLICT 名单已变化，请刷新后重试",
                        "current_version": _unit_version(unit.id)}), 409
    limit = _project_limit(camp)
    results = []
    # 锁定涉及用户（按 id 排序防死锁），再逐项处理
    user_ids = sorted({o.get("user_id") for o in ops if isinstance(o, dict) and o.get("user_id")})
    locked = {m.user_id: m for u in user_ids
              if (m := _lock_member(camp.id, u)) is not None}
    for o in ops:
        target, op = (o.get("user_id") if isinstance(o, dict) else None), \
                     (o.get("op") if isinstance(o, dict) else None)
        item = {"user_id": target, "op": op}
        if op not in ('add', 'remove'):
            results.append({**item, "result": "forbidden", "message": "op 仅支持 add/remove"})
            continue
        row = CampUnitMember.query.filter_by(unit_id=unit.id, user_id=target).first()
        if op == 'add':
            member = locked.get(target)
            if not member or member.role not in ('member', 'student'):
                results.append({**item, "result": "forbidden", "message": "非本营成员，不可勾选"})
                continue
            if row and row.status == 'active':
                results.append({**item, "result": "unchanged", "message": "已在本项目"})
                continue
            if limit is not None and _active_project_count(camp.id, target) >= limit:
                results.append({**item, "result": "conflict",
                                "message": f"PROJECT_MEMBERSHIP_LIMIT_REACHED 该成员参与数已达上限 {limit}"})
                continue
            _add_unit_member(unit, target, 'member', 'leader_pick', user.id)
            results.append({**item, "result": "added"})
        else:
            if not row or row.status != 'active':
                results.append({**item, "result": "unchanged", "message": "不在本项目"})
                continue
            if row.role == 'leader':
                results.append({**item, "result": "forbidden",
                                "message": "负责人不可被移除，请先变更负责人"})
                continue
            _end_unit_member(unit, row, 'deselect', 'leader_pick', user.id, None)
            results.append({**item, "result": "removed"})
    db.session.commit()
    new_version = _unit_version(unit.id)
    payload = {"code": 200, "version": new_version, "results": results}
    try:
        redis_client.setex(idem_key, 86400, json.dumps(payload, ensure_ascii=False))
    except Exception:
        pass
    return jsonify(payload)


# ─────────────────────────────────────────────
# 变更场景（管理员通道：原因必填 + 事件留痕 + 行状态化 ended；selecting/running 均可用）
# ─────────────────────────────────────────────

def _admin_reason(d):
    return (d.get("reason") or "").strip()[:500]


@bp.route("/units/<int:uid>/members/<int:target>/end", methods=["POST"])
@jwt_required()
@camp_role()
@audit_log(operation="移除项目成员")
def member_end(uid, target):
    """管理员移除/接受退出项目成员（H-005：开营前后通用变更通道）。
    body: {reason 必填, kind?: 'exit'成员自助线下申请|'remove'管理员移除}。
    行状态化 ended（历史提交与贡献保留），3 上限名额即时释放。"""
    unit, camp, err = _unit_or_404(uid)
    if err:
        return err
    if not _camp_writable(camp):
        return jsonify({"code": 400, "message": "CAMP_ARCHIVED_READ_ONLY 营期已归档，只读"}), 400
    d = request.json or {}
    reason = _admin_reason(d)
    if not reason:
        return jsonify({"code": 400, "message": "管理员通道变更须填写原因（留痕）"}), 400
    row = CampUnitMember.query.filter_by(unit_id=unit.id, user_id=target).first()
    if not row or row.status != 'active':
        return jsonify({"code": 404, "message": "该成员不在本项目（或已退出）"}), 404
    if row.role == 'leader':
        return jsonify({"code": 400, "message": "负责人不可直接移除，请先变更负责人"}), 400
    action = 'exit' if d.get("kind") == 'exit' else 'remove'
    _end_unit_member(unit, row, action, 'admin_adjust', _current_user().id, reason)
    _notify(target, "你已退出项目",
            f"管理员将你移出「{camp.name}」项目「{unit.name}」。原因：{reason}",
            camp.id, source_id=unit.id)
    db.session.commit()
    return jsonify({"code": 200, "message": "已移除（历史贡献保留，参与名额已释放）"})


@bp.route("/units/<int:uid>/leader", methods=["POST"])
@jwt_required()
@camp_role()
@audit_log(operation="变更项目负责人")
def leader_change(uid):
    """变更项目负责人（线下指定 → admin 操作生效，事件留痕）。
    body: {new_leader_id, reason 必填}。旧负责人转为本项目普通成员（贡献保留）；
    新负责人不在营期池时自动入池（role='member'）。"""
    unit, camp, err = _unit_or_404(uid)
    if err:
        return err
    if not _camp_writable(camp):
        return jsonify({"code": 400, "message": "CAMP_ARCHIVED_READ_ONLY 营期已归档，只读"}), 400
    d = request.json or {}
    reason = _admin_reason(d)
    new_leader = d.get("new_leader_id")
    if not reason:
        return jsonify({"code": 400, "message": "管理员通道变更须填写原因（留痕）"}), 400
    if not new_leader:
        return jsonify({"code": 400, "message": "缺少 new_leader_id"}), 400
    new_user = UserModel.query.get(new_leader)
    if not new_user:
        return jsonify({"code": 404, "message": "新负责人用户不存在"}), 404
    if new_user.is_admin():
        return jsonify({"code": 400, "message": "管理员不作为项目负责人"}), 400
    if new_leader == unit.owner_user_id:
        return jsonify({"code": 400, "message": "该用户已是本项目负责人"}), 400
    # 新负责人若已有其他负责项目 → 拒（一人一营最多负责 1 个）
    if CampUnitMember.query.filter(
            CampUnitMember.user_id == new_leader, CampUnitMember.role == 'leader',
            CampUnitMember.status == 'active'
    ).join(CampUnit, CampUnit.id == CampUnitMember.unit_id).filter(
            CampUnit.camp_session_id == camp.id,
            CampUnit.id != unit.id).first():
        return jsonify({"code": 402, "message": "PROJECT_NOT_APPROVED 该用户已负责本营其他项目（一人最多负责 1 个）"}), 402
    old_leader = unit.owner_user_id
    admin = _current_user()
    member = _lock_member(camp.id, new_leader)
    if not member:
        db.session.add(CampMember(camp_session_id=camp.id, user_id=new_leader, role='member'))
    elif member.role not in ('member', 'student'):
        return jsonify({"code": 400, "message": "新负责人营内身份异常"}), 400
    # 3 上限：新负责人不在本项目时计入 +1
    already = CampUnitMember.query.filter_by(
        unit_id=unit.id, user_id=new_leader, status='active').first()
    limit = _project_limit(camp)
    if not already and limit is not None and _active_project_count(camp.id, new_leader) >= limit:
        return jsonify({"code": 409, "message": f"PROJECT_MEMBERSHIP_LIMIT_REACHED 新负责人参与数已达上限 {limit}"}), 409
    old_row = CampUnitMember.query.filter_by(unit_id=unit.id, user_id=old_leader).first()
    if old_row and old_row.status == 'active':
        old_row.role = 'member'                      # 旧负责人转普通成员，贡献保留
    unit.owner_user_id = new_leader
    _add_unit_member(unit, new_leader, 'leader', 'admin_adjust', admin.id, reason=reason)
    _add_event(camp.id, unit.id, new_leader, 'leader_change', 'admin_adjust', admin.id,
               before={"leader": old_leader}, after={"leader": new_leader}, reason=reason)
    _notify(new_leader, "你已成为项目负责人",
            f"管理员指定你为「{camp.name}」项目「{unit.name}」的负责人。", camp.id,
            source_id=unit.id, important=True)
    _notify(old_leader, "项目负责人已变更",
            f"「{unit.name}」的负责人已变更为他人，你转为本项目成员（历史贡献保留）。原因：{reason}",
            camp.id, source_id=unit.id)
    db.session.commit()
    return jsonify({"code": 200, "message": "负责人已变更（原负责人转为成员）"})


@bp.route("/units/<int:uid>/status", methods=["POST"])
@jwt_required()
@camp_role()
@audit_log(operation="变更项目状态")
def unit_status(uid):
    """项目暂停/终止/恢复：body {status: 'paused'|'terminated'|'active', reason 必填}。
    terminated=解散：全员行状态化 ended（3 上限名额释放、历史贡献保留），不可恢复；
    paused=暂停：仅单元状态（里程碑冻结在阶段4 消费），可恢复 active。"""
    unit, camp, err = _unit_or_404(uid)
    if err:
        return err
    if not _camp_writable(camp):
        return jsonify({"code": 400, "message": "CAMP_ARCHIVED_READ_ONLY 营期已归档，只读"}), 400
    d = request.json or {}
    reason = _admin_reason(d)
    status = d.get("status")
    if not reason:
        return jsonify({"code": 400, "message": "管理员通道变更须填写原因（留痕）"}), 400
    if status not in ('active', 'paused', 'terminated'):
        return jsonify({"code": 400, "message": "status 仅支持 active/paused/terminated"}), 400
    if unit.status == 'terminated':
        return jsonify({"code": 400, "message": "项目已终止，不可恢复（如需重开请负责人重新申报）"}), 400
    if status == unit.status:
        return jsonify({"code": 400, "message": f"项目已是 {status} 状态"}), 400
    admin = _current_user()
    prev_status = unit.status
    unit.status = status
    _add_event(camp.id, unit.id, unit.owner_user_id, 'unit_status', 'admin_adjust',
               admin.id, before={"unit_status": prev_status},
               after={"unit_status": status}, reason=reason)
    if status == 'terminated':
        for row in CampUnitMember.query.filter_by(unit_id=unit.id, status='active').all():
            if row.role == 'leader':
                row.status, row.ended_at = 'ended', datetime.now()   # 负责人关系一并结束（历史保留）
            else:
                _end_unit_member(unit, row, 'remove', 'admin_adjust', admin.id, f"项目终止：{reason}")
        _notify(unit.owner_user_id, "项目已终止",
                f"「{camp.name}」项目「{unit.name}」已被管理员终止。原因：{reason}",
                camp.id, source_id=unit.id, important=True)
    elif status == 'paused':
        _notify(unit.owner_user_id, "项目已暂停",
                f"「{camp.name}」项目「{unit.name}」已被管理员暂停。原因：{reason}",
                camp.id, source_id=unit.id, important=True)
    else:
        _notify(unit.owner_user_id, "项目已恢复",
                f"「{camp.name}」项目「{unit.name}」已恢复进行。原因：{reason}",
                camp.id, source_id=unit.id)
    db.session.commit()
    return jsonify({"code": 200, "message": f"项目状态已改为 {status}"})


# ─────────────────────────────────────────────
# 团队总览（admin：本营全部项目与成员）
# ─────────────────────────────────────────────

@bp.route("/projects/<int:sid>/overview")
@jwt_required()
@camp_role()
def project_overview(sid):
    """admin 团队总览：本营全部项目 + 各成员名单 + 事件流（最近 200 条）。"""
    camp, err = _camp_or_404(sid)
    if err:
        return err
    if camp.category != 'project':
        return jsonify({"code": 400, "message": "该营期不是项目营"}), 400
    units = (CampUnit.query.filter_by(camp_session_id=sid, unit_type='project')
             .order_by(CampUnit.created_at).all())
    unit_ids = [u.id for u in units]
    profiles = {p.unit_id: p for p in ProjectProfile.query.filter(
        ProjectProfile.unit_id.in_(unit_ids)).all()} if unit_ids else {}
    rows = (CampUnitMember.query
            .filter(CampUnitMember.unit_id.in_(unit_ids))
            .order_by(CampUnitMember.status, CampUnitMember.role.desc()).all()) if unit_ids else []
    names = _usernames({u.owner_user_id for u in units} |
                       {r.user_id for r in rows})
    counts = dict(db.session.query(CampUnitMember.unit_id, db.func.count(CampUnitMember.id))
                  .filter(CampUnitMember.unit_id.in_(unit_ids), CampUnitMember.status == 'active')
                  .group_by(CampUnitMember.unit_id).all()) if unit_ids else {}
    projects = []
    for u in units:
        d = _project_dict(u, profiles.get(u.id), names, member_count=counts.get(u.id, 0))
        d["members"] = [{
            "user_id": r.user_id, "username": names.get(r.user_id, str(r.user_id)),
            "role": r.role, "status": r.status,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "ended_at": r.ended_at.isoformat() if r.ended_at else None,
        } for r in rows if r.unit_id == u.id]
        projects.append(d)
    events = (CampMembershipEvent.query.filter_by(camp_session_id=sid)
              .order_by(CampMembershipEvent.id.desc()).limit(200).all())
    return jsonify({"code": 200, "projects": projects,
                    "events": [{"id": e.id, "unit_id": e.unit_id, "user_id": e.user_id,
                                "username": names.get(e.user_id, str(e.user_id)),
                                "action": e.action, "source": e.source,
                                "reason": e.reason,
                                "occurred_at": e.occurred_at.isoformat() if e.occurred_at else None}
                               for e in events]})

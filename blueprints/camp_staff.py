"""营期工作人员（CampStaff）蓝图 — 老师身份与营期负责人架构·阶段 1（2026-09-20，migrate_42）。

配套文档：docs/计划/培训营老师身份与营期负责人架构设计方案.md

三条不可破坏的边界（方案 §1.3）：
- 平台权限不等于营期职责：老师只管被委任的营，不进管理端、不碰平台资源；
- 治理人员不等于参与成员：CampStaff 与 CampMember 平行，成员统计/承诺日/选导生不涉及老师；
- 前端视角不等于后端授权：camp_access 每次请求独立核对本营真实授权（无缓存，解除即生效）。

权限策略层（方案 §5.2）：中央权限表 STAFF_PERMISSIONS，@camp_access(permission) 收口判定；
现有 @camp_role() 端点按方案逐个迁移（不一次性放开，防静默扩权）。本轮迁移：报名审批、
成员管理、请假/考勤全营视图；营期状态迁移恒留 super_admin（2026-09-20 拍板 P-01）。

主负责人唯一性（方案 §3.3）：owner 变更（委任/转交）在同一事务内先 FOR UPDATE 锁营期行，
再结束旧 owner、落新行；owner 只从活跃 CampStaff 行读取，不在 CampSession 上存副本。
"""
from datetime import datetime
from functools import wraps

from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required
from sqlalchemy import text

from exts import db
from models import CampSession, CampStaff, CampStaffEvent, UserModel

from . import audit_log, _current_user
from .notification import create_notification

bp = Blueprint("camp_staff", __name__, url_prefix="/camp")


# ─────────────────────────────────────────────
# 权限策略层（CampAccessService 首期形态）
# ─────────────────────────────────────────────

# 中央权限表（方案 §5.2）。owner 比 teacher 多：委任老师、选导生管理、请假兜底、
# 营期配置。
# 2026-09-20 产品拍板（P-01/P-04）：owner 只负责营期日常管理——开营/结营（session
# transition）与主负责人转交恒为 super_admin 权限，不进入本表。
STAFF_PERMISSIONS = {
    'owner': {
        'application.review',      # 审批学员/导生报名
        'member.manage',           # 成员直接添加、移除、改派
        'mentor_selection.manage', # 选导生配置
        'mentor_selection.operate',# 选导生运营（指派/批量回填/导出；manage ⊃ operate）
        'learning.read_all',       # 全营学习进度
        'attendance.read_all',     # 全营考勤
        'leave.override',          # 全营请假兜底审批
        'meeting.read_all',        # 全营组会
        'reward.issue',            # 发放营期奖励
        'announcement.publish',    # 发布营期公告
        'session.configure',       # 编辑营期基本信息
        'staff.manage',            # 委任/解除协同老师
    },
    'teacher': {
        'application.review',
        'member.manage',
        'mentor_selection.operate',
        'learning.read_all',
        'attendance.read_all',
        'leave.escalation',
        'meeting.read_all',
        'reward.issue',
        'announcement.publish',
    },
}

VALID_STAFF_ROLES = ('owner', 'teacher')


def camp_staff_row(sid, user_id):
    """用户在本营的活跃 CampStaff 行（无则 None）。鉴权不缓存——委任/解除即时生效（方案 §10.2）。"""
    return CampStaff.query.filter_by(
        camp_session_id=sid, user_id=user_id, status='active').first()


def staff_permissions(role):
    """营期工作人员角色的权限集合（未知角色空集）。"""
    return STAFF_PERMISSIONS.get(role, set())


def has_camp_access(user, sid, permission):
    """策略判定：super_admin 恒过；本营 active staff 按角色权限集判定。"""
    if user.is_admin():
        return True
    row = camp_staff_row(sid, user.id)
    return bool(row and permission in staff_permissions(row.role))


def camp_access(permission):
    """营期策略门禁装饰器（与 camp_role 平行，不替代）：
    super_admin 通过；本营 active CampStaff 且角色含该权限通过；其余 403。
    数据范围由端点内 sid 边界保证——不提供「老师查全部营期」的通道（方案 §5.3）。"""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            user = _current_user()
            if not user:
                return jsonify({"code": 401, "message": "用户未认证"}), 401
            if not has_camp_access(user, kwargs.get('sid'), permission):
                return jsonify({"code": 403, "message": "需要本营负责人权限"}), 403
            return func(*args, **kwargs)
        return wrapper
    return decorator


def active_staff_ids(sid):
    """本营活跃工作人员 user_id 列表（owner 在前——通知接收顺序即责任顺序）。"""
    rows = CampStaff.query.filter_by(camp_session_id=sid, status='active') \
        .order_by(CampStaff.role.desc()).all()      # teacher < owner 字典序，desc 让 owner 在前
    return [r.user_id for r in rows]


def camp_responsible_ids(sid):
    """管理侧通知接收人责任链（方案 §7.2）：本营 owner+teachers；
    无任何活跃工作人员的营才兜底 super_admin（迁移期兼容，委任完成后可关）。"""
    ids = active_staff_ids(sid)
    if ids:
        return ids
    return [u.id for u in UserModel.query.filter(
        UserModel.role == 'super_admin',
        UserModel.status.is_(None) | (UserModel.status != 'banned')).all()]


# ─────────────────────────────────────────────
# 辅助
# ─────────────────────────────────────────────

def _camp_or_404(sid):
    camp = CampSession.query.get(sid)
    if not camp or camp.status == 'deleted':
        return None, (jsonify({"code": 404, "message": "营期不存在"}), 404)
    return camp, None


def _lock_camp(sid):
    """事务内锁营期行（串行化 owner 变更，方案 §3.3；MySQL InnoDB 行锁）。"""
    db.session.execute(text("SELECT id FROM camp_session WHERE id = :sid FOR UPDATE"),
                       {"sid": sid})


def _staff_event(sid, uid, action, before_role, after_role, operator_id, reason=None):
    db.session.add(CampStaffEvent(
        camp_session_id=sid, user_id=uid, action=action,
        before_role=before_role, after_role=after_role,
        operator_id=operator_id, reason=(reason or None) or None))


def _notify_staff_assigned(camp, uid, role, operator):
    """委任通知（方案 §10.1）：直达老师工作台（perspective=teacher 深链）。"""
    u = UserModel.query.get(uid)
    if not u:
        return
    is_owner = role == 'owner'
    create_notification(
        uid, "营期负责人委任",
        f"你被 {operator.username} 委任为「{camp.name}」的"
        f"{'主负责人（第一责任人）' if is_owner else '协同老师'}，"
        f"点击进入老师工作台处理报名、成员与营期运营。",
        category='camp', source_type='camp_staff', source_id=camp.id,
        camp_session_id=camp.id, is_important=is_owner)


def _notify_staff_ended(camp, uid, role, operator, reason):
    u = UserModel.query.get(uid)
    if not u:
        return
    content = f"{operator.username} 解除了你在「{camp.name}」的{'主负责人' if role == 'owner' else '协同老师'}职责。"
    if reason:
        content += f"原因：{reason}。"
    create_notification(uid, "营期职责已解除", content,
                        category='camp', source_type='camp_staff', source_id=camp.id,
                        camp_session_id=camp.id, is_important=True)


# ─────────────────────────────────────────────
# 工作人员管理 API（方案 §5.4）
# ─────────────────────────────────────────────

@bp.route("/sessions/<int:sid>/staff")
@jwt_required()
def staff_list(sid):
    """本营工作人员（含已结束行，ended 标记）+ 职责事件时间线。
    super_admin 或本营活跃 staff 可看；普通成员/外人 403（防组织信息外泄）。"""
    camp, err = _camp_or_404(sid)
    if err:
        return err
    user = _current_user()
    if not user.is_admin() and not camp_staff_row(sid, user.id):
        return jsonify({"code": 403, "message": "仅本营负责人可查看工作人员"}), 403
    rows = CampStaff.query.filter_by(camp_session_id=sid) \
        .order_by(CampStaff.status.desc(), CampStaff.role.desc(), CampStaff.id).all()
    uids = {r.user_id for r in rows} | {r.assigned_by for r in rows} \
        | {r.ended_by for r in rows if r.ended_by}
    names = {u.id: u.username for u in UserModel.query.filter(UserModel.id.in_(uids))} if uids else {}
    staff = [{
        "user_id": r.user_id, "username": names.get(r.user_id, str(r.user_id)),
        "role": r.role, "status": r.status,
        "assigned_at": r.assigned_at.isoformat() if r.assigned_at else None,
        "assigned_by_name": names.get(r.assigned_by),
        "ended_at": r.ended_at.isoformat() if r.ended_at else None,
        "ended_by_name": names.get(r.ended_by),
        "end_reason": r.end_reason,
    } for r in rows]
    events = [{
        "user_id": e.user_id, "username": names.get(e.user_id, str(e.user_id)),
        "action": e.action, "before_role": e.before_role, "after_role": e.after_role,
        "operator_name": names.get(e.operator_id),
        "reason": e.reason,
        "occurred_at": e.occurred_at.isoformat() if e.occurred_at else None,
    } for e in CampStaffEvent.query.filter_by(camp_session_id=sid)
        .order_by(CampStaffEvent.id.desc()).limit(50).all()]
    return jsonify({"code": 200, "staff": staff, "events": events})


@bp.route("/sessions/<int:sid>/staff", methods=["POST"])
@jwt_required()
@audit_log(operation="委任营期工作人员")
def staff_assign(sid):
    """委任协同老师（owner/超管）或主负责人（仅超管——首期 owner 变更收口 super_admin，方案 §5.4）。
    body: {user_id, role: 'owner'|'teacher'}。已结束的关系复职=改回 active 并记新事件；
    目标须为真实存在的普通用户（super_admin 无需委任，天然全营权限）。"""
    camp, err = _camp_or_404(sid)
    if err:
        return err
    user = _current_user()
    d = request.json or {}
    uid, role = d.get("user_id"), d.get("role", "teacher")
    if not uid or role not in VALID_STAFF_ROLES:
        return jsonify({"code": 400, "message": "缺少 user_id 或 role 非法（owner/teacher）"}), 400
    if role == 'owner' and not user.is_admin():
        return jsonify({"code": 403, "message": "主负责人委任/转交由管理员操作（首期收口）"}), 403
    if not user.is_admin() and not has_camp_access(user, sid, 'staff.manage'):
        return jsonify({"code": 403, "message": "仅主负责人可委任协同老师"}), 403
    target = UserModel.query.get(uid)
    if not target:
        return jsonify({"code": 404, "message": "用户不存在"}), 404
    if target.is_admin():
        return jsonify({"code": 400, "message": "管理员天然拥有营期权限，无需委任"}), 400
    if target.status == 'banned':
        return jsonify({"code": 400, "message": "该账号已被封禁"}), 400

    _lock_camp(sid)
    row = CampStaff.query.filter_by(camp_session_id=sid, user_id=uid).first()
    if row and row.status == 'active':
        if row.role == role:
            return jsonify({"code": 409, "message": f"该用户已是本营{'主负责人' if role == 'owner' else '协同老师'}"}), 409
        if role == 'owner':
            return jsonify({"code": 409, "message": "已有活跃的行，主负责人变更请走转交（transfer-owner）"}), 409
        return jsonify({"code": 409, "message": "角色冲突，请先解除当前职责"}), 409
    if role == 'owner' and CampStaff.query.filter_by(
            camp_session_id=sid, role='owner', status='active').first():
        return jsonify({"code": 409, "message": "本营已有主负责人，变更请走转交（transfer-owner）"}), 409

    if row:      # 复职（方案 §5.4：已结束关系不覆盖，改状态+记新事件）
        before = row.role
        row.role, row.status = role, 'active'
        row.assigned_by, row.assigned_at = user.id, datetime.now()
        row.ended_by, row.ended_at, row.end_reason = None, None, None
    else:
        row = CampStaff(camp_session_id=sid, user_id=uid, role=role,
                        assigned_by=user.id, assigned_at=datetime.now())
        db.session.add(row)
        before = None
    _staff_event(sid, uid, 'assign', before, role, user.id)
    _notify_staff_assigned(camp, uid, role, user)
    db.session.commit()
    return jsonify({"code": 200, "message": "已委任",
                    "staff_role": role, "user_id": uid})


@bp.route("/sessions/<int:sid>/staff/<int:uid>/end", methods=["POST"])
@jwt_required()
@audit_log(operation="解除营期工作人员")
def staff_end(sid, uid):
    """解除职责（状态化，不物理删除）。teacher：owner/超管可解；owner：仅超管
    （owner 解除后营回到无负责人=super_admin 兜底，属治理动作）。body: {reason?}"""
    camp, err = _camp_or_404(sid)
    if err:
        return err
    user = _current_user()
    _lock_camp(sid)
    row = CampStaff.query.filter_by(camp_session_id=sid, user_id=uid).first()
    if not row or row.status != 'active':
        return jsonify({"code": 404, "message": "该用户在本营没有生效中的职责"}), 404
    if row.role == 'owner' and not user.is_admin():
        return jsonify({"code": 403, "message": "主负责人的解除/转交由管理员操作"}), 403
    if not user.is_admin() and not has_camp_access(user, sid, 'staff.manage'):
        return jsonify({"code": 403, "message": "仅主负责人可解除协同老师"}), 403
    reason = ((request.json or {}).get("reason") or "").strip()[:500] or None
    row.status, row.ended_by, row.ended_at = 'ended', user.id, datetime.now()
    row.end_reason = reason
    _staff_event(sid, uid, 'end', row.role, None, user.id, reason)
    _notify_staff_ended(camp, uid, row.role, user, reason)
    db.session.commit()
    return jsonify({"code": 200, "message": "已解除"})


@bp.route("/sessions/<int:sid>/staff/transfer-owner", methods=["POST"])
@jwt_required()
@audit_log(operation="转交营期主负责人")
def staff_transfer_owner(sid):
    """转交主负责人（仅 super_admin，方案 §5.4 首期口径）：同一事务内结束旧 owner、
    落新 owner（目标为活跃 teacher 时升级），新旧 owner 与事件全程留痕。body: {user_id, reason?}"""
    camp, err = _camp_or_404(sid)
    if err:
        return err
    user = _current_user()
    if not user.is_admin():
        return jsonify({"code": 403, "message": "主负责人转交由管理员操作（首期收口）"}), 403
    d = request.json or {}
    uid = d.get("user_id")
    if not uid:
        return jsonify({"code": 400, "message": "缺少 user_id"}), 400
    target = UserModel.query.get(uid)
    if not target or target.is_admin() or target.status == 'banned':
        return jsonify({"code": 400, "message": "转交目标须为正常的普通用户"}), 400
    reason = (d.get("reason") or "").strip()[:500] or None

    _lock_camp(sid)
    old = CampStaff.query.filter_by(
        camp_session_id=sid, role='owner', status='active').first()
    if old and old.user_id == uid:
        return jsonify({"code": 409, "message": "该用户已是本营主负责人"}), 409
    row = CampStaff.query.filter_by(camp_session_id=sid, user_id=uid).first()
    if old:
        old.status, old.ended_by, old.ended_at = 'ended', user.id, datetime.now()
        old.end_reason = reason or "转交主负责人"
        _staff_event(sid, old.user_id, 'transfer', 'owner', None, user.id, reason)
        _notify_staff_ended(camp, old.user_id, 'owner', user, reason or "主负责人已转交")
    if row:
        before = row.role
        row.role, row.status = 'owner', 'active'
        row.assigned_by, row.assigned_at = user.id, datetime.now()
        row.ended_by, row.ended_at, row.end_reason = None, None, None
    else:
        row = CampStaff(camp_session_id=sid, user_id=uid, role='owner',
                        assigned_by=user.id, assigned_at=datetime.now())
        db.session.add(row)
        before = None
    _staff_event(sid, uid, 'promote' if before == 'teacher' else 'assign', before, 'owner',
                 user.id, reason)
    _notify_staff_assigned(camp, uid, 'owner', user)
    db.session.commit()
    return jsonify({"code": 200, "message": "主负责人已转交", "user_id": uid})


# ─────────────────────────────────────────────
# 老师工作台·概览（阶段 2 最小版：待办实时投影，方案 §10.1）
# ─────────────────────────────────────────────

@bp.route("/sessions/<int:sid>/teacher/overview")
@jwt_required()
def teacher_overview(sid):
    """老师概览：回答「现在什么阶段 / 我今天要处理什么 / 哪里有风险 / 下一个时间点」。
    待办从业务状态实时投影（不建通用 WorkItem 表——需要领取/转派/SLA 时再升级，方案 §10.2），
    只回摘要与计数，名单明细由各业务端点分页返回（§12.4：概览不内嵌两百个学员）。"""
    from models import (CampMember, CampJoinRequest, CampLeave, CampMentorProfile,
                        CampMentorPreference)
    camp, err = _camp_or_404(sid)
    if err:
        return err
    user = _current_user()
    if not user.is_admin() and not camp_staff_row(sid, user.id):
        return jsonify({"code": 403, "message": "仅本营负责人可查看老师概览"}), 403

    stage_labels = {'draft': '草稿', 'upcoming': '待开放', 'selecting': '报名与选导生',
                    'running': '进行中', 'archived': '已结营'}
    students = CampMember.query.filter_by(camp_session_id=sid, role='student').all()
    mentors = CampMember.query.filter_by(camp_session_id=sid, role='mentor').all()
    unmatched = [m for m in students if not m.team_mentor_id]

    # 待审报名（学员+导生分开计）与最早等待时间
    pending_apps = CampJoinRequest.query.filter_by(camp_session_id=sid, status='pending') \
        .order_by(CampJoinRequest.created_at).all()
    app_students = [r for r in pending_apps if (r.apply_role or 'student') == 'student']
    app_mentors = [r for r in pending_apps if r.apply_role == 'mentor']
    oldest_app = pending_apps[0].created_at.isoformat() if pending_apps else None

    # 待审请假（含未分组标记——优先处理的升级件）
    pending_leaves = CampLeave.query.filter_by(camp_session_id=sid, status='pending') \
        .order_by(CampLeave.created_at).all()
    student_team = {m.user_id: m.team_mentor_id for m in students}
    unassigned_leaves = sum(1 for lv in pending_leaves if not student_team.get(lv.user_id))

    # 选导生就绪（2026-09-21 阶段收缩）：仅 selecting 阶段投影——开营后选导生流程
    # 已结束，不再出现「未发布名片/未交志愿」等选导生阶段待办
    ms_stats = None
    if camp.mentor_selection_enabled and camp.status == 'selecting':
        profile_uids = {p.user_id for p in CampMentorProfile.query
                        .filter_by(camp_session_id=sid).all()}
        submitted = {p.student_user_id for p in CampMentorPreference.query
                     .filter_by(camp_session_id=sid).all()}
        ms_stats = {
            "mentors_without_profile": len([m for m in mentors if m.user_id not in profile_uids]),
            "students_without_preference": len([m for m in students if m.user_id not in submitted]),
            "preference_deadline": camp.ms_preference_deadline.isoformat()
            if camp.ms_preference_deadline else None,
        }

    def _item(key, count, label, extra=None):
        return {"key": key, "count": count, "label": label, **(extra or {})}

    # 待办按阶段投影（section=前端导航目标，契约见 TeacherOverview.goTodo）：
    # archived 全量只读复盘——不生成任何待处理事项；未分组学员在 selecting 是选导生
    # 收官待办（跳 ms），running 起转为运营风险项走「成员管理」处理
    archived = camp.status == 'archived'
    work_items = []
    if not archived and app_students:
        work_items.append(_item("camp.application.pending", len(app_students), "待审学员报名",
                                {"section": "admissions"}))
    if not archived and app_mentors:
        work_items.append(_item("camp.mentor_application.pending", len(app_mentors), "待审导生报名",
                                {"section": "admissions"}))
    if unmatched and camp.status == 'selecting':
        work_items.append(_item("camp.student.unmatched", len(unmatched), "未分配导生的学员",
                                {"section": "ms"}))
    elif unmatched and camp.status == 'running':
        work_items.append(_item("camp.student.ungrouped", len(unmatched), "未分组学员",
                                {"section": "members"}))
    if not archived and pending_leaves:
        work_items.append(_item("camp.leave.pending", len(pending_leaves), "待审批请假",
                                {"unassigned": unassigned_leaves, "section": "leaves"}))
    if ms_stats:
        if ms_stats["mentors_without_profile"]:
            work_items.append(_item("camp.mentor.no_profile",
                                    ms_stats["mentors_without_profile"], "未发布名片的导生",
                                    {"section": "ms"}))
        if ms_stats["students_without_preference"]:
            work_items.append(_item("camp.student.no_preference",
                                    ms_stats["students_without_preference"], "未提交志愿的学员",
                                    {"section": "ms"}))

    return jsonify({"code": 200, "overview": {
        "stage": camp.status,
        "stage_label": stage_labels.get(camp.status, camp.status),
        "start_date": camp.start_date.isoformat(),
        "end_date": camp.end_date.isoformat(),
        "counts": {"students": len(students), "mentors": len(mentors),
                   "unmatched": len(unmatched)},
        "work_items": work_items,
        "oldest_pending_application_at": oldest_app,
        "ms_stats": ms_stats,
        "my_staff_role": (camp_staff_row(sid, user.id).role
                          if not user.is_admin() and camp_staff_row(sid, user.id) else None),
    }})

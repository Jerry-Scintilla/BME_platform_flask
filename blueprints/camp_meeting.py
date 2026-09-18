"""营期·组会留档蓝图（2026-09-17，migrate_34）：培训组（导生组）与项目组通用。

组会生命周期（2026-09-18 调整为按开会的自然顺序）：
发起（标题+日期，轻量创建）→ 布置（课内章节+课外任务，/assignments）→
会后提交纪要（文字/文件/录像，经 PUT 编辑端点首次归档）。
状态不落列，由「有纪要」（content 或附件存在）派生：无纪要=进行中、有纪要=已完结；
迁移前的存量记录一律带纪要，天然全是已完结态。

组长（培训组=导生）/项目负责人发起与归档，组员可查看下载；窗口=营期未归档即可操作
（selecting 预备会 / running 例会都算，与组长活动考勤同口径），结营 _camp_writable 整体只读。

双作用域（一张表桥接两套组长模型，方案 §3.7/§3.8）：
- scope='team'：培训组。现役学习营不落 camp_unit（mentor_team 单元迁移窗口未到），
  组=CampMember(role='mentor') + 学员 team_mentor_id 指回（1 导生 1 组），
  故以组长（导生）用户为组的锚点（mentor_id）。
- scope='unit'：项目组。挂 camp_unit.id，负责人=CampUnitMember(role='leader')。

附件走 storage 层（STORAGE_BACKEND=minio|local），object_key 规则
camp/{sid}/meeting/{meeting_id}/{uuid}{ext}；下载走鉴权代理端点
/camp/meetings/attachments/<aid>，视频 inline 直播并支持 HTTP Range（206）。
"""
import os
import re
import shutil
import tempfile
import uuid
import zipfile
from datetime import datetime

from flask import Blueprint, request, jsonify, Response, send_file
from flask_jwt_extended import jwt_required
from urllib.parse import quote

from exts import db
from storage import storage
from models import (CampSession, CampMember, CampUnit, CampUnitMember,
                    CampMeeting, CampMeetingAttachment, UserModel,
                    CourseModel, Chapter, CampChapterCertification,
                    CampMeetingChapterPlan, CampMeetingTask,
                    CampMeetingTaskSubmission, CampMeetingTaskAttachment)

from . import audit_log, _current_user
from .camp import _camp_writable, _in_my_team, _direction_of_mentor
from .camp_project import _camp_or_404, _unit_or_404, _is_unit_leader, _usernames
from .camp_delivery import _unit_role
from .media_sign import media_token_response, media_signed_url, resolve_media_request
from .notification import create_notification

bp = Blueprint("camp_meeting", __name__, url_prefix="/camp")

MAX_FILE_MB = 100     # 普通文件单件上限（与章节材料/交付链同口径）
MAX_VIDEO_MB = 500    # 视频单件上限（组会录像）
MAX_TOTAL_MB = 1024   # 单次请求整包上限（多文件合计粗检，multipart 开销有余量）


# ─────────────────────────────────────────────
# 辅助
# ─────────────────────────────────────────────

def _team_context(sid, user):
    """解析请求者在学习营的培训组上下文（以组长为锚）。
    返回 (mentor, member_count) ；未编组/非营内成员返回 (None, 0)。"""
    member = CampMember.query.filter_by(camp_session_id=sid, user_id=user.id).first()
    mentor_id = None
    if member and member.role == 'mentor':
        mentor_id = user.id
    elif member and member.role == 'student' and member.team_mentor_id:
        mentor_id = member.team_mentor_id
    if not mentor_id:
        return None, 0
    mentor = UserModel.query.get(mentor_id)
    if not mentor:
        return None, 0
    count = CampMember.query.filter_by(
        camp_session_id=sid, role='student', team_mentor_id=mentor_id).count()
    return mentor, count


def _can_view(user, m):
    """组会可见性：本组成员（team=组长及其学员；unit=active 单元成员）或 admin。"""
    if user.is_admin():
        return True
    if m.scope == 'team':
        if m.mentor_id == user.id:
            return True
        mentor = UserModel.query.get(m.mentor_id)
        return bool(mentor and _in_my_team(m.camp_session_id, mentor, user.id))
    unit = CampUnit.query.get(m.unit_id)
    return bool(unit and _unit_role(unit, user))


def _can_manage(user, m):
    """组会管理权（编辑/删除）：创建人、现任组长（team=该导生；unit=active leader）或 admin。"""
    if user.is_admin() or m.created_by == user.id:
        return True
    if m.scope == 'team':
        return m.mentor_id == user.id
    unit = CampUnit.query.get(m.unit_id)
    return bool(unit and _is_unit_leader(unit, user))


def _atts_by_meeting(meeting_ids):
    out = {mid: [] for mid in meeting_ids}
    if not meeting_ids:
        return out
    for a in CampMeetingAttachment.query.filter(
            CampMeetingAttachment.meeting_id.in_(meeting_ids)).all():
        out[a.meeting_id].append(a)
    return out


def _meeting_dict(m, atts, names):
    return {
        "id": m.id, "scope": m.scope, "unit_id": m.unit_id, "mentor_id": m.mentor_id,
        "title": m.title, "meeting_date": m.meeting_date.isoformat(),
        "content": m.content,
        "created_by": m.created_by, "creator_name": names.get(m.created_by, str(m.created_by)),
        "created_at": m.created_at.isoformat() if m.created_at else None,
        "updated_at": m.updated_at.isoformat() if m.updated_at else None,
        "attachments": [{"id": a.id, "filename": a.filename, "size": a.size,
                         "content_type": a.content_type,
                         "is_video": (a.content_type or '').startswith('video/')}
                        for a in atts],
    }


def _meetings_payload(sid, user, student_ids, leader_view, **filters):
    """按作用域过滤取组会列表并序列化（新会日期在前，同日新纪录在前）。
    user/student_ids/leader_view 供列表卡片的布置摘要计数（导生=提交 x/期望；组员=我的待提交 n）。"""
    rows = (CampMeeting.query.filter_by(camp_session_id=sid, **filters)
            .order_by(CampMeeting.meeting_date.desc(), CampMeeting.id.desc()).all())
    atts = _atts_by_meeting([m.id for m in rows])
    creator_ids = {m.created_by for m in rows}
    names = _usernames(creator_ids)
    summaries = _meeting_summaries([m.id for m in rows], user, student_ids, leader_view)
    return [{**_meeting_dict(m, atts[m.id], names), **summaries.get(m.id, {})}
            for m in rows]


def _meeting_summaries(meeting_ids, user, student_ids, leader_view):
    """列表摘要：任务数/课内章数 + 提交计数（按 submit_type 判有效提交）。
    leader_view=True → 提交 x/期望（任务 × 组员）；否则 → 我的待提交 n。"""
    out = {}
    if not meeting_ids:
        return out
    tasks = (CampMeetingTask.query
             .filter(CampMeetingTask.meeting_id.in_(meeting_ids)).all())
    plans = dict(db.session.query(CampMeetingChapterPlan.meeting_id,
                                  db.func.count(CampMeetingChapterPlan.id))
                 .filter(CampMeetingChapterPlan.meeting_id.in_(meeting_ids))
                 .group_by(CampMeetingChapterPlan.meeting_id).all())
    tasks_by_meeting = {}
    for t in tasks:
        tasks_by_meeting.setdefault(t.meeting_id, []).append(t)
    subs = (CampMeetingTaskSubmission.query
            .filter(CampMeetingTaskSubmission.task_id.in_([t.id for t in tasks])).all()
            if tasks else [])
    att_counts = (_att_counts_of([s.id for s in subs]) if subs else {})
    task_by_id = {t.id: t for t in tasks}
    # 有效提交 (meeting_id, task_id, uid) 三元组
    valid_pairs = set()
    for s in subs:
        t = task_by_id[s.task_id]
        if _submission_valid(s, t, att_counts.get(s.id, 0)):
            valid_pairs.add((t.meeting_id, t.id, s.student_user_id))
    for mid in meeting_ids:
        m_tasks = tasks_by_meeting.get(mid, [])
        done_by_user = {}
        for (m_id, t_id, uid) in valid_pairs:
            if m_id == mid:
                done_by_user.setdefault(uid, set()).add(t_id)
        summary = {"task_count": len(m_tasks), "chapter_count": int(plans.get(mid, 0))}
        if leader_view:
            summary["expected_count"] = len(m_tasks) * len(student_ids or [])
            summary["submission_count"] = sum(len(v) for v in done_by_user.values())
        else:
            mine = done_by_user.get(user.id, set())
            summary["my_pending"] = len([t for t in m_tasks if t.id not in mine])
        out[mid] = summary
    return out


def _att_counts_of(submission_ids):
    if not submission_ids:
        return {}
    return dict(db.session.query(CampMeetingTaskAttachment.submission_id,
                                 db.func.count(CampMeetingTaskAttachment.id))
                .filter(CampMeetingTaskAttachment.submission_id.in_(submission_ids))
                .group_by(CampMeetingTaskAttachment.submission_id).all())


def _submission_valid(sub, task, att_count):
    """提交是否满足任务的 submit_type（file=有附件 / text=有文字 / any=任一）。"""
    has_text = bool((sub.content or '').strip())
    has_file = att_count > 0
    if task.submit_type == 'file':
        return has_file
    if task.submit_type == 'text':
        return has_text
    return has_text or has_file


def _parse_meeting_form(existing=None):
    """解析/校验 multipart 表单（创建与编辑共用）。
    返回 (fields, files, err_resp)；fields=None 表示非法。existing 供编辑时兜底旧值。"""
    title = (request.form.get("title") or "").strip()
    date_raw = (request.form.get("meeting_date") or "").strip()
    content_raw = request.form.get("content")
    content = content_raw.strip() or None if content_raw is not None else None
    if existing is not None:                       # 编辑：未提供的字段保持旧值
        title = title or existing.title
        date_raw = date_raw or existing.meeting_date.isoformat()
        if content_raw is None:
            content = existing.content
    if not title:
        return None, None, (jsonify({"code": 400, "message": "请填写会议主题"}), 400)
    if len(title) > 200:
        return None, None, (jsonify({"code": 400, "message": "会议主题过长（≤200 字）"}), 400)
    try:
        meeting_date = datetime.strptime(date_raw, "%Y-%m-%d").date()
    except ValueError:
        return None, None, (jsonify({"code": 400, "message": "会议日期格式须为 YYYY-MM-DD"}), 400)
    files = [f for f in request.files.getlist("Files") if f.filename]
    # 整包粗检（multipart 开销有余量；单件上限上传后按类型细查）
    cl = request.content_length
    if cl and cl > MAX_TOTAL_MB * 1024 * 1024:
        return None, None, (jsonify(
            {"code": 413, "message": f"单次提交总量超过 {MAX_TOTAL_MB}MB 上限"}), 413)
    return {"title": title, "meeting_date": meeting_date, "content": content}, files, None


def _save_attachments(files, meeting, camp_id):
    """逐文件上传 storage 并落附件行（不 commit；失败即回滚返回 err，与章节材料同款）。
    视频/普通文件分档限大小；超限对象即时清理（改进既有链「传完才拒、对象滞留」）。"""
    for f in files:
        ext = os.path.splitext(f.filename)[1].lower()[:20]
        key = f"camp/{camp_id}/meeting/{meeting.id}/{uuid.uuid4().hex}{ext}"
        mime = (f.mimetype or 'application/octet-stream')[:100]
        limit_mb = MAX_VIDEO_MB if mime.startswith('video/') else MAX_FILE_MB
        try:
            storage.put_object(key, f.stream, content_type=mime)
            size = storage.stat_object(key).size
        except Exception:
            db.session.rollback()
            return (jsonify({"code": 500, "message": "附件上传失败（存储服务不可用？），请稍后重试"}), 500)
        if size > limit_mb * 1024 * 1024:
            storage.remove_object(key)
            db.session.rollback()
            return (jsonify({"code": 400,
                             "message": f"{f.filename} 超过 {limit_mb}MB 上限"}), 400)
        db.session.add(CampMeetingAttachment(
            meeting_id=meeting.id, object_key=key, filename=f.filename[:200],
            size=size, content_type=mime))
    return None


def _scope_write_denied(m, camp):
    """组会写操作的前置冻结检查：营只读或（unit 域）单元非 active。"""
    if not _camp_writable(camp):
        return jsonify({"code": 400, "message": "CAMP_ARCHIVED_READ_ONLY 营期已归档，只读"}), 400
    if m.scope == 'unit':
        unit = CampUnit.query.get(m.unit_id)
        if unit and unit.status != 'active':
            return jsonify({"code": 400, "message": "项目已停用/终止，组会记录只读"}), 400
    return None


def _notify_new_meeting(user_ids, camp_id, meeting, by_name, role_label):
    """发起组会通知组员（调用方剔除创建人；失败不阻断主流程）。
    source_type='camp_meeting' → 通知中心点击深链 /camp?tab=meetings&sid=
    （项目营无营期层 tab，CampView 自动回落 ProjectHub，同链两用）。"""
    for uid in user_ids:
        try:
            create_notification(
                uid, f"新组会：{meeting.title}",
                f"{role_label} {by_name} 发起了组会「{meeting.title}」"
                f"（{meeting.meeting_date.isoformat()}），布置发布后会再次通知。",
                category='camp', source_type='camp_meeting', source_id=meeting.id,
                camp_session_id=camp_id)
        except Exception:   # 通知失败不阻断主流程
            pass


def _notify_minutes_submitted(user_ids, camp_id, meeting, by_name, role_label):
    """纪要归档通知组员（进行中→已完结的首次提交时一次性通知，失败不阻断主流程）。"""
    for uid in user_ids:
        try:
            create_notification(
                uid, f"组会纪要已归档：{meeting.title}",
                f"{role_label} {by_name} 提交了「{meeting.title}」"
                f"（{meeting.meeting_date.isoformat()}）的组会纪要，点击查看。",
                category='camp', source_type='camp_meeting', source_id=meeting.id,
                camp_session_id=camp_id)
        except Exception:
            pass


# ─────────────────────────────────────────────
# 培训组（导生组）域
# ─────────────────────────────────────────────

@bp.route("/sessions/<int:sid>/team-meetings")
@jwt_required()
def team_meeting_list(sid):
    """我的培训组组会列表。请求者是导生→本人组；是学员→其归属导生组；
    未编组/非营内成员返回 group=None（前端空态分流），不报错。"""
    camp, err = _camp_or_404(sid)
    if err:
        return err
    user = _current_user()
    mentor, count = _team_context(sid, user)
    if not mentor:
        return jsonify({"code": 200, "group": None, "is_leader": False, "meetings": []})
    is_leader = mentor.id == user.id or user.is_admin()
    student_ids = [r.user_id for r in CampMember.query.filter_by(
        camp_session_id=sid, role='student', team_mentor_id=mentor.id).all()]
    return jsonify({
        "code": 200,
        "group": {"scope": "team", "mentor_id": mentor.id, "mentor_name": mentor.username,
                  "member_count": count},
        "is_leader": is_leader,
        "meetings": _meetings_payload(sid, user, student_ids, is_leader,
                                      scope='team', mentor_id=mentor.id),
    })


@bp.route("/sessions/<int:sid>/team-meetings", methods=["POST"])
@jwt_required()
@audit_log(operation="发起组会")
def team_meeting_create(sid):
    """组长（本营导生）发起培训组组会（multipart：title/meeting_date 必填；
    content/Files 选填——补录已开完的会时可随创建一并归档纪要，正常流程会后经
    「提交纪要」（PUT 编辑端点）补交，未归档前组会保持进行中态）。"""
    camp, err = _camp_or_404(sid)
    if err:
        return err
    if camp.category != 'learning':
        return jsonify({"code": 400, "message": "组会纪要（培训组）仅学习营可用"}), 400
    user = _current_user()
    member = CampMember.query.filter_by(camp_session_id=sid, user_id=user.id).first()
    if not member or member.role != 'mentor':
        return jsonify({"code": 403, "message": "仅组长（导生）可发起组会"}), 403
    if not _camp_writable(camp):
        return jsonify({"code": 400, "message": "CAMP_ARCHIVED_READ_ONLY 营期已归档，只读"}), 400
    fields, files, err = _parse_meeting_form()
    if err:
        return err
    m = CampMeeting(camp_session_id=sid, scope='team', mentor_id=user.id,
                    created_by=user.id, **fields)
    db.session.add(m)
    db.session.flush()
    err = _save_attachments(files, m, sid)
    if err:
        return err
    db.session.commit()
    _notify_new_meeting(
        [r.user_id for r in CampMember.query.filter_by(
            camp_session_id=sid, role='student', team_mentor_id=m.mentor_id).all()
         if r.user_id != user.id],
        sid, m, user.username, "组长")
    names = _usernames({m.created_by})
    return jsonify({"code": 200, "message": "组会已创建",
                    "meeting": _meeting_dict(m, CampMeetingAttachment.query.filter_by(
                        meeting_id=m.id).all(), names)})


# ─────────────────────────────────────────────
# 项目组域
# ─────────────────────────────────────────────

@bp.route("/units/<int:uid>/meetings")
@jwt_required()
def unit_meeting_list(uid):
    """项目组组会列表（本组 active 成员/负责人/admin 可见）。"""
    unit, camp, err = _unit_or_404(uid)
    if err:
        return err
    user = _current_user()
    if not _unit_role(unit, user):
        return jsonify({"code": 403, "message": "仅项目成员可查看组会纪要"}), 403
    is_leader = _is_unit_leader(unit, user)
    student_ids = [r.user_id for r in CampUnitMember.query.filter_by(
        unit_id=unit.id, status='active').all() if r.role != 'leader']
    return jsonify({
        "code": 200,
        "group": {"scope": "unit", "unit_id": unit.id, "unit_name": unit.name,
                  "member_count": CampUnitMember.query.filter_by(
                      unit_id=unit.id, status='active').count()},
        "is_leader": is_leader,
        "meetings": _meetings_payload(camp.id, user, student_ids, is_leader,
                                      scope='unit', unit_id=unit.id),
    })


@bp.route("/units/<int:uid>/meetings", methods=["POST"])
@jwt_required()
@audit_log(operation="发起项目组会")
def unit_meeting_create(uid):
    """项目负责人发起项目组组会（表单同培训组链：title/meeting_date 必填，
    纪要会后经「提交纪要」补归档）。"""
    unit, camp, err = _unit_or_404(uid)
    if err:
        return err
    user = _current_user()
    if not _is_unit_leader(unit, user):
        return jsonify({"code": 403, "message": "仅项目负责人可发起组会"}), 403
    if not _camp_writable(camp):
        return jsonify({"code": 400, "message": "CAMP_ARCHIVED_READ_ONLY 营期已归档，只读"}), 400
    if unit.status != 'active':
        return jsonify({"code": 400, "message": "项目已停用/终止，组会记录只读"}), 400
    fields, files, err = _parse_meeting_form()
    if err:
        return err
    m = CampMeeting(camp_session_id=camp.id, scope='unit', unit_id=unit.id,
                    created_by=user.id, **fields)
    db.session.add(m)
    db.session.flush()
    err = _save_attachments(files, m, camp.id)
    if err:
        return err
    db.session.commit()
    _notify_new_meeting(
        [r.user_id for r in CampUnitMember.query.filter_by(
            unit_id=unit.id, status='active').all()
         if r.user_id != user.id],
        camp.id, m, user.username, "项目负责人")
    names = _usernames({m.created_by})
    return jsonify({"code": 200, "message": "组会已创建",
                    "meeting": _meeting_dict(m, CampMeetingAttachment.query.filter_by(
                        meeting_id=m.id).all(), names)})


# ─────────────────────────────────────────────
# 组会通用（编辑/删除）
# ─────────────────────────────────────────────

def _meeting_or_404(mid):
    m = CampMeeting.query.get(mid)
    if not m:
        return None, (jsonify({"code": 404, "message": "组会纪要不存在"}), 404)
    return m, None


@bp.route("/meetings/<int:mid>", methods=["PUT"])
@jwt_required()
@audit_log(operation="提交组会纪要")
def meeting_update(mid):
    """提交/编辑组会纪要（创建人/现任组长/admin）：multipart，未提供的文字字段保持旧值，
    Files[] 为追加附件（历史附件不覆盖；删单个附件走附件端点）。
    会后首次归档纪要（原本无文字且无附件）时通知组员，之后的安静编辑不再打扰。"""
    m, err = _meeting_or_404(mid)
    if err:
        return err
    camp = CampSession.query.get(m.camp_session_id)
    user = _current_user()
    if not _can_manage(user, m):
        return jsonify({"code": 403, "message": "仅创建人或组长可提交组会纪要"}), 403
    denied = _scope_write_denied(m, camp)
    if denied:
        return denied
    fields, files, err = _parse_meeting_form(existing=m)
    if err:
        return err
    keep_atts = CampMeetingAttachment.query.filter_by(meeting_id=m.id).count()
    if not fields["content"] and not files and not keep_atts:
        return jsonify({"code": 400, "message": "请填写纪要内容或上传附件"}), 400
    had_minutes = bool(m.content) or keep_atts > 0    # 归档前态（进行中）
    m.title, m.meeting_date, m.content = (fields["title"], fields["meeting_date"],
                                          fields["content"])
    db.session.flush()
    err = _save_attachments(files, m, m.camp_session_id)
    if err:
        return err
    db.session.commit()
    if not had_minutes:                              # 进行中 → 已完结，一次性通知
        _notify_minutes_submitted(
            [u.id for u in _group_students(m) if u.id != user.id],
            m.camp_session_id, m, user.username,
            "组长" if m.scope == 'team' else "项目负责人")
    names = _usernames({m.created_by})
    return jsonify({"code": 200, "message": "组会纪要已更新",
                    "meeting": _meeting_dict(m, CampMeetingAttachment.query.filter_by(
                        meeting_id=m.id).all(), names)})


@bp.route("/meetings/<int:mid>", methods=["DELETE"])
@jwt_required()
@audit_log(operation="删除组会纪要")
def meeting_delete(mid):
    """删组会（创建人/现任组长/admin）：附件对象幂等清理 + 行删除（附件行随 FK CASCADE）。"""
    m, err = _meeting_or_404(mid)
    if err:
        return err
    camp = CampSession.query.get(m.camp_session_id)
    user = _current_user()
    if not _can_manage(user, m):
        return jsonify({"code": 403, "message": "仅创建人或组长可删除组会纪要"}), 403
    denied = _scope_write_denied(m, camp)
    if denied:
        return denied
    for a in CampMeetingAttachment.query.filter_by(meeting_id=m.id).all():
        storage.remove_object(a.object_key)    # 幂等，对象缺失静默
    db.session.delete(m)
    db.session.commit()
    return jsonify({"code": 200, "message": "已删除"})


@bp.route("/meetings/attachments/<int:aid>", methods=["DELETE"])
@jwt_required()
@audit_log(operation="删除组会附件")
def meeting_attachment_delete(aid):
    """删单个附件（创建人/现任组长/admin）：对象幂等清理 + 行删除。"""
    a = CampMeetingAttachment.query.get(aid)
    if not a:
        return jsonify({"code": 404, "message": "附件不存在"}), 404
    m, err = _meeting_or_404(a.meeting_id)
    if err:
        return err
    camp = CampSession.query.get(m.camp_session_id)
    user = _current_user()
    if not _can_manage(user, m):
        return jsonify({"code": 403, "message": "仅创建人或组长可删除附件"}), 403
    denied = _scope_write_denied(m, camp)
    if denied:
        return denied
    if not m.content and CampMeetingAttachment.query.filter(
            CampMeetingAttachment.meeting_id == m.id,
            CampMeetingAttachment.id != a.id).count() == 0:
        return jsonify({"code": 400, "message": "纪要仅剩该附件，删除前请先填写文字内容"}), 400
    storage.remove_object(a.object_key)
    db.session.delete(a)
    db.session.commit()
    return jsonify({"code": 200, "message": "已删除"})


# ─────────────────────────────────────────────
# 布置与任务（2026-09-17 教学单元：一次组会 = 纪要 + 课内章节布置 + 课外任务 + 审阅打包）
# ─────────────────────────────────────────────

VALID_SUBMIT_TYPES = ('file', 'text', 'any')
SUBMIT_TYPE_TEXT = {'file': '需交文件', 'text': '需交文字', 'any': '文字或文件'}


def _group_students(m):
    """组会作用域的组员名单（不含组长/负责人）：team=该导生组学员；unit=active 成员减 leader。"""
    if m.scope == 'team':
        ids = [r.user_id for r in CampMember.query.filter_by(
            camp_session_id=m.camp_session_id, role='student',
            team_mentor_id=m.mentor_id).all()]
    else:
        ids = [r.user_id for r in CampUnitMember.query.filter_by(
            unit_id=m.unit_id, status='active').all() if r.role != 'leader']
    users = UserModel.query.filter(UserModel.id.in_(ids)).all() if ids else []
    return sorted(users, key=lambda u: u.id)     # 稳定排序，矩阵列序一致


def _leader_uid(m):
    """组会现任组长（提交通知接收人）：team=导生；unit=active leader 行（兜底 owner）。"""
    if m.scope == 'team':
        return m.mentor_id
    row = CampUnitMember.query.filter_by(
        unit_id=m.unit_id, role='leader', status='active').first()
    if row:
        return row.user_id
    unit = CampUnit.query.get(m.unit_id)
    return unit.owner_user_id if unit else None


def _notify_meeting_users(m, user_ids, title, content):
    for uid in user_ids:
        try:
            create_notification(uid, title, content, category='camp',
                                source_type='camp_meeting', source_id=m.id,
                                camp_session_id=m.camp_session_id)
        except Exception:   # 通知失败不阻断主流程
            pass


def _chapters_of(course_id):
    """课程的学习单元章（与 camp._chapters_payload 同级次过滤规则：存在 L>=2 时 L1 纯标题不下发）。"""
    chs = (Chapter.query.filter_by(course_id=course_id)
           .order_by(Chapter.order, Chapter.id).all())
    if any(ch.level and ch.level >= 2 for ch in chs):
        chs = [ch for ch in chs if not (ch.level and ch.level < 2)]
    return chs


def _chapter_catalog(camp, m):
    """布置区的章节选择目录（仅 team 域）：导生方向课程 × 学习单元章；
    admin 无方向归属 → 营全部分类方向课程并集。"""
    course_ids = []
    if m.mentor_id:
        direction = _direction_of_mentor(camp, m.mentor_id)
        course_ids = (direction or {}).get("course_ids") or []
    if not course_ids:
        from .camp_ms import _ms_directions
        seen = set()
        for d in _ms_directions(camp):
            for cid in d.get("course_ids", []):
                if cid not in seen:
                    seen.add(cid)
                    course_ids.append(cid)
    out = []
    for cid in course_ids:
        c = CourseModel.query.get(cid)
        if not c:
            continue
        out.append({"course_id": cid, "course_title": c.title,
                    "chapters": [{"id": ch.id, "name": ch.name} for ch in _chapters_of(cid)]})
    return out


def _task_att_dict(a, viewer):
    """任务附件序列化：内嵌短签直链（打开详情即换签，2h 有效；权限在下载端点复查）。"""
    return {"id": a.id, "filename": a.filename, "size": a.size,
            "content_type": a.content_type,
            "url": media_signed_url('meeting_task', a.id, viewer.id)}


def _detail_payload(m, user, is_leader):
    """组会详情（视角分流）：导生=审阅矩阵（任务提交明细 + 章节认证矩阵 + 章节目录）；
    组员=我的任务与提交 + 我的章节认证态。"""
    camp = CampSession.query.get(m.camp_session_id)
    students = _group_students(m)
    tasks = (CampMeetingTask.query.filter_by(meeting_id=m.id)
             .order_by(CampMeetingTask.id).all())
    plans = (CampMeetingChapterPlan.query.filter_by(meeting_id=m.id)
             .order_by(CampMeetingChapterPlan.id).all())
    subs = (CampMeetingTaskSubmission.query
            .filter(CampMeetingTaskSubmission.task_id.in_([t.id for t in tasks])).all()
            if tasks else [])
    atts = {}
    for a in (CampMeetingTaskAttachment.query.filter(
            CampMeetingTaskAttachment.submission_id.in_(
                [s.id for s in subs])).all() if subs else []):
        atts.setdefault(a.submission_id, []).append(a)
    sub_by_key = {(s.task_id, s.student_user_id): s for s in subs}

    tasks_out = []
    for t in tasks:
        item = {"id": t.id, "title": t.title, "note": t.note,
                "submit_type": t.submit_type,
                "submit_type_text": SUBMIT_TYPE_TEXT.get(t.submit_type, t.submit_type)}
        if is_leader:
            item["submissions"] = {}
            item["submission_count"] = 0
            for u in students:
                s = sub_by_key.get((t.id, u.id))
                if not s:
                    continue
                valid = _submission_valid(s, t, len(atts.get(s.id, [])))
                item["submissions"][str(u.id)] = {
                    "valid": valid, "content": s.content,
                    "updated_at": s.updated_at.isoformat() if s.updated_at else None,
                    "attachments": [_task_att_dict(a, user) for a in atts.get(s.id, [])]}
                if valid:
                    item["submission_count"] += 1
        else:
            s = sub_by_key.get((t.id, user.id))
            item["my_submission"] = (None if s is None else {
                "valid": _submission_valid(s, t, len(atts.get(s.id, []))),
                "content": s.content,
                "updated_at": s.updated_at.isoformat() if s.updated_at else None,
                "attachments": [_task_att_dict(a, user) for a in atts.get(s.id, [])]})
        tasks_out.append(item)

    chapters_out = []
    if plans:
        ch_names = {c.id: c.name for c in Chapter.query.filter(
            Chapter.id.in_([p.chapter_id for p in plans])).all()}
        course_titles = {c.id: c.title for c in CourseModel.query.filter(
            CourseModel.id.in_({p.course_id for p in plans})).all()}
        cert_rows = CampChapterCertification.query.filter(
            CampChapterCertification.camp_session_id == m.camp_session_id,
            CampChapterCertification.chapter_id.in_(
                [p.chapter_id for p in plans])).all()
        certs = {(r.student_user_id, r.chapter_id): r for r in cert_rows}
        for p in plans:
            ch = {"chapter_id": p.chapter_id, "course_id": p.course_id,
                  "chapter_title": ch_names.get(p.chapter_id, str(p.chapter_id)),
                  "course_title": course_titles.get(p.course_id, "")}
            if is_leader:
                ch["certs"] = {str(u.id): (None if (u.id, p.chapter_id) not in certs
                                           else {"score": certs[(u.id, p.chapter_id)].score})
                               for u in students}
                ch["certified_count"] = sum(1 for v in ch["certs"].values() if v)
            else:
                r = certs.get((user.id, p.chapter_id))
                ch["my_cert"] = None if r is None else {"score": r.score}
            chapters_out.append(ch)

    out = {"code": 200, "is_leader": is_leader,
           "meeting": _meeting_dict(m, CampMeetingAttachment.query.filter_by(
               meeting_id=m.id).all(), _usernames({m.created_by})),
           "students": ([{"user_id": u.id, "username": u.username} for u in students]
                        if is_leader else []),
           "tasks": tasks_out, "chapters": chapters_out}
    if is_leader and m.scope == 'team':
        out["chapter_catalog"] = _chapter_catalog(camp, m)
    return out


@bp.route("/meetings/<int:mid>/detail")
@jwt_required()
def meeting_detail(mid):
    """组会详情（视角分流：导生=审阅矩阵；组员=我的任务与认证态）。"""
    m, err = _meeting_or_404(mid)
    if err:
        return err
    user = _current_user()
    if not _can_view(user, m):
        return jsonify({"code": 403, "message": "仅本组成员可查看组会"}), 403
    return jsonify(_detail_payload(m, user, _can_manage(user, m)))


@bp.route("/sessions/<int:sid>/team/task-summary")
@jwt_required()
def team_task_summary(sid):
    """导生「学员进度」页的作业维度（09-17）：本团队每学员的组会任务提交汇总
    （已交数 / 任务总数），与章节认证并显——学习全貌一页看齐。admin 无归属组返回空。"""
    camp, err = _camp_or_404(sid)
    if err:
        return err
    user = _current_user()
    member = CampMember.query.filter_by(camp_session_id=sid, user_id=user.id).first()
    if not user.is_admin() and (not member or member.role != 'mentor'):
        return jsonify({"code": 403, "message": "仅本营导生可查看任务汇总"}), 403
    mentor_id = None if user.is_admin() else user.id
    meeting_ids = ([m.id for m in CampMeeting.query.filter_by(
        camp_session_id=sid, scope='team', mentor_id=mentor_id).all()]
        if mentor_id else [])
    student_ids = ([r.user_id for r in CampMember.query.filter_by(
        camp_session_id=sid, role='student', team_mentor_id=mentor_id).all()]
        if mentor_id else [])
    tasks = (CampMeetingTask.query.filter(
        CampMeetingTask.meeting_id.in_(meeting_ids)).all() if meeting_ids else [])
    subs = (CampMeetingTaskSubmission.query
            .filter(CampMeetingTaskSubmission.task_id.in_([t.id for t in tasks])).all()
            if tasks else [])
    att_counts = _att_counts_of([s.id for s in subs])
    task_by_id = {t.id: t for t in tasks}
    done = set()   # (task_id, student_id) 有效提交对
    for s in subs:
        t = task_by_id[s.task_id]
        if s.student_user_id in student_ids and _submission_valid(s, t, att_counts.get(s.id, 0)):
            done.add((t.id, s.student_user_id))
    return jsonify({"code": 200, "meeting_count": len(meeting_ids), "task_total": len(tasks),
                    "summary": [{"user_id": uid, "submitted": sum(
                        1 for (tid, su) in done if su == uid)} for uid in student_ids]})


@bp.route("/meetings/<int:mid>/assignments", methods=["PUT"])
@jwt_required()
@audit_log(operation="保存组会布置")
def meeting_assignments_save(mid):
    """保存组会布置（现任组长/admin）。body: {chapters: [chapter_id], tasks: [{id?, title, note?, submit_type?}]}
    —— chapters 整组替换（仅 team 域，项目营无按章认证）；tasks 带 id=更新、无 id=新增、
    缺席=删除（已有学生提交的任务拒删，防数据丢失）。"""
    m, err = _meeting_or_404(mid)
    if err:
        return err
    camp = CampSession.query.get(m.camp_session_id)
    user = _current_user()
    if not _can_manage(user, m):
        return jsonify({"code": 403, "message": "仅组长可布置任务"}), 403
    denied = _scope_write_denied(m, camp)
    if denied:
        return denied
    d = request.json or {}

    # ── 课内章节（整组替换，team 域）──
    chapter_ids, seen = [], set()
    for raw in (d.get("chapters") or []):
        try:
            cid = int(raw)
        except (TypeError, ValueError):
            return jsonify({"code": 400, "message": "chapters 须为章节 id 数组"}), 400
        if cid not in seen:
            seen.add(cid)
            chapter_ids.append(cid)
    ch_course = {}
    if chapter_ids:
        for c in Chapter.query.filter(Chapter.id.in_(chapter_ids)).all():
            ch_course[c.id] = c.course_id
        if len(ch_course) != len(chapter_ids):
            return jsonify({"code": 404, "message": "布置章节不存在"}), 404
    if m.scope != 'team':
        chapter_ids = []          # 项目营忽略章节布置

    # ── 课外任务（upsert + 受保护删除）──
    raw_tasks = d.get("tasks") or []
    if len(raw_tasks) > 20:
        return jsonify({"code": 400, "message": "单次组会任务过多（≤20）"}), 400
    norm = []
    for t in raw_tasks:
        title = (t.get("title") or "").strip()
        if not title:
            return jsonify({"code": 400, "message": "请填写任务标题"}), 400
        st = t.get("submit_type") or 'any'
        if st not in VALID_SUBMIT_TYPES:
            return jsonify({"code": 400, "message": "submit_type 须为 file/text/any"}), 400
        note = (t.get("note") or "").strip()[:500] or None
        tid = t.get("id")
        try:
            tid = int(tid) if tid else None
        except (TypeError, ValueError):
            return jsonify({"code": 400, "message": "任务 id 非法"}), 400
        norm.append((tid, title[:200], note, st))

    existing = {t.id: t for t in CampMeetingTask.query.filter_by(meeting_id=mid).all()}
    keep_ids = {tid for tid, *_ in norm if tid}
    for tid, t in existing.items():
        if tid in keep_ids:
            continue
        if CampMeetingTaskSubmission.query.filter_by(task_id=tid).count():
            return jsonify({"code": 400,
                            "message": f"任务「{t.title}」已有学生提交，不能删除（可清空标题旁的说明代替）"}), 400
        db.session.delete(t)
    changed = False
    for tid, title, note, st in norm:
        if tid and tid in existing:
            t = existing[tid]
            if (t.title, t.note, t.submit_type) != (title, note, st):
                t.title, t.note, t.submit_type = title, note, st
                changed = True
        else:
            db.session.add(CampMeetingTask(meeting_id=mid, title=title, note=note,
                                           submit_type=st, created_by=user.id))
            changed = True

    old_plan_ids = {p.chapter_id for p in
                    CampMeetingChapterPlan.query.filter_by(meeting_id=mid).all()}
    if old_plan_ids != set(chapter_ids):
        changed = True
    CampMeetingChapterPlan.query.filter_by(meeting_id=mid).delete(synchronize_session=False)
    for cid in chapter_ids:
        db.session.add(CampMeetingChapterPlan(meeting_id=mid,
                                              course_id=ch_course[cid], chapter_id=cid))
    db.session.commit()

    if changed and (chapter_ids or norm):
        _notify_meeting_users(
            m, [u.id for u in _group_students(m)],
            f"组会布置更新：{m.title}",
            f"组会「{m.title}」（{m.meeting_date.isoformat()}）更新了任务与课内布置，点击查看。")
    return jsonify(_detail_payload(m, user, True))


@bp.route("/meetings/tasks/<int:tid>/submission", methods=["POST"])
@jwt_required()
@audit_log(operation="提交组会任务")
def meeting_task_submit(tid):
    """组员提交任务（multipart：content 选填 + Files[] 多文件；每人一条 upsert，
    未提供的字段保持旧值）。最终态须满足任务 submit_type，否则回滚 400。"""
    t = CampMeetingTask.query.get(tid)
    if not t:
        return jsonify({"code": 404, "message": "任务不存在"}), 404
    m, err = _meeting_or_404(t.meeting_id)
    if err:
        return err
    camp = CampSession.query.get(m.camp_session_id)
    user = _current_user()
    if not _can_view(user, m):
        return jsonify({"code": 403, "message": "仅本组组员可提交任务"}), 403
    if _can_manage(user, m):
        return jsonify({"code": 403, "message": "布置人无需提交任务"}), 403
    if not _camp_writable(camp):
        return jsonify({"code": 400, "message": "CAMP_ARCHIVED_READ_ONLY 营期已归档，只读"}), 400

    sub = CampMeetingTaskSubmission.query.filter_by(
        task_id=t.id, student_user_id=user.id).first()
    content_raw = request.form.get("content")
    content = (content_raw.strip() or None) if content_raw is not None else (sub.content if sub else None)
    files = [f for f in request.files.getlist("Files") if f.filename]
    if not sub:
        sub = CampMeetingTaskSubmission(task_id=t.id, student_user_id=user.id, content=content)
        db.session.add(sub)
        db.session.flush()
    else:
        sub.content = content
    for f in files:
        ext = os.path.splitext(f.filename)[1].lower()[:20]
        key = f"camp/{m.camp_session_id}/meeting/{m.id}/task/{t.id}/{uuid.uuid4().hex}{ext}"
        mime = (f.mimetype or 'application/octet-stream')[:100]
        try:
            storage.put_object(key, f.stream, content_type=mime)
            size = storage.stat_object(key).size
        except Exception:
            db.session.rollback()
            return jsonify({"code": 500, "message": "附件上传失败（存储服务不可用？），请稍后重试"}), 500
        if size > MAX_FILE_MB * 1024 * 1024:
            storage.remove_object(key)
            db.session.rollback()
            return jsonify({"code": 400, "message": f"{f.filename} 超过 {MAX_FILE_MB}MB 上限"}), 400
        db.session.add(CampMeetingTaskAttachment(
            submission_id=sub.id, object_key=key, filename=f.filename[:200],
            size=size, content_type=mime))
    db.session.flush()
    att_rows = CampMeetingTaskAttachment.query.filter_by(submission_id=sub.id).all()
    if not _submission_valid(sub, t, len(att_rows)):
        db.session.rollback()
        need = {'file': '至少上传 1 个文件', 'text': '请填写文字内容'}.get(
            t.submit_type, '请填写文字或上传文件')
        return jsonify({"code": 400, "message": f"按任务要求（{SUBMIT_TYPE_TEXT.get(t.submit_type)}），{need}"}), 400
    db.session.commit()
    leader = _leader_uid(m)
    if leader and leader != user.id:
        _notify_meeting_users(m, [leader], f"任务提交：{t.title}",
                              f"{user.username} 提交了组会「{m.title}」的任务「{t.title}」，点击查看。")
    return jsonify({"code": 200, "message": "已提交", "my_submission": {
        "valid": True, "content": sub.content,
        "updated_at": sub.updated_at.isoformat() if sub.updated_at else None,
        "attachments": [_task_att_dict(a, user) for a in att_rows]}})


def _task_attachment_ctx(aid):
    a = CampMeetingTaskAttachment.query.get(aid)
    if not a:
        return None, None, None, (jsonify({"code": 404, "message": "附件不存在"}), 404)
    sub = CampMeetingTaskSubmission.query.get(a.submission_id)
    t = CampMeetingTask.query.get(sub.task_id) if sub else None
    m, err = _meeting_or_404(t.meeting_id) if t else (None, (jsonify({"code": 404, "message": "任务不存在"}), 404))
    if err:
        return None, None, None, err
    return a, sub, m, None


@bp.route("/meetings/task-attachments/<int:aid>", methods=["DELETE"])
@jwt_required()
@audit_log(operation="删除组会任务附件")
def meeting_task_attachment_delete(aid):
    """删单个任务附件（提交本人/组长）：删后仍须满足 submit_type，否则拒删。"""
    a, sub, m, err = _task_attachment_ctx(aid)
    if err:
        return err
    camp = CampSession.query.get(m.camp_session_id)
    user = _current_user()
    if sub.student_user_id != user.id and not _can_manage(user, m):
        return jsonify({"code": 403, "message": "仅提交人或组长可删除附件"}), 403
    if not _camp_writable(camp):
        return jsonify({"code": 400, "message": "CAMP_ARCHIVED_READ_ONLY 营期已归档，只读"}), 400
    t = CampMeetingTask.query.get(sub.task_id)
    remain = (CampMeetingTaskAttachment.query.filter(
        CampMeetingTaskAttachment.submission_id == sub.id,
        CampMeetingTaskAttachment.id != a.id).count())
    if not _submission_valid(sub, t, remain):
        return jsonify({"code": 400,
                        "message": "该附件是本次提交的唯一有效内容，删除前请先补文字或其他文件"}), 400
    storage.remove_object(a.object_key)
    db.session.delete(a)
    db.session.commit()
    return jsonify({"code": 200, "message": "已删除"})


@bp.route("/meetings/task-attachments/<int:aid>")
def meeting_task_attachment_download(aid):
    """任务附件代理下载（双通道鉴权；提交本人/组长/admin）。直链 URL 由详情回包内嵌短签生成。"""
    a, sub, m, err = _task_attachment_ctx(aid)
    if err:
        return err
    user, auth_err = resolve_media_request('meeting_task', aid)
    if auth_err:
        return auth_err
    if sub.student_user_id != user.id and not _can_manage(user, m):
        return jsonify({"code": 403, "message": "仅提交人或组长可下载附件"}), 403
    try:
        obj = storage.get_object(a.object_key)
    except Exception:
        return jsonify({"code": 500, "message": "附件读取失败（存储服务不可用？）"}), 500
    resp = Response(obj, mimetype=a.content_type or 'application/octet-stream')
    resp.headers["Content-Disposition"] = \
        f"attachment; filename*=UTF-8''{quote(a.filename)}"
    return resp


def _zip_name(s):
    """zip 内路径名清洗（Windows 非法字符/换行/超长）。"""
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]', '_', str(s)).strip()
    return (cleaned[:80] or '_')


@bp.route("/meetings/<int:mid>/submissions/zip/token")
@jwt_required()
def meeting_zip_token(mid):
    """换组会提交打包短签（组长；zip 生成耗时，直链走短签双通道鉴权）。"""
    m, err = _meeting_or_404(mid)
    if err:
        return err
    user = _current_user()
    if not _can_manage(user, m):
        return jsonify({"code": 403, "message": "仅组长可打包下载"}), 403
    return media_token_response('meeting_zip', mid, user.id,
                                path=f"/camp/meetings/{mid}/submissions/zip")


@bp.route("/meetings/<int:mid>/submissions/zip")
def meeting_submissions_zip(mid):
    """一键打包本次组会全部任务提交（组长）：zip 按 学生/任务 分文件夹，文字提交转 txt、
    未交任务夹内放「未提交.txt」标记；根目录「提交情况.txt」汇总矩阵。
    流式打包（内存 64MB 优先，超限落盘用完即删），与课程资源批量下载同款。"""
    m, err = _meeting_or_404(mid)
    if err:
        return err
    user, auth_err = resolve_media_request('meeting_zip', mid)
    if auth_err:
        return auth_err
    if not _can_manage(user, m):
        return jsonify({"code": 403, "message": "仅组长可打包下载"}), 403
    students = _group_students(m)
    tasks = (CampMeetingTask.query.filter_by(meeting_id=m.id)
             .order_by(CampMeetingTask.id).all())
    subs = (CampMeetingTaskSubmission.query
            .filter(CampMeetingTaskSubmission.task_id.in_([t.id for t in tasks])).all()
            if tasks else [])
    atts = {}
    for a in (CampMeetingTaskAttachment.query.filter(
            CampMeetingTaskAttachment.submission_id.in_(
                [s.id for s in subs])).all() if subs else []):
        atts.setdefault(a.submission_id, []).append(a)
    sub_by_key = {(s.task_id, s.student_user_id): s for s in subs}
    if not any(sub_by_key.get((t.id, u.id)) for t in tasks for u in students):
        return jsonify({"code": 400, "message": "本次组会暂无提交可打包"}), 400

    task_by_id = {t.id: t for t in tasks}
    lines = [f"组会：{m.title}（{m.meeting_date.isoformat()}）",
             f"组员 {len(students)} 人 · 任务 {len(tasks)} 项", ""]
    for t in tasks:
        lines.append(f"【任务】{t.title}（{SUBMIT_TYPE_TEXT.get(t.submit_type, t.submit_type)}）")
        for u in students:
            s = sub_by_key.get((t.id, u.id))
            if not s:
                lines.append(f"  未交    {u.username}")
                continue
            n = len(atts.get(s.id, []))
            text = '有文字' if (s.content or '').strip() else '无文字'
            mark = '已交' if _submission_valid(s, t, n) else '未达标'
            lines.append(f"  {mark}  {u.username}（{text} · 文件×{n}）")
        lines.append("")

    buf = tempfile.SpooledTemporaryFile(max_size=64 * 1024 * 1024)
    try:
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("提交情况.txt", "\n".join(lines))
            for u in students:
                for t in tasks:
                    folder = f"{_zip_name(u.username)}/{_zip_name(t.title)}"
                    s = sub_by_key.get((t.id, u.id))
                    if not s:
                        zf.writestr(f"{folder}/未提交.txt", "该学员未提交本任务。")
                        continue
                    if (s.content or '').strip():
                        zf.writestr(f"{folder}/文字提交.txt", s.content)
                    used = {}
                    for a in atts.get(s.id, []):
                        base = a.filename or f"file_{a.id}"
                        if base in used:
                            used[base] += 1
                            stem, ext = os.path.splitext(base)
                            base = f"{stem}({used[base]}){ext}"
                        else:
                            used[base] = 1
                        obj = storage.get_object(a.object_key)
                        try:
                            with zf.open(f"{folder}/{_zip_name(base)}", 'w') as target:
                                shutil.copyfileobj(obj, target, length=1024 * 1024)
                        finally:
                            obj.close()
                            obj.release_conn()
        buf.seek(0)
        return send_file(buf, mimetype='application/zip', as_attachment=True,
                         download_name=f"{_zip_name(m.title)}-提交打包.zip", max_age=0)
    finally:
        pass   # send_file 已接管 buf 的读；此处只兜底异常路径（SpooledTemporaryFile 随 GC 清理）


# ─────────────────────────────────────────────
# 附件代理下载（支持 HTTP Range——视频在线播放依赖 206）
# 短签直连机制见 media_sign.py（三链共用：meeting / material / submission）
# ─────────────────────────────────────────────

@bp.route("/meetings/attachments/<int:aid>/token")
@jwt_required()
def meeting_attachment_token(aid):
    """换取媒体直连短签 URL（有效期 2h；权限同查看）。前端在点击下载/播放时换取。"""
    a = CampMeetingAttachment.query.get(aid)
    if not a:
        return jsonify({"code": 404, "message": "附件不存在"}), 404
    m, err = _meeting_or_404(a.meeting_id)
    if err:
        return err
    user = _current_user()
    if not _can_view(user, m):
        return jsonify({"code": 403, "message": "仅本组成员可访问组会附件"}), 403
    return media_token_response('meeting', aid, user.id)

_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


def _parse_range(header, size):
    """解析单段 Range 头。返回 (start, end) / None（无 Range 或忽略）/ 'invalid'（416）。
    支持 bytes=start-end / bytes=start- / bytes=-suffix 三形；多段 Range 不支持按整读处理。"""
    if not header:
        return None
    m = _RANGE_RE.match(header.strip())
    if not m or (m.group(1) == "" and m.group(2) == "") or "," in header:
        return None
    start_raw, end_raw = m.group(1), m.group(2)
    if start_raw == "":                          # bytes=-N：最后 N 字节
        n = int(end_raw)
        if n <= 0 or size <= 0:
            return 'invalid'
        start, end = max(size - n, 0), size - 1
    else:
        start = int(start_raw)
        end = int(end_raw) if end_raw else size - 1
        if end >= size:
            end = size - 1
        if start > end or start >= size:
            return 'invalid'
    return start, end


@bp.route("/meetings/attachments/<int:aid>")
def meeting_attachment_download(aid):
    """附件代理下载（存储对象不暴露直链；权限同查看：本组成员/admin）。
    双通道鉴权：媒体短签（?u=&e=&st=，/token 端点换取）或常规 JWT 均可——
    `<a>/<video>` 带不了 Authorization 头走短签，脚本/带 token 客户端走 JWT。
    视频/图片 inline 供浏览器直接播放显示（<video> 拖动进度条走 Range 206），
    其余 attachment 下载；Content-Length/Accept-Ranges 全量给出。"""
    a = CampMeetingAttachment.query.get(aid)
    if not a:
        return jsonify({"code": 404, "message": "附件不存在"}), 404
    m, err = _meeting_or_404(a.meeting_id)
    if err:
        return err
    user, auth_err = resolve_media_request('meeting', aid)
    if auth_err:
        return auth_err
    if not _can_view(user, m):
        return jsonify({"code": 403, "message": "仅本组成员可下载组会附件"}), 403
    try:
        size = a.size if a.size is not None else storage.stat_object(a.object_key).size
    except Exception:
        return jsonify({"code": 500, "message": "附件读取失败（存储服务不可用？）"}), 500

    rng = _parse_range(request.headers.get("Range"), size)
    if rng == 'invalid':
        resp = jsonify({"code": 416, "message": "Range 请求越界"})
        resp.status_code = 416
        resp.headers["Content-Range"] = f"bytes */{size}"
        return resp

    offset = length = None
    if rng:
        offset, length = rng[0], rng[1] - rng[0] + 1
    try:
        obj = storage.get_object(a.object_key, offset=offset, length=length)
    except Exception:
        return jsonify({"code": 500, "message": "附件读取失败（存储服务不可用？）"}), 500

    ctype = a.content_type or 'application/octet-stream'
    inline = ctype.startswith(('video/', 'image/')) or ctype == 'application/pdf'
    disposition = 'inline' if inline else 'attachment'
    resp = Response(obj, status=206 if rng else 200, mimetype=ctype)
    resp.headers["Accept-Ranges"] = "bytes"
    resp.headers["Content-Length"] = str(length if rng else size)
    if rng:
        resp.headers["Content-Range"] = f"bytes {rng[0]}-{rng[1]}/{size}"
    resp.headers["Content-Disposition"] = \
        f"{disposition}; filename*=UTF-8''{quote(a.filename)}"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp

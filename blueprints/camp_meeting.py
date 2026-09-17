"""营期·组会留档蓝图（2026-09-17，migrate_34）：培训组（导生组）与项目组通用。

组长（培训组=导生）/项目负责人提交组会纪要：标题+会议日期+文字纪要+附件（文件/视频），
组员可查看下载；窗口=营期未归档即可提交（selecting 预备会 / running 例会都算，
与组长活动考勤同口径），结营 _camp_writable 整体只读。

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
import uuid
from datetime import datetime

from flask import Blueprint, request, jsonify, Response
from flask_jwt_extended import jwt_required
from urllib.parse import quote

from exts import db
from storage import storage
from models import (CampSession, CampMember, CampUnit, CampUnitMember,
                    CampMeeting, CampMeetingAttachment, UserModel)

from . import audit_log, _current_user
from .camp import _camp_writable, _in_my_team
from .camp_project import _camp_or_404, _unit_or_404, _is_unit_leader, _usernames
from .camp_delivery import _unit_role
from .media_sign import media_token_response, resolve_media_request
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


def _meetings_payload(sid, **filters):
    """按作用域过滤取组会列表并序列化（新会日期在前，同日新纪录在前）。"""
    rows = (CampMeeting.query.filter_by(camp_session_id=sid, **filters)
            .order_by(CampMeeting.meeting_date.desc(), CampMeeting.id.desc()).all())
    atts = _atts_by_meeting([m.id for m in rows])
    creator_ids = {m.created_by for m in rows}
    names = _usernames(creator_ids)
    return [_meeting_dict(m, atts[m.id], names) for m in rows]


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
    """新纪要通知组员（调用方剔除创建人；失败不阻断主流程）。
    source_type='camp_meeting' → 通知中心点击深链 /camp?tab=meetings&sid=
    （项目营无营期层 tab，CampView 自动回落 ProjectHub，同链两用）。"""
    for uid in user_ids:
        try:
            create_notification(
                uid, f"新组会纪要：{meeting.title}",
                f"{role_label} {by_name} 提交了「{meeting.title}」"
                f"（{meeting.meeting_date.isoformat()}）的组会纪要，点击查看。",
                category='camp', source_type='camp_meeting', source_id=meeting.id,
                camp_session_id=camp_id)
        except Exception:   # 通知失败不阻断主流程
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
    return jsonify({
        "code": 200,
        "group": {"scope": "team", "mentor_id": mentor.id, "mentor_name": mentor.username,
                  "member_count": count},
        "is_leader": mentor.id == user.id or user.is_admin(),
        "meetings": _meetings_payload(sid, scope='team', mentor_id=mentor.id),
    })


@bp.route("/sessions/<int:sid>/team-meetings", methods=["POST"])
@jwt_required()
@audit_log(operation="提交组会纪要")
def team_meeting_create(sid):
    """组长（本营导生）提交培训组组会纪要（multipart：title/meeting_date 必填 +
    content 选填 + Files[] 多文件；content/Files 至少其一）。"""
    camp, err = _camp_or_404(sid)
    if err:
        return err
    if camp.category != 'learning':
        return jsonify({"code": 400, "message": "组会纪要（培训组）仅学习营可用"}), 400
    user = _current_user()
    member = CampMember.query.filter_by(camp_session_id=sid, user_id=user.id).first()
    if not member or member.role != 'mentor':
        return jsonify({"code": 403, "message": "仅组长（导生）可提交组会纪要"}), 403
    if not _camp_writable(camp):
        return jsonify({"code": 400, "message": "CAMP_ARCHIVED_READ_ONLY 营期已归档，只读"}), 400
    fields, files, err = _parse_meeting_form()
    if err:
        return err
    if not fields["content"] and not files:
        return jsonify({"code": 400, "message": "请填写纪要内容或上传附件"}), 400
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
    return jsonify({"code": 200, "message": "组会纪要已提交",
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
    member_count = CampUnitMember.query.filter_by(unit_id=unit.id, status='active').count()
    return jsonify({
        "code": 200,
        "group": {"scope": "unit", "unit_id": unit.id, "unit_name": unit.name,
                  "member_count": member_count},
        "is_leader": _is_unit_leader(unit, user),
        "meetings": _meetings_payload(camp.id, scope='unit', unit_id=unit.id),
    })


@bp.route("/units/<int:uid>/meetings", methods=["POST"])
@jwt_required()
@audit_log(operation="提交项目组会纪要")
def unit_meeting_create(uid):
    """项目负责人提交项目组组会纪要（表单同培训组链）。"""
    unit, camp, err = _unit_or_404(uid)
    if err:
        return err
    user = _current_user()
    if not _is_unit_leader(unit, user):
        return jsonify({"code": 403, "message": "仅项目负责人可提交组会纪要"}), 403
    if not _camp_writable(camp):
        return jsonify({"code": 400, "message": "CAMP_ARCHIVED_READ_ONLY 营期已归档，只读"}), 400
    if unit.status != 'active':
        return jsonify({"code": 400, "message": "项目已停用/终止，组会记录只读"}), 400
    fields, files, err = _parse_meeting_form()
    if err:
        return err
    if not fields["content"] and not files:
        return jsonify({"code": 400, "message": "请填写纪要内容或上传附件"}), 400
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
    return jsonify({"code": 200, "message": "组会纪要已提交",
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
@audit_log(operation="编辑组会纪要")
def meeting_update(mid):
    """编辑组会（创建人/现任组长/admin）：multipart，未提供的文字字段保持旧值，
    Files[] 为追加附件（历史附件不覆盖；删单个附件走附件端点）。"""
    m, err = _meeting_or_404(mid)
    if err:
        return err
    camp = CampSession.query.get(m.camp_session_id)
    user = _current_user()
    if not _can_manage(user, m):
        return jsonify({"code": 403, "message": "仅创建人或组长可编辑组会纪要"}), 403
    denied = _scope_write_denied(m, camp)
    if denied:
        return denied
    fields, files, err = _parse_meeting_form(existing=m)
    if err:
        return err
    keep_atts = CampMeetingAttachment.query.filter_by(meeting_id=m.id).count()
    if not fields["content"] and not files and not keep_atts:
        return jsonify({"code": 400, "message": "请填写纪要内容或上传附件"}), 400
    m.title, m.meeting_date, m.content = (fields["title"], fields["meeting_date"],
                                          fields["content"])
    db.session.flush()
    err = _save_attachments(files, m, m.camp_session_id)
    if err:
        return err
    db.session.commit()
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

"""营期·选导生（Mentor Selection）蓝图。

开营前置的可选阶段（类似点外卖）：导生发布名片（照片/简介/分类/名额）→ 学员交 3 个
有序志愿（可附留言）→ 导生手动「收下」（满额即止、先到先得）→ 一轮落选学员二轮互选
（只在有余额导生里重选）→ 结束后进入开营。

核心约定：
  - live 师生链接 = camp_member.team_mentor_id（唯一真相，下游考勤看板/请假审批零改动）；
    camp_mentor_match 只是配对账本（round/来源），写入时同步设置链接。
  - 阶段不落库、读时计算（_ms_phase）：upcoming → collecting → round1 → round2 → done；
    「提前截止」= 老师把对应 deadline 改成 now（复用 session_update）。
  - 导生名片仅 upcoming/collecting 可改（防挑选期改容量/换照片）。
  - 名额口径一律按 live 链接计（teacher 经 member_assign 预分配的插班生也占名额）。

本模块与 camp.py 的分工：ms 配置解析/校验/阶段计算等共享助手放这里，camp.py 的
session_create/update/_session_dict 从这里 import（camp_ms 不反向依赖 camp，避免环）。
"""
import json
import os
from datetime import datetime, time
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from flask import Blueprint, request, jsonify, send_from_directory
from flask_jwt_extended import jwt_required

from exts import db, redis_client
from models import (
    CampSession, CampMember, CampMentorProfile, CampMentorPreference,
    CampMentorMatch, UserModel,
)

from . import camp_role, audit_log, _current_user
from .notification import create_notification
from .forms import AvatarForm

bp = Blueprint("camp_ms", __name__, url_prefix="/camp/ms")

# 名片照片目录（相对项目根；迁移脚本负责创建。DB 只存相对文件名）
MENTOR_PHOTO_DIR = os.path.join('.', 'data', 'mentor_photos')

# 营级分类标签默认集（session 未配置 ms_tags 时应用层默认）
MS_DEFAULT_TAGS = ["硬件组", "软件组", "深度学习", "机械设计", "其他"]

# 阶段常量（见 _ms_phase）
MS_DISABLED, MS_UPCOMING, MS_COLLECTING, MS_ROUND1, MS_ROUND2, MS_DONE = (
    'disabled', 'upcoming', 'collecting', 'round1', 'round2', 'done')

MS_SOURCE_TYPE = 'mentor_selection'   # 通知 source_type（String(20) 放得下 16 字符）


# ─────────────────────────────────────────────
# 配置解析 / 校验 / 阶段计算（camp.py 共用）
# ─────────────────────────────────────────────

def _camp_writable(camp):
    """archived 营只读（与 camp.py 同义，本地复刻避免反向依赖）。"""
    return camp.status != 'archived'


def _ms_tags_list(camp):
    """营级分类标签：ms_tags JSON 数组字符串 → list；未配置/脏数据回退默认集。"""
    if camp.ms_tags:
        try:
            tags = json.loads(camp.ms_tags)
            if isinstance(tags, list):
                out = [t for t in tags if isinstance(t, str) and t.strip()]
                if out:
                    return out[:20]
        except (ValueError, TypeError):
            pass
    return list(MS_DEFAULT_TAGS)


def _parse_ms_dt(val):
    """'YYYY-MM-DD HH:MM'[:SS] / ISO 字符串（或 datetime）→ naive datetime；失败 raise ValueError。"""
    if isinstance(val, datetime):
        return val.replace(tzinfo=None)
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(str(val).strip(), fmt)
        except ValueError:
            continue
    raise ValueError(f"时间格式应为 YYYY-MM-DD HH:MM：{val}")


def _apply_ms_fields(camp, d):
    """从请求体应用选导生配置（只处理出现的键；datetime 解析失败 raise ValueError）。
    空字符串/None → 置 NULL（update 里用于清除二轮等）。"""
    if "mentor_selection_enabled" in d:
        camp.mentor_selection_enabled = bool(d.get("mentor_selection_enabled"))
    for f in ("ms_preference_start", "ms_preference_deadline",
              "ms_round1_deadline", "ms_round2_deadline"):
        if f in d:
            v = d.get(f)
            setattr(camp, f, _parse_ms_dt(v) if v else None)
    if "ms_tags" in d:
        tags = d.get("ms_tags")
        if isinstance(tags, list):
            clean = [str(t).strip() for t in tags if str(t).strip()]
            camp.ms_tags = json.dumps(clean, ensure_ascii=False) if clean else None
        else:
            camp.ms_tags = None


def _validate_ms(camp):
    """选导生配置校验（create/update 存库前调用）。返回 None 或错误 message。
    规则：enabled 时 ps < pd < r1 严格递增（pd==r1 会产生零挑选窗口）；r2 留空或 > r1；
    各时间点不得晚于开营日当天末（选导生是开营前置阶段）。关闭 enabled 随时允许。"""
    if not camp.mentor_selection_enabled:
        return None
    ps, pd_, r1, r2 = (camp.ms_preference_start, camp.ms_preference_deadline,
                       camp.ms_round1_deadline, camp.ms_round2_deadline)
    if not (ps and pd_ and r1):
        return "启用选导生需设置志愿开始 / 志愿截止 / 一轮截止时间"
    if not (ps < pd_ < r1):
        return "选导生时间需满足：志愿开始 < 志愿截止 < 一轮截止"
    if r2 and r2 <= r1:
        return "二轮截止需晚于一轮截止（留空则不设二轮）"
    camp_end = datetime.combine(camp.start_date, time(23, 59, 59))
    for label, v in (("志愿开始", ps), ("志愿截止", pd_),
                     ("一轮截止", r1), ("二轮截止", r2)):
        if v and v > camp_end:
            return f"选导生{label}时间不得晚于开营日（{camp.start_date.isoformat()}）"
    return None


def _unmatched_count(camp):
    """未匹配学员数（live 链接 team_mentor_id 为空；不查账本，永不漂移）。"""
    return CampMember.query.filter_by(
        camp_session_id=camp.id, role='student', team_mentor_id=None).count()


def _ms_phase(camp, now=None):
    """选导生阶段（读时计算，不落库）：
      disabled  未启用（含"开了但没配全"的坏配置——overview 层给 config_error 告警）
      upcoming  未到开始（导生已可建名片）
      collecting 收志愿（学员提交/修改一轮志愿）
      round1    一轮挑选（导生收人）
      round2    二轮互选（仅一轮未匹配学员，只在有余额导生里重选）
      done      结束（含二轮被跳过：r2 未设，或窗口内已无人未匹配 → 提前 done）"""
    now = now or datetime.now()
    if not camp.mentor_selection_enabled:
        return MS_DISABLED
    ps, pd_, r1, r2 = (camp.ms_preference_start, camp.ms_preference_deadline,
                       camp.ms_round1_deadline, camp.ms_round2_deadline)
    if not (ps and pd_ and r1):
        return MS_DISABLED
    if now < ps:
        return MS_UPCOMING
    if now < pd_:
        return MS_COLLECTING
    if now < r1:
        return MS_ROUND1
    if r2 and r2 > r1 and now < r2 and _unmatched_count(camp) > 0:
        return MS_ROUND2
    return MS_DONE


def _ms_dict(camp):
    """选导生字段的序列化（并入 _session_dict，session_list/featured 自动带出）。"""
    def _fmt(v):
        return v.strftime("%Y-%m-%d %H:%M") if v else None
    return {
        "mentor_selection_enabled": bool(camp.mentor_selection_enabled),
        "ms_preference_start": _fmt(camp.ms_preference_start),
        "ms_preference_deadline": _fmt(camp.ms_preference_deadline),
        "ms_round1_deadline": _fmt(camp.ms_round1_deadline),
        "ms_round2_deadline": _fmt(camp.ms_round2_deadline),
        "ms_tags": _ms_tags_list(camp),
    }


# ─────────────────────────────────────────────
# 请求内小助手
# ─────────────────────────────────────────────

def _camp_or_404(sid):
    camp = CampSession.query.get(sid)
    if not camp:
        return None, (jsonify({"code": 404, "message": "营期不存在"}), 404)
    return camp, None


def _member_row(sid, uid, role=None):
    q = CampMember.query.filter_by(camp_session_id=sid, user_id=uid)
    if role:
        q = q.filter_by(role=role)
    return q.first()


def _is_staff(user):
    return user.is_admin()


def _live_matched(camp_id, mentor_id):
    """导生已收人数（live 链接口径：含 teacher 预分配的插班生）。"""
    return CampMember.query.filter_by(
        camp_session_id=camp_id, role='student', team_mentor_id=mentor_id).count()


def _avatar_url(u):
    return f"/data/avatars/{u.avatar_url}" if u and u.avatar_url else None


def _photo_url(p):
    return f"/camp/ms/photo/{p.photo}" if p and p.photo else None


def _current_round(phase):
    return 2 if phase == MS_ROUND2 else 1


def _fmt_dt(v):
    return v.strftime("%Y-%m-%d %H:%M") if v else None


# ─────────────────────────────────────────────
# 阶段过渡通知（lazy 触发：读端点顺带检查；Redis SETNX 防并发重发）
# ─────────────────────────────────────────────

def _maybe_notify_transition(camp, phase):
    """读端点顺带调用。Redis 游标 ms:phase_last:{sid} 去重 + SETNX 锁；
    Redis 不可用 → 降级跳过（绝不裸发全营重复通知）。
    老师 session_update 动过 ms 配置会删游标（允许按新时间线重发）。"""
    if phase not in (MS_COLLECTING, MS_ROUND1, MS_ROUND2, MS_DONE):
        return
    key_last, key_lock = f"ms:phase_last:{camp.id}", f"ms:notify_lock:{camp.id}"
    try:
        last = redis_client.get(key_last)
        if last and last.decode() == phase:
            return
        if not redis_client.set(key_lock, "1", nx=True, ex=30):
            return                       # 别的请求正在发
    except Exception:
        return
    try:
        _send_phase_notifications(camp, phase)
        db.session.commit()
        redis_client.set(key_last, phase)
    except Exception:
        db.session.rollback()
    finally:
        try:
            redis_client.delete(key_lock)
        except Exception:
            pass


def _send_phase_notifications(camp, phase):
    """按阶段向对应人群发通知（只 add 不 commit，与 _maybe_notify_transition 的 commit 同事务）。"""
    name = camp.name
    students = [m.user_id for m in CampMember.query.filter_by(
        camp_session_id=camp.id, role='student').all()]
    mentors = [m.user_id for m in CampMember.query.filter_by(
        camp_session_id=camp.id, role='mentor').all()]
    common = dict(category='camp', source_type=MS_SOURCE_TYPE,
                  source_id=camp.id, camp_session_id=camp.id)

    if phase == MS_COLLECTING:
        for uid in students:
            create_notification(uid, "选导生开始",
                                f"「{name}」选导生开始，请在 {_fmt_dt(camp.ms_preference_deadline)} 前浏览导生名片并提交 3 个志愿。",
                                **common)
    elif phase == MS_ROUND1:
        counts = dict(db.session.query(CampMentorPreference.mentor_user_id, func.count())
                      .filter(CampMentorPreference.camp_session_id == camp.id,
                              CampMentorPreference.round == 1)
                      .group_by(CampMentorPreference.mentor_user_id).all())
        for uid in mentors:
            n = counts.get(uid, 0)
            create_notification(uid, "选导生：开始挑选",
                                f"「{name}」学员志愿已收集完毕，{n} 位学员选择你，请在 {_fmt_dt(camp.ms_round1_deadline)} 前完成挑选。",
                                **common)
    elif phase == MS_ROUND2:
        unmatched = [m.user_id for m in CampMember.query.filter_by(
            camp_session_id=camp.id, role='student', team_mentor_id=None).all()]
        for uid in unmatched:
            create_notification(uid, "选导生：二轮互选",
                                f"你在「{name}」一轮未被匹配，请在 {_fmt_dt(camp.ms_round2_deadline)} 前在仍有名额的导生中重新提交志愿。",
                                is_important=True, **common)
    elif phase == MS_DONE:
        for uid in students + mentors:
            create_notification(uid, "选导生结束",
                                f"「{name}」选导生已结束，结果已公布，可在营期页查看。",
                                **common)


# ─────────────────────────────────────────────
# 名片照片（公开静态：img 标签带不了 JWT，与 /data/avatars 同口径）
# ─────────────────────────────────────────────

@bp.route("/photo/<path:filename>")
def mentor_photo(filename):
    return send_from_directory(MENTOR_PHOTO_DIR, filename)


# ─────────────────────────────────────────────
# 阶段总览（成员/员工；顺带触发过渡通知）
# ─────────────────────────────────────────────

@bp.route("/<int:sid>/phase")
@jwt_required()
def phase_view(sid):
    camp, err = _camp_or_404(sid)
    if err:
        return err
    user = _current_user()
    member = _member_row(sid, user.id)
    if not member and not _is_staff(user):
        return jsonify({"code": 403, "message": "仅营期成员可查看"}), 403

    phase = _ms_phase(camp)
    if camp.mentor_selection_enabled and phase != MS_DISABLED:
        _maybe_notify_transition(camp, phase)

    me = {"role": "staff" if not member else member.role}
    if member and member.role == 'student':
        my_mentor = UserModel.query.get(member.team_mentor_id) if member.team_mentor_id else None
        r1 = _my_preferences(sid, user.id, 1)
        r2 = _my_preferences(sid, user.id, 2)
        submittable = None
        if not member.team_mentor_id:
            if phase == MS_COLLECTING:
                submittable = 1
            elif phase == MS_ROUND2:
                submittable = 2
        me.update({
            "round1": r1, "round2": r2, "submittable_round": submittable,
            "unmatched": member.team_mentor_id is None,
            "my_mentor": ({"user_id": my_mentor.id, "username": my_mentor.username}
                          if my_mentor else None),
        })
    elif member and member.role == 'mentor':
        profile = CampMentorProfile.query.filter_by(
            camp_session_id=sid, user_id=user.id).first()
        matched = _live_matched(sid, user.id)
        cur = _current_round(phase) if phase in (MS_ROUND1, MS_ROUND2, MS_COLLECTING) else None
        me.update({
            "has_profile": profile is not None,
            "profile": _profile_dict(profile) if profile else None,
            "profile_locked": phase not in (MS_UPCOMING, MS_COLLECTING),
            "matched_count": matched,
            "remaining": max(0, (profile.capacity if profile else 0) - matched),
            "suitor_count": (CampMentorPreference.query.filter_by(
                camp_session_id=sid, mentor_user_id=user.id, round=cur).count()
                if cur else 0),
        })

    # 从众信号：一轮已交志愿的去重学员数 / 营内学员总数（前端 collecting/round1 展示用）
    submitted = (db.session.query(CampMentorPreference.student_user_id)
                 .filter_by(camp_session_id=sid, round=1).distinct().count())
    student_total = CampMember.query.filter_by(
        camp_session_id=sid, role='student').count()

    return jsonify({"code": 200, "phase": phase,
                    "enabled": bool(camp.mentor_selection_enabled),
                    "config_error": bool(camp.mentor_selection_enabled and not (
                        camp.ms_preference_start and camp.ms_preference_deadline
                        and camp.ms_round1_deadline)),
                    "deadlines": {
                        "preference_start": _fmt_dt(camp.ms_preference_start),
                        "preference_deadline": _fmt_dt(camp.ms_preference_deadline),
                        "round1_deadline": _fmt_dt(camp.ms_round1_deadline),
                        "round2_deadline": _fmt_dt(camp.ms_round2_deadline),
                    },
                    "round2_enabled": bool(camp.ms_round2_deadline
                                           and camp.ms_round2_deadline > camp.ms_round1_deadline),
                    "ms_tags": _ms_tags_list(camp),
                    "stats": {"submitted": submitted, "students": student_total},
                    "me": me})


# ─────────────────────────────────────────────
# 导生名片
# ─────────────────────────────────────────────

def _profile_dict(p):
    try:
        tags = json.loads(p.tags) if p.tags else []
    except (ValueError, TypeError):
        tags = []
    u = UserModel.query.get(p.user_id)
    matched = _live_matched(p.camp_session_id, p.user_id)
    return {
        "user_id": p.user_id, "username": u.username if u else "",
        "avatar": _avatar_url(u), "photo_url": _photo_url(p),
        "bio": p.bio or "", "tags": [t for t in tags if isinstance(t, str)],
        "capacity": p.capacity or 0, "matched": matched,
        "remaining": max(0, (p.capacity or 0) - matched),
        "full": matched >= (p.capacity or 0),
        "updated_at": _fmt_dt(p.updated_at),
    }


@bp.route("/<int:sid>/profile")
@jwt_required()
def profile_get(sid):
    camp, err = _camp_or_404(sid)
    if err:
        return err
    if not camp.mentor_selection_enabled:
        return jsonify({"code": 400, "message": "该营期未启用选导生"}), 400
    user = _current_user()
    if not _member_row(sid, user.id, role='mentor'):
        return jsonify({"code": 403, "message": "仅本营导生可操作"}), 403
    p = CampMentorProfile.query.filter_by(camp_session_id=sid, user_id=user.id).first()
    return jsonify({"code": 200, "profile": _profile_dict(p) if p else None,
                    "locked": _ms_phase(camp) not in (MS_UPCOMING, MS_COLLECTING)})


@bp.route("/<int:sid>/profile", methods=["PUT"])
@jwt_required()
@audit_log(operation="编辑导生名片")
def profile_put(sid):
    camp, err = _camp_or_404(sid)
    if err:
        return err
    if not camp.mentor_selection_enabled:
        return jsonify({"code": 400, "message": "该营期未启用选导生"}), 400
    if not _camp_writable(camp):
        return jsonify({"code": 400, "message": "营期已归档，只读"}), 400
    user = _current_user()
    if not _member_row(sid, user.id, role='mentor'):
        return jsonify({"code": 403, "message": "仅本营导生可操作"}), 403
    phase = _ms_phase(camp)
    if phase not in (MS_UPCOMING, MS_COLLECTING):
        return jsonify({"code": 400, "message": "志愿已截止，名片已锁定"}), 400

    d = request.json or {}
    bio = (d.get("bio") or "").strip()
    if len(bio) > 1000:
        return jsonify({"code": 400, "message": "自我介绍不能超过 1000 字"}), 400
    tags = d.get("tags") or []
    if not isinstance(tags, list):
        return jsonify({"code": 400, "message": "tags 需为数组"}), 400
    allowed = set(_ms_tags_list(camp))
    bad = [t for t in tags if t not in allowed]
    if bad:
        return jsonify({"code": 400, "message": f"标签不在营期可选范围: {', '.join(map(str, bad))}"}), 400
    try:
        capacity = int(d.get("capacity") or 8)
    except (ValueError, TypeError):
        return jsonify({"code": 400, "message": "capacity 需为整数"}), 400
    if not (1 <= capacity <= 30):
        return jsonify({"code": 400, "message": "capacity 需在 1-30 之间"}), 400
    matched = _live_matched(sid, user.id)
    if capacity < matched:
        return jsonify({"code": 400, "message": f"capacity 不能低于已收人数（{matched}）"}), 400

    p = CampMentorProfile.query.filter_by(camp_session_id=sid, user_id=user.id).first()
    if not p:
        p = CampMentorProfile(camp_session_id=sid, user_id=user.id, capacity=capacity)
        db.session.add(p)
    p.bio = bio
    p.tags = json.dumps([str(t) for t in tags], ensure_ascii=False)
    p.capacity = capacity
    db.session.commit()
    return jsonify({"code": 200, "message": "已保存", "profile": _profile_dict(p)})


@bp.route("/<int:sid>/profile/photo", methods=["POST"])
@jwt_required()
@audit_log(operation="上传导生名片照片")
def profile_photo_upload(sid):
    camp, err = _camp_or_404(sid)
    if err:
        return err
    if not camp.mentor_selection_enabled:
        return jsonify({"code": 400, "message": "该营期未启用选导生"}), 400
    if not _camp_writable(camp):
        return jsonify({"code": 400, "message": "营期已归档，只读"}), 400
    user = _current_user()
    if not _member_row(sid, user.id, role='mentor'):
        return jsonify({"code": 403, "message": "仅本营导生可操作"}), 403
    if _ms_phase(camp) not in (MS_UPCOMING, MS_COLLECTING):
        return jsonify({"code": 400, "message": "志愿已截止，名片已锁定"}), 400

    form = AvatarForm(request.files)
    if not form.validate():
        return jsonify({"code": 400, "message": "照片仅支持 jpg/jpeg/png，且不超过 5MB"}), 400
    file = form.avatar.data
    ext = os.path.splitext(file.filename or "")[1].lower().lstrip(".")
    if ext not in ("jpg", "jpeg", "png"):
        return jsonify({"code": 400, "message": "照片仅支持 jpg/jpeg/png"}), 400

    filename = f"{sid}_{user.id}.{ext}"
    os.makedirs(MENTOR_PHOTO_DIR, exist_ok=True)
    p = CampMentorProfile.query.filter_by(camp_session_id=sid, user_id=user.id).first()
    if p and p.photo and p.photo != filename:
        try:
            os.remove(os.path.join(MENTOR_PHOTO_DIR, p.photo))
        except OSError:
            pass
    file.save(os.path.join(MENTOR_PHOTO_DIR, filename))
    if not p:
        p = CampMentorProfile(camp_session_id=sid, user_id=user.id, photo=filename)
        db.session.add(p)
    else:
        p.photo = filename
    db.session.commit()
    return jsonify({"code": 200, "message": "照片已上传", "photo_url": _photo_url(p)})


# ─────────────────────────────────────────────
# 浏览导生（学员端外卖卡片）
# ─────────────────────────────────────────────

@bp.route("/<int:sid>/mentors")
@jwt_required()
def mentors_list(sid):
    camp, err = _camp_or_404(sid)
    if err:
        return err
    if not camp.mentor_selection_enabled:
        return jsonify({"code": 400, "message": "该营期未启用选导生"}), 400
    user = _current_user()
    member = _member_row(sid, user.id)
    if not member and not _is_staff(user):
        return jsonify({"code": 403, "message": "仅营期成员可查看"}), 403

    phase = _ms_phase(camp)
    if camp.mentor_selection_enabled and phase != MS_DISABLED:
        _maybe_notify_transition(camp, phase)

    tag = request.args.get("tag")
    profiles = CampMentorProfile.query.filter_by(camp_session_id=sid).all()
    data = []
    for p in profiles:
        item = _profile_dict(p)
        if tag and tag not in item["tags"]:
            continue
        data.append(item)
    # 满员沉底、其余按剩余名额降序（外卖式"还有余量的店排前面"）
    data.sort(key=lambda x: (x["full"], -x["remaining"]))
    return jsonify({"code": 200, "phase": phase, "mentors": data})


# ─────────────────────────────────────────────
# 学员志愿
# ─────────────────────────────────────────────

def _my_preferences(sid, student_id, round_):
    rows = (CampMentorPreference.query
            .filter_by(camp_session_id=sid, student_user_id=student_id, round=round_)
            .order_by(CampMentorPreference.rank).all())
    out = []
    for r in rows:
        m = UserModel.query.get(r.mentor_user_id)
        out.append({"rank": r.rank, "mentor_id": r.mentor_user_id,
                    "mentor_name": m.username if m else "", "note": r.note})
    return out


@bp.route("/<int:sid>/preferences/mine")
@jwt_required()
def preferences_mine(sid):
    camp, err = _camp_or_404(sid)
    if err:
        return err
    if not camp.mentor_selection_enabled:
        return jsonify({"code": 400, "message": "该营期未启用选导生"}), 400
    user = _current_user()
    if not _member_row(sid, user.id, role='student'):
        return jsonify({"code": 403, "message": "仅本营学员可操作"}), 403
    return jsonify({"code": 200,
                    "round1": _my_preferences(sid, user.id, 1),
                    "round2": _my_preferences(sid, user.id, 2)})


@bp.route("/<int:sid>/preferences", methods=["POST"])
@jwt_required()
@audit_log(operation="提交选导生志愿")
def preferences_submit(sid):
    camp, err = _camp_or_404(sid)
    if err:
        return err
    if not camp.mentor_selection_enabled:
        return jsonify({"code": 400, "message": "该营期未启用选导生"}), 400
    if not _camp_writable(camp):
        return jsonify({"code": 400, "message": "营期已归档，只读"}), 400
    user = _current_user()
    if not _member_row(sid, user.id, role='student'):
        return jsonify({"code": 403, "message": "仅本营学员可操作"}), 403

    phase = _ms_phase(camp)
    student = _member_row(sid, user.id, role='student')
    if student.team_mentor_id:
        return jsonify({"code": 403, "message": "你已有归属导生，无需再提交志愿"}), 403
    if phase == MS_COLLECTING:
        round_ = 1
    elif phase == MS_ROUND2:
        round_ = 2
    else:
        return jsonify({"code": 403, "message": "当前不在志愿提交窗口"}), 403

    d = request.json or {}
    lst = d.get("list")
    if not isinstance(lst, list) or not (1 <= len(lst) <= 3):
        return jsonify({"code": 400, "message": "需提交 1-3 个有序志愿"}), 400

    seen = set()
    items = []
    for it in lst:
        if not isinstance(it, dict):
            return jsonify({"code": 400, "message": "志愿格式错误"}), 400
        mid = it.get("mentor_id")
        if not mid or mid in seen:
            return jsonify({"code": 400, "message": "志愿导师缺失或重复"}), 400
        seen.add(mid)
        note = (it.get("note") or "").strip()
        if len(note) > 200:
            return jsonify({"code": 400, "message": "留言不能超过 200 字"}), 400
        m = UserModel.query.get(mid)
        p = CampMentorProfile.query.filter_by(camp_session_id=sid, user_id=mid).first()
        if not _member_row(sid, mid, role='mentor') or not p:
            return jsonify({"code": 400, "message": "所选导师不在本营或未发布名片"}), 400
        if round_ == 2 and _live_matched(sid, mid) >= (p.capacity or 0):
            return jsonify({"code": 400, "message": f"{m.username if m else '该导师'} 名额已满，请调整志愿"}), 400
        items.append((mid, note))

    # 替换语义：该轮整组删旧插新（截止前可反复改）
    CampMentorPreference.query.filter_by(
        camp_session_id=sid, student_user_id=user.id, round=round_).delete(
        synchronize_session=False)
    for rank, (mid, note) in enumerate(items, start=1):
        db.session.add(CampMentorPreference(
            camp_session_id=sid, student_user_id=user.id,
            mentor_user_id=mid, round=round_, rank=rank, note=note or None))
    db.session.commit()
    return jsonify({"code": 200, "message": "志愿已提交（截止前可修改）",
                    "round": round_, "preferences": _my_preferences(sid, user.id, round_)})


# ─────────────────────────────────────────────
# 导生挑选（订单式）
# ─────────────────────────────────────────────

@bp.route("/<int:sid>/suitors")
@jwt_required()
def suitors_list(sid):
    camp, err = _camp_or_404(sid)
    if err:
        return err
    if not camp.mentor_selection_enabled:
        return jsonify({"code": 400, "message": "该营期未启用选导生"}), 400
    user = _current_user()
    if not _member_row(sid, user.id, role='mentor'):
        return jsonify({"code": 403, "message": "仅本营导生可操作"}), 403

    phase = _ms_phase(camp)
    if camp.mentor_selection_enabled and phase != MS_DISABLED:
        _maybe_notify_transition(camp, phase)
    if phase not in (MS_COLLECTING, MS_ROUND1, MS_ROUND2):
        return jsonify({"code": 400, "message": "当前不在挑选阶段"}), 400

    round_ = _current_round(phase)
    rows = (CampMentorPreference.query
            .filter_by(camp_session_id=sid, mentor_user_id=user.id, round=round_)
            .order_by(CampMentorPreference.rank, CampMentorPreference.created_at).all())
    # 已被收下的标注归属（学员看得到自己被谁收下，导生端也可看到避免误点）
    member_map = {m.user_id: m for m in CampMember.query.filter_by(
        camp_session_id=sid, role='student').all()}
    mentor_ids = {m.team_mentor_id for m in member_map.values() if m.team_mentor_id}
    users = {u.id: u for u in UserModel.query.filter(
        UserModel.id.in_([r.student_user_id for r in rows] + list(mentor_ids))).all()} \
        if rows else {}
    data = []
    for r in rows:
        u = users.get(r.student_user_id)
        m = member_map.get(r.student_user_id)
        matched_to = m.team_mentor_id if m else None
        mu = users.get(matched_to) if matched_to else None
        data.append({
            "user_id": r.student_user_id, "username": u.username if u else "",
            "avatar": _avatar_url(u), "rank": r.rank, "note": r.note,
            "matched": matched_to is not None,
            "matched_mentor_name": mu.username if mu else None,
        })
    profile = CampMentorProfile.query.filter_by(
        camp_session_id=sid, user_id=user.id).first()
    matched = _live_matched(sid, user.id)
    return jsonify({"code": 200, "round": round_, "preview": phase == MS_COLLECTING,
                    "phase": phase,
                    "capacity": profile.capacity if profile else 0,
                    "matched": matched,
                    "remaining": max(0, (profile.capacity if profile else 0) - matched),
                    "suitors": data})


@bp.route("/<int:sid>/pick", methods=["POST"])
@jwt_required()
@audit_log(operation="导生收下学员")
def pick_student(sid):
    camp, err = _camp_or_404(sid)
    if err:
        return err
    if not camp.mentor_selection_enabled:
        return jsonify({"code": 400, "message": "该营期未启用选导生"}), 400
    if not _camp_writable(camp):
        return jsonify({"code": 400, "message": "营期已归档，只读"}), 400
    user = _current_user()
    if not _member_row(sid, user.id, role='mentor'):
        return jsonify({"code": 403, "message": "仅本营导生可操作"}), 403

    phase = _ms_phase(camp)
    if phase not in (MS_ROUND1, MS_ROUND2):
        return jsonify({"code": 400, "message": "当前不在挑选窗口"}), 400
    round_ = _current_round(phase)

    student_id = (request.json or {}).get("student_id")
    if not student_id:
        return jsonify({"code": 400, "message": "缺少 student_id"}), 400

    # 锁序：学员成员行 → 我的名片行（全体 pick 同序，无死锁；分别串行化"抢同一学员"与"同导生容量"）
    student = (CampMember.query.filter_by(
        camp_session_id=sid, user_id=student_id, role='student')
        .with_for_update().first())
    if not student:
        return jsonify({"code": 404, "message": "学员不在本营"}), 404
    profile = (CampMentorProfile.query.filter_by(
        camp_session_id=sid, user_id=user.id).with_for_update().first())

    pref = CampMentorPreference.query.filter_by(
        camp_session_id=sid, student_user_id=student_id,
        mentor_user_id=user.id, round=round_).first()
    if not pref:
        return jsonify({"code": 400, "message": "该学员本轮未选择你"}), 400
    if student.team_mentor_id:
        if student.team_mentor_id == user.id:
            return jsonify({"code": 409, "message": "该学员已在你团队中"}), 409
        return jsonify({"code": 409, "message": "该学员已被其他导生收下"}), 409
    if not profile:
        return jsonify({"code": 400, "message": "你尚未发布名片，无法收人"}), 400
    if _live_matched(sid, user.id) >= (profile.capacity or 0):
        return jsonify({"code": 409, "message": "名额已满"}), 409

    db.session.add(CampMentorMatch(camp_session_id=sid, mentor_user_id=user.id,
                                   student_user_id=student_id, round=round_,
                                   source='mentor_pick'))
    student.team_mentor_id = user.id
    # 通知学员（同事务）
    create_notification(student_id, "选导生：你被选中",
                        f"「{camp.name}」导生 {user.username} 收下了你，你已加入其团队。",
                        category='camp', source_type=MS_SOURCE_TYPE,
                        source_id=camp.id, camp_session_id=sid, is_important=True)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify({"code": 409, "message": "该学员刚被其他导生收下"}), 409
    return jsonify({"code": 200, "message": "已收下",
                    "matched_count": _live_matched(sid, user.id)})


@bp.route("/<int:sid>/matched")
@jwt_required()
def matched_list(sid):
    camp, err = _camp_or_404(sid)
    if err:
        return err
    if not camp.mentor_selection_enabled:
        return jsonify({"code": 400, "message": "该营期未启用选导生"}), 400
    user = _current_user()
    if not _member_row(sid, user.id, role='mentor'):
        return jsonify({"code": 403, "message": "仅本营导生可操作"}), 403
    # live 团队 + 账本 provenance（teacher member_assign 预分配的插班生无账本行，同样展示）
    rows = CampMember.query.filter_by(
        camp_session_id=sid, role='student', team_mentor_id=user.id).all()
    ledger = {m.student_user_id: m for m in CampMentorMatch.query.filter_by(
        camp_session_id=sid, mentor_user_id=user.id).all()}
    data = []
    for r in rows:
        u = UserModel.query.get(r.user_id)
        led = ledger.get(r.user_id)
        data.append({"user_id": r.user_id, "username": u.username if u else "",
                     "avatar": _avatar_url(u),
                     "round": led.round if led else None,
                     "source": led.source if led else None,
                     "joined_at": _fmt_dt(r.joined_at)})
    data.sort(key=lambda x: x["joined_at"] or "")
    return jsonify({"code": 200, "matched": data})


# ─────────────────────────────────────────────
# 老师看板 / 手动指派 / 结果
# ─────────────────────────────────────────────

@bp.route("/<int:sid>/overview")
@jwt_required()
@camp_role()
def overview(sid):
    camp, err = _camp_or_404(sid)
    if err:
        return err
    if not camp.mentor_selection_enabled:
        return jsonify({"code": 400, "message": "该营期未启用选导生"}), 400

    phase = _ms_phase(camp)
    mentor_rows = CampMember.query.filter_by(camp_session_id=sid, role='mentor').all()
    student_rows = CampMember.query.filter_by(camp_session_id=sid, role='student').all()
    profiles = {p.user_id: p for p in CampMentorProfile.query.filter_by(
        camp_session_id=sid).all()}
    chose = {(r[0], r[1]): r[2] for r in db.session.query(
        CampMentorPreference.mentor_user_id, CampMentorPreference.round, func.count())
        .filter(CampMentorPreference.camp_session_id == sid)
        .group_by(CampMentorPreference.mentor_user_id, CampMentorPreference.round).all()}
    submitted = {(r[0], r[1]) for r in db.session.query(
        CampMentorPreference.student_user_id, CampMentorPreference.round)
        .filter(CampMentorPreference.camp_session_id == sid).all()}
    users = {u.id: u for u in UserModel.query.filter(UserModel.id.in_(
        [m.user_id for m in mentor_rows + student_rows])).all()}

    mentors = []
    for m in mentor_rows:
        u = users.get(m.user_id)
        p = profiles.get(m.user_id)
        matched = _live_matched(sid, m.user_id)
        mentors.append({
            "user_id": m.user_id, "username": u.username if u else "",
            "has_profile": p is not None,
            "capacity": p.capacity if p else 0,
            "chose_r1": chose.get((m.user_id, 1), 0),
            "chose_r2": chose.get((m.user_id, 2), 0),
            "matched": matched,
            "remaining": max(0, (p.capacity if p else 0) - matched),
        })
    students = []
    for s in student_rows:
        u = users.get(s.user_id)
        mu = users.get(s.team_mentor_id) if s.team_mentor_id else None
        students.append({
            "user_id": s.user_id, "username": u.username if u else "",
            "matched": s.team_mentor_id is not None,
            "mentor_name": mu.username if mu else None,
            "submitted_r1": (s.user_id, 1) in submitted,
            "submitted_r2": (s.user_id, 2) in submitted,
        })
    matched_n = sum(1 for s in student_rows if s.team_mentor_id)
    return jsonify({"code": 200, "phase": phase,
                    "config_error": bool(not (camp.ms_preference_start
                                              and camp.ms_preference_deadline
                                              and camp.ms_round1_deadline)),
                    "deadlines": {
                        "preference_start": _fmt_dt(camp.ms_preference_start),
                        "preference_deadline": _fmt_dt(camp.ms_preference_deadline),
                        "round1_deadline": _fmt_dt(camp.ms_round1_deadline),
                        "round2_deadline": _fmt_dt(camp.ms_round2_deadline),
                    },
                    "mentors": mentors, "students": students,
                    "stats": {"students": len(student_rows), "matched": matched_n,
                              "unmatched": len(student_rows) - matched_n,
                              "r2_enabled": bool(camp.ms_round2_deadline
                                                 and camp.ms_round2_deadline > camp.ms_round1_deadline)}})


@bp.route("/<int:sid>/assign", methods=["POST"])
@jwt_required()
@camp_role()
@audit_log(operation="手动指派导生")
def assign(sid):
    camp, err = _camp_or_404(sid)
    if err:
        return err
    if not camp.mentor_selection_enabled:
        return jsonify({"code": 400, "message": "该营期未启用选导生"}), 400
    if not _camp_writable(camp):
        return jsonify({"code": 400, "message": "营期已归档，只读"}), 400
    d = request.json or {}
    student_id, mentor_id = d.get("student_id"), d.get("mentor_id")
    if not student_id or not mentor_id:
        return jsonify({"code": 400, "message": "缺少 student_id/mentor_id"}), 400

    student = (CampMember.query.filter_by(
        camp_session_id=sid, user_id=student_id, role='student')
        .with_for_update().first())
    if not student:
        return jsonify({"code": 404, "message": "学员不在本营"}), 404
    mentor = _member_row(sid, mentor_id, role='mentor')
    if not mentor:
        return jsonify({"code": 404, "message": "导生不在本营"}), 404
    profile = CampMentorProfile.query.filter_by(camp_session_id=sid, user_id=mentor_id).first()
    cap = profile.capacity if profile else 0
    mu = UserModel.query.get(mentor_id)
    if not d.get("allow_over") and _live_matched(sid, mentor_id) >= cap:
        return jsonify({"code": 409,
                        "message": f"{mu.username if mu else '该导生'} 名额已满（{cap}），如需越过请 allow_over"}), 409
    if student.team_mentor_id == mentor_id:
        return jsonify({"code": 400, "message": "该学员已归属此导生"}), 400

    # upsert 账本 + live 链接
    row = CampMentorMatch.query.filter_by(
        camp_session_id=sid, student_user_id=student_id).first()
    if row:
        row.mentor_user_id = mentor_id
        row.round = None
        row.source = 'admin'
    else:
        db.session.add(CampMentorMatch(camp_session_id=sid, mentor_user_id=mentor_id,
                                       student_user_id=student_id, round=None,
                                       source='admin'))
    student.team_mentor_id = mentor_id
    create_notification(student_id, "选导生：导生已指派",
                        f"老师已将你指派给「{camp.name}」导生 {mu.username if mu else ''}。",
                        category='camp', source_type=MS_SOURCE_TYPE,
                        source_id=camp.id, camp_session_id=sid, is_important=True)
    db.session.commit()
    return jsonify({"code": 200, "message": "已指派"})


@bp.route("/<int:sid>/results")
@jwt_required()
def results(sid):
    camp, err = _camp_or_404(sid)
    if err:
        return err
    if not camp.mentor_selection_enabled:
        return jsonify({"code": 400, "message": "该营期未启用选导生"}), 400
    user = _current_user()
    member = _member_row(sid, user.id)
    if not member and not _is_staff(user):
        return jsonify({"code": 403, "message": "仅营期成员可查看"}), 403
    if _ms_phase(camp) != MS_DONE:
        return jsonify({"code": 400, "message": "选导生尚未结束"}), 400

    ledger = {m.student_user_id: m for m in CampMentorMatch.query.filter_by(
        camp_session_id=sid).all()}
    if member and member.role == 'student':
        mu = UserModel.query.get(member.team_mentor_id) if member.team_mentor_id else None
        return jsonify({"code": 200, "my_mentor": (
            {"user_id": mu.id, "username": mu.username} if mu else None)})
    if member and member.role == 'mentor':
        rows = CampMember.query.filter_by(
            camp_session_id=sid, role='student', team_mentor_id=user.id).all()
        data = []
        for r in rows:
            u = UserModel.query.get(r.user_id)
            led = ledger.get(r.user_id)
            data.append({"user_id": r.user_id, "username": u.username if u else "",
                         "round": led.round if led else None,
                         "source": led.source if led else None})
        return jsonify({"code": 200, "my_students": data})
    # teacher / super_admin：全量
    rows = CampMember.query.filter_by(camp_session_id=sid, role='student').all()
    data = []
    for r in rows:
        su = UserModel.query.get(r.user_id)
        mu = UserModel.query.get(r.team_mentor_id) if r.team_mentor_id else None
        led = ledger.get(r.user_id)
        data.append({"student_id": r.user_id, "student_name": su.username if su else "",
                     "mentor_id": r.team_mentor_id, "mentor_name": mu.username if mu else None,
                     "round": led.round if led else None,
                     "source": led.source if led else None})
    return jsonify({"code": 200, "results": data})

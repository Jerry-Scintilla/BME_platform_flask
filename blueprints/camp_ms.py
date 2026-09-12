"""营期·选导生（Mentor Selection）蓝图。

开营前置的可选阶段（单轮制）：导生发布名片（照片/简介/分类/名额）→ 学员在收集期
提交 3 个有序志愿（可附留言，截止前可整组替换）→ 截止后老师导出 CSV → 线下协调 →
老师批量指派回填（主路径）；协调期导生也可在人员确认页自助勾选（/pick，共用
live 链接/账本写入路径，账本 source=mentor_pick）。旧两轮互选已退役。

核心约定：
  - live 师生链接 = camp_member.team_mentor_id（唯一真相，下游考勤看板/请假审批零改动）；
    camp_mentor_match 只是配对账本（round/来源），写入时同步设置链接。
  - 阶段不落库、读时计算（_ms_phase）：upcoming → collecting → done；
    「提前截止」= 老师把 ms_preference_deadline 改成 now（复用 session_update）。
  - 导生名片仅 upcoming/collecting 可改（防协调期改容量/换照片）。
  - 名额口径一律按 live 链接计（teacher 经 member_assign 预分配的插班生也占名额）。
  - 一轮/二轮截止字段（ms_round1_deadline / ms_round2_deadline）已随单轮化废弃：
    保留模型列与序列化键仅为兼容旧数据/旧前端，配置写入与阶段计算一律忽略。

本模块与 camp.py 的分工：ms 配置解析/校验/阶段计算等共享助手放这里，camp.py 的
session_create/update/_session_dict 从这里 import（camp_ms 不反向依赖 camp，避免环）。
"""
import csv
import io
import json
import os
from datetime import datetime, time
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from flask import Blueprint, Response, request, jsonify, send_from_directory
from flask_jwt_extended import jwt_required

from exts import db, redis_client
from models import (
    CampSession, CampMember, CampMentorProfile, CampMentorPreference,
    CampMentorMatch, CampMentorFavorite, UserModel, UserCourseModel,
)

from . import camp_role, audit_log, _current_user
from .notification import create_notification
from .forms import AvatarForm

bp = Blueprint("camp_ms", __name__, url_prefix="/camp/ms")

# 名片照片目录（相对项目根；迁移脚本负责创建。DB 只存相对文件名）
MENTOR_PHOTO_DIR = os.path.join('.', 'data', 'mentor_photos')

# 营级分类标签默认集（session 未配置 ms_tags 时应用层默认）
MS_DEFAULT_TAGS = ["硬件组", "软件组", "深度学习", "机械设计", "其他"]

# 阶段常量（见 _ms_phase；单轮化后不再有导生挑选阶段）
MS_DISABLED, MS_UPCOMING, MS_COLLECTING, MS_DONE = (
    'disabled', 'upcoming', 'collecting', 'done')

MS_SOURCE_TYPE = 'mentor_selection'   # 通知 source_type（String(20) 放得下 16 字符）


# ─────────────────────────────────────────────
# 配置解析 / 校验 / 阶段计算（camp.py 共用）
# ─────────────────────────────────────────────

def _camp_writable(camp):
    """archived 营只读（与 camp.py 同义，本地复刻避免反向依赖）。"""
    return camp.status != 'archived'


def _ms_directions_raw(camp):
    """ms_tags 原始解析：兼容两种形状——
    新：[{"name":"硬件组","course_id":12},...]（09-12 方向制：分类=方向+课程）
    旧：["硬件组",...]（纯字符串，legacy 营；无课程绑定，编辑时强制补齐）
    返回 list（元素为 dict 或 str），未配置/脏数据回退默认集字符串。"""
    if camp.ms_tags:
        try:
            tags = json.loads(camp.ms_tags)
            if isinstance(tags, list) and tags:
                out = []
                for t in tags[:20]:
                    if isinstance(t, dict) and str(t.get("name") or "").strip():
                        out.append({"name": str(t["name"]).strip(),
                                    "course_id": t.get("course_id")})
                    elif isinstance(t, str) and t.strip():
                        out.append(t.strip())
                if out:
                    return out
        except (ValueError, TypeError):
            pass
    return list(MS_DEFAULT_TAGS)


def _ms_tags_list(camp):
    """营级分类标签名数组（方向制后的统一读出口）：dict 取 name、str 原样。
    现有消费方（名片校验/市集过滤/phase/session dict）零改动。"""
    return [d["name"] if isinstance(d, dict) else d for d in _ms_directions_raw(camp)]


def _ms_directions(camp):
    """方向定义（继承与课程派生/管理端配置回显的唯一取用口）：[{name, course_id}]。
    legacy 纯字符串条目归一为 {name, course_id: None}（老营未补绑课程的过渡态，可见可补）。"""
    from models import CourseModel
    out = []
    for d in _ms_directions_raw(camp):
        name = d["name"] if isinstance(d, dict) else d
        cid = d.get("course_id") if isinstance(d, dict) else None
        cid = int(cid) if cid is not None and str(cid).isdigit() else None
        if cid is not None and not CourseModel.query.get(cid):
            cid = None
        out.append({"name": name, "course_id": cid})
    return out


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
    空字符串/None → 置 NULL。round 截止键已随单轮化废弃，传入一律忽略。
    ms_tags 接受两种形状（09-12 方向制）：[{name, course_id}] 对象数组（新）或纯字符串数组
    （legacy 容忍直存，_validate_ms 在 enabled 时会拦截缺课程的形状）。"""
    if "mentor_selection_enabled" in d:
        camp.mentor_selection_enabled = bool(d.get("mentor_selection_enabled"))
    for f in ("ms_preference_start", "ms_preference_deadline"):
        if f in d:
            v = d.get(f)
            setattr(camp, f, _parse_ms_dt(v) if v else None)
    if "ms_tags" in d:
        tags = d.get("ms_tags")
        if isinstance(tags, list):
            clean = []
            for t in tags:
                if isinstance(t, dict) and str(t.get("name") or "").strip():
                    cid = t.get("course_id")
                    cid = int(cid) if cid is not None and str(cid).isdigit() else None
                    clean.append({"name": str(t["name"]).strip(), "course_id": cid})
                elif isinstance(t, str) and t.strip():
                    clean.append(t.strip())
            camp.ms_tags = json.dumps(clean, ensure_ascii=False) if clean else None
        else:
            camp.ms_tags = None


def _validate_ms(camp):
    """选导生配置校验（create/update 存库前调用）。返回 None 或错误 message。
    09-12 时间统领拍板：选导生是营期的第一个阶段——志愿时间窗必须落在营期起止之内
    （营期开始日 00:00 ≤ 志愿开始 < 志愿截止 ≤ 营期结束日 23:59，不再压在营期开始之前）；
    且每个分类必须绑定一门存在的课程（方向制：分类=方向+课程）。
    round 截止字段已随单轮化废弃，不参与校验。关闭 enabled 随时允许。"""
    if not camp.mentor_selection_enabled:
        return None
    ps, pd_ = camp.ms_preference_start, camp.ms_preference_deadline
    if not (ps and pd_):
        return "启用选导生需设置志愿开始 / 志愿截止时间"
    if not ps < pd_:
        return "选导生时间需满足：志愿开始 < 志愿截止"
    camp_start = datetime.combine(camp.start_date, time(0, 0, 0))
    camp_end = datetime.combine(camp.end_date, time(23, 59, 59))
    if ps < camp_start:
        return f"志愿开始不得早于营期开始日（{camp.start_date.isoformat()}）——选导生是营期内的第一阶段"
    for label, v in (("志愿开始", ps), ("志愿截止", pd_)):
        if v and v > camp_end:
            return f"选导生{label}时间不得晚于营期结束日（{camp.end_date.isoformat()}）"
    dirs = _ms_directions(camp)
    if not dirs:
        return "启用选导生需至少配置一个分类方向"
    for d in dirs:
        if d["course_id"] is None:
            return f"分类「{d['name']}」未关联有效课程（方向制：每个分类必须绑定一门课程）"
    return None


def _ms_phase(camp, now=None):
    """选导生阶段（读时计算，不落库）：
      disabled  未启用（含"开了但没配全"的坏配置——overview 层给 config_error 告警）
      upcoming  未到开始（导生已可建名片）
      collecting 收志愿（学员提交/修改志愿，唯一提交窗口）
      done      截止后（老师导出 CSV → 线下协调 → 批量指派回填，指派即逐人通知）"""
    now = now or datetime.now()
    if not camp.mentor_selection_enabled:
        return MS_DISABLED
    ps, pd_ = camp.ms_preference_start, camp.ms_preference_deadline
    if not (ps and pd_):
        return MS_DISABLED
    if now < ps:
        return MS_UPCOMING
    if now < pd_:
        return MS_COLLECTING
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
        # 09-12 方向制：管理端配置回显用（含课程绑定；legacy 未绑为 null）
        "ms_directions": _ms_directions(camp),
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
    """导生名下学员数（live 链接口径：含 teacher 预分配的插班生）。"""
    return CampMember.query.filter_by(
        camp_session_id=camp_id, role='student', team_mentor_id=mentor_id).count()


def _inherit_direction_course(camp, student_uid, mentor_uid):
    """方向制继承（09-12）：学员归属导生 → 自动入读该导生方向绑定的课程。
    - 方向取导生名片 tags[0] → _ms_directions 的 course_id；无名片/legacy 无课程 → 静默跳过
    - UserCourse UQ(user, course)：已有行（同课跨营）复用并重打 camp_session_id 戳（同旧 /camp/selection 口径）
    - 不 commit（由调用方事务一并提交）；release/移除导生不回收旧课行（学习历史保留）
    """
    profile = CampMentorProfile.query.filter_by(
        camp_session_id=camp.id, user_id=mentor_uid).first()
    if not profile or not profile.tags:
        return None
    try:
        tags = json.loads(profile.tags)
    except (ValueError, TypeError):
        return None
    if not (isinstance(tags, list) and tags):
        return None
    direction = next((d for d in _ms_directions(camp)
                      if d["name"] == str(tags[0])), None)
    if not direction or direction["course_id"] is None:
        return None
    uc = UserCourseModel.query.filter_by(
        user_id=student_uid, course_id=direction["course_id"]).first()
    if uc:
        uc.camp_session_id = camp.id
        return uc
    uc = UserCourseModel(user_id=student_uid, course_id=direction["course_id"],
                         camp_session_id=camp.id,
                         status=UserCourseModel.STATUS_ACTIVE)
    db.session.add(uc)
    return uc


def _avatar_url(u):
    return f"/data/avatars/{u.avatar_url}" if u and u.avatar_url else None


def _photo_url(p):
    return f"/camp/ms/photo/{p.photo}" if p and p.photo else None


def _fmt_dt(v):
    return v.strftime("%Y-%m-%d %H:%M") if v else None


# ─────────────────────────────────────────────
# 阶段过渡通知（lazy 触发：读端点顺带检查；Redis SETNX 防并发重发）
# ─────────────────────────────────────────────

def _maybe_notify_transition(camp, phase):
    """读端点顺带调用。Redis 游标 ms:phase_last:{sid} 去重 + SETNX 锁；
    Redis 不可用 → 降级跳过（绝不裸发全营重复通知）。
    老师 session_update 动过 ms 配置会删游标（允许按新时间线重发）。"""
    if phase not in (MS_COLLECTING, MS_DONE):
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
    """按阶段向对应人群发通知（只 add 不 commit，与 _maybe_notify_transition 的 commit 同事务）。
    单轮化后截止（done）不再向学员/导生群发结果——配对结果由老师批量指派时逐人通知。"""
    name = camp.name
    common = dict(category='camp', source_type=MS_SOURCE_TYPE,
                  source_id=camp.id, camp_session_id=camp.id)

    if phase == MS_COLLECTING:
        students = [m.user_id for m in CampMember.query.filter_by(
            camp_session_id=camp.id, role='student').all()]
        for uid in students:
            create_notification(uid, "选导生开始",
                                f"「{name}」选导生开始，请在 {_fmt_dt(camp.ms_preference_deadline)} 前浏览导生名片并提交 1-3 个志愿。",
                                **common)
    elif phase == MS_DONE:
        # 截止提醒仅告知老师（Phase 1a 后全局「老师」即 super_admin；批量查询无法走
        # is_admin() 方法收口，按同口径过滤 role）
        staffs = UserModel.query.filter(UserModel.role == 'super_admin').all()
        for u in staffs:
            create_notification(u.id, "选导生：志愿已截止",
                                f"「{name}」学员志愿已截止，请导出志愿 CSV 完成线下协调，再批量指派导生。",
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
        # 单轮化：只有 collecting 一个提交窗口；round2 键保留但恒空（前端兼容）
        submittable = 1 if (not member.team_mentor_id and phase == MS_COLLECTING) else None
        me.update({
            "round1": _my_preferences(sid, user.id, 1),
            "round2": [],
            "submittable_round": submittable,
            "unmatched": member.team_mentor_id is None,
            "my_mentor": ({"user_id": my_mentor.id, "username": my_mentor.username}
                          if my_mentor else None),
        })
    elif member and member.role == 'mentor':
        profile = CampMentorProfile.query.filter_by(
            camp_session_id=sid, user_id=user.id).first()
        matched = _live_matched(sid, user.id)
        me.update({
            "has_profile": profile is not None,
            "profile": _profile_dict(profile) if profile else None,
            "profile_locked": phase not in (MS_UPCOMING, MS_COLLECTING),
            "matched_count": matched,
            "remaining": (0 if profile is None
                          else None if profile.capacity is None
                          else max(0, profile.capacity - matched)),
            "suitor_count": CampMentorPreference.query.filter_by(
                camp_session_id=sid, mentor_user_id=user.id, round=1).count(),
        })

    # 从众信号：已交志愿的去重学员数 / 营内学员总数（前端 collecting 期展示用）
    submitted = (db.session.query(CampMentorPreference.student_user_id)
                 .filter_by(camp_session_id=sid, round=1).distinct().count())
    student_total = CampMember.query.filter_by(
        camp_session_id=sid, role='student').count()

    return jsonify({"code": 200, "phase": phase,
                    "enabled": bool(camp.mentor_selection_enabled),
                    "config_error": bool(camp.mentor_selection_enabled and not (
                        camp.ms_preference_start and camp.ms_preference_deadline)),
                    "deadlines": {
                        "preference_start": _fmt_dt(camp.ms_preference_start),
                        "preference_deadline": _fmt_dt(camp.ms_preference_deadline),
                        "round1_deadline": _fmt_dt(camp.ms_round1_deadline),
                        "round2_deadline": _fmt_dt(camp.ms_round2_deadline),
                    },
                    "round2_enabled": False,
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
        # capacity None=不限（09-11 起）；remaining None 同义；full 仅有限名额才可能为真
        "capacity": p.capacity, "matched": matched,
        "remaining": None if p.capacity is None else max(0, p.capacity - matched),
        "full": p.capacity is not None and matched >= p.capacity,
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
    # 09-12 方向制：导生只能选一个分类方向（学员随导生继承方向与课程）
    if len(tags) != 1:
        return jsonify({"code": 400, "message": "请选择恰好 1 个分类方向"}), 400
    allowed = set(_ms_tags_list(camp))
    bad = [t for t in tags if t not in allowed]
    if bad:
        return jsonify({"code": 400, "message": f"标签不在营期可选范围: {', '.join(map(str, bad))}"}), 400
    try:
        # 名额缺省=不限（用户 2026-09-11 拍板，废除 2026-09-03 起的缺省补 8）：
        # 不传/传 null/空串 → capacity=None（不限）；传数字走 1-30 范围校验，0 仍拒（B-1 语义保留）
        raw_cap = d.get("capacity")
        capacity = None if raw_cap is None or raw_cap == "" else int(raw_cap)
    except (ValueError, TypeError):
        return jsonify({"code": 400, "message": "capacity 需为整数"}), 400
    if capacity is not None and not (1 <= capacity <= 30):
        return jsonify({"code": 400, "message": "capacity 需在 1-30 之间（不填为不限）"}), 400
    matched = _live_matched(sid, user.id)
    if capacity is not None and capacity < matched:
        return jsonify({"code": 400, "message": f"capacity 不能低于已分配人数（{matched}）"}), 400

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
    # 满员沉底、其余按剩余名额降序（外卖式"还有余量的店排前面"）；
    # capacity=NULL=不限（09-11 D-1）→ remaining=None 视作无限余量排最前，不可取负
    data.sort(key=lambda x: (x["full"], -(x["remaining"] if x["remaining"] is not None else float("inf"))))
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
                    "round2": []})


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
    if phase != MS_COLLECTING:
        return jsonify({"code": 403, "message": "当前不在志愿提交窗口"}), 403
    round_ = 1                      # 单轮制：round 恒为 1

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
        if not _member_row(sid, mid, role='mentor') or not CampMentorProfile.query.filter_by(
                camp_session_id=sid, user_id=mid).first():
            return jsonify({"code": 400, "message": "所选导师不在本营或未发布名片"}), 400
        items.append((mid, note))

    # 替换语义：整组删旧插新（截止前可反复改）
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
# 学员收藏（市集个人便签：不限数量、不参与配对/导出，仅供浏览整理。
# 前端契约（60b39fd）：GET → {mentor_ids}；PUT/DELETE → {mentor_id, favorited}）
# ─────────────────────────────────────────────

def _favorite_base_guard(sid):
    """收藏三端点共用的前置校验：营存在、启用选导生、未归档、当前用户是本营学员。"""
    camp, err = _camp_or_404(sid)
    if err:
        return None, None, err
    if not camp.mentor_selection_enabled:
        return None, None, (jsonify({"code": 400, "message": "该营期未启用选导生"}), 400)
    if not _camp_writable(camp):
        return None, None, (jsonify({"code": 400, "message": "营期已归档，只读"}), 400)
    user = _current_user()
    student = _member_row(sid, user.id, role='student')
    if not student:
        return None, None, (jsonify({"code": 403, "message": "仅本营学员可操作"}), 403)
    return camp, student, None


@bp.route("/<int:sid>/favorites")
@jwt_required()
def favorites_list(sid):
    _, student, err = _favorite_base_guard(sid)
    if err:
        return err
    rows = CampMentorFavorite.query.filter_by(
        camp_session_id=sid, student_user_id=student.user_id).all()
    return jsonify({"code": 200, "mentor_ids": sorted({r.mentor_user_id for r in rows})})


@bp.route("/<int:sid>/favorites/<int:mentor_id>", methods=["PUT"])
@jwt_required()
def favorite_add(sid, mentor_id):
    camp, student, err = _favorite_base_guard(sid)
    if err:
        return err
    if student.team_mentor_id:
        return jsonify({"code": 403, "message": "你已有归属导生，无需再收藏"}), 403
    if _ms_phase(camp) != MS_COLLECTING:
        return jsonify({"code": 403, "message": "当前不在收藏窗口（志愿收集期内可标记）"}), 403
    if not _member_row(sid, mentor_id, role='mentor') or not CampMentorProfile.query.filter_by(
            camp_session_id=sid, user_id=mentor_id).first():
        return jsonify({"code": 400, "message": "所选导师不在本营或未发布名片"}), 400

    exists = CampMentorFavorite.query.filter_by(
        camp_session_id=sid, student_user_id=student.user_id, mentor_user_id=mentor_id).first()
    if not exists:                      # 幂等：重复收藏直接回成功
        db.session.add(CampMentorFavorite(
            camp_session_id=sid, student_user_id=student.user_id, mentor_user_id=mentor_id))
        db.session.commit()
    return jsonify({"code": 200, "message": "已收藏", "mentor_id": mentor_id, "favorited": True})


@bp.route("/<int:sid>/favorites/<int:mentor_id>", methods=["DELETE"])
@jwt_required()
def favorite_remove(sid, mentor_id):
    _, student, err = _favorite_base_guard(sid)
    if err:
        return err
    CampMentorFavorite.query.filter_by(               # 幂等：不存在也回成功
        camp_session_id=sid, student_user_id=student.user_id, mentor_user_id=mentor_id).delete(
        synchronize_session=False)
    db.session.commit()
    return jsonify({"code": 200, "message": "已取消收藏", "mentor_id": mentor_id, "favorited": False})


# ─────────────────────────────────────────────
# 导生视角：谁报了我（纯只读名单；收人动作已随单轮化下线，协调由老师线下完成）
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

    rows = (CampMentorPreference.query
            .filter_by(camp_session_id=sid, mentor_user_id=user.id, round=1)
            .order_by(CampMentorPreference.rank, CampMentorPreference.created_at).all())
    # 标注当前归属（live 链接口径）：collecting 期名单仍在变（preview），done 后供参考
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
    return jsonify({"code": 200, "round": 1, "preview": phase == MS_COLLECTING,
                    "phase": phase,
                    "capacity": profile.capacity if profile else 0,
                    "matched": matched,
                    "remaining": (None if (profile and profile.capacity is None)
                                  else max(0, (profile.capacity if profile else 0) - matched)),
                    "suitors": data})


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
# 导生自助勾选（D-4：志愿截止后的协调期，导生在人员确认页自行勾选；
# 与老师批量指派共用 live 链接/账本写入路径，主路径仍是老师回填）
# ─────────────────────────────────────────────

def _pick_writable(camp):
    """勾选窗口 = 志愿截止后（done）。收集期内志愿仍在变，不开放；
    归档营只读由 _camp_writable 另行拦截。"""
    return _ms_phase(camp) == MS_DONE and _camp_writable(camp)


@bp.route("/<int:sid>/pick/roster")
@jwt_required()
def pick_roster(sid):
    """导生视角的勾选名单：本营全部学员 + 志愿信号（是否选我/志愿序/留言）+ 归属状态。
    status：mine=已在我名下（source 标 admin=老师指派不可释放 / mentor_pick=自助勾选）、
    taken=已属其他导生、free=未分配。排序：选了我的按志愿序在前，未选/未交居中，被占靠后。"""
    camp, err = _camp_or_404(sid)
    if err:
        return err
    if not camp.mentor_selection_enabled:
        return jsonify({"code": 400, "message": "该营期未启用选导生"}), 400
    user = _current_user()
    if not _member_row(sid, user.id, role='mentor'):
        return jsonify({"code": 403, "message": "仅本营导生可操作"}), 403

    phase = _ms_phase(camp)
    profile = CampMentorProfile.query.filter_by(camp_session_id=sid, user_id=user.id).first()
    cap = profile.capacity if profile else 0
    students = CampMember.query.filter_by(camp_session_id=sid, role='student').all()
    # 志愿信号：该学员 round1 里指向我的 rank/note
    prefs = {p.student_user_id: p for p in CampMentorPreference.query.filter_by(
        camp_session_id=sid, mentor_user_id=user.id, round=1).all()}
    submitted = {r[0] for r in db.session.query(CampMentorPreference.student_user_id)
                 .filter(CampMentorPreference.camp_session_id == sid,
                         CampMentorPreference.round == 1).all()}
    mentor_ids = {s.team_mentor_id for s in students if s.team_mentor_id}
    users = {u.id: u for u in UserModel.query.filter(UserModel.id.in_(
        [s.user_id for s in students] + list(mentor_ids))).all()} if students else {}
    ledger = {m.student_user_id: m for m in CampMentorMatch.query.filter_by(
        camp_session_id=sid, mentor_user_id=user.id).all()}

    data = []
    for s in students:
        u = users.get(s.user_id)
        p = prefs.get(s.user_id)
        led = ledger.get(s.user_id)
        mu = users.get(s.team_mentor_id) if s.team_mentor_id else None
        data.append({
            "user_id": s.user_id, "username": u.username if u else "",
            "avatar": _avatar_url(u),
            "rank": p.rank if p else None,
            "note": p.note if p else None,
            "submitted": s.user_id in submitted,
            "status": ("mine" if s.team_mentor_id == user.id
                       else "taken" if s.team_mentor_id else "free"),
            "mentor_name": mu.username if mu else None,
            "source": led.source if (s.team_mentor_id == user.id and led) else None,
        })
    data.sort(key=lambda x: (
        2 if x["status"] == "taken" else 0,          # 被占的沉底
        x["rank"] if x["rank"] else 9,               # 选了我的按志愿序
        0 if x["submitted"] else 1,                  # 交过志愿的靠前
        x["username"]))
    matched = _live_matched(sid, user.id)
    return jsonify({"code": 200, "phase": phase, "writable": _pick_writable(camp),
                    "capacity": cap, "matched": matched,
                    "remaining": None if cap is None else max(0, cap - matched),
                    "students": data})


@bp.route("/<int:sid>/pick", methods=["POST"])
@jwt_required()
@audit_log(operation="导生勾选学员")
def pick(sid):
    """导生自助勾选/释放：body {student_user_id, action: pick|release（默认 pick）}。
    仅 done（协调期）开放。pick 硬校验名额（导生无 allow_over 逃生门）与单归属，
    行锁防并发抢占；release 仅限自己勾选的（source=mentor_pick），老师指派走管理员改派。"""
    camp, err = _camp_or_404(sid)
    if err:
        return err
    if not camp.mentor_selection_enabled:
        return jsonify({"code": 400, "message": "该营期未启用选导生"}), 400
    if not _camp_writable(camp):
        return jsonify({"code": 400, "message": "营期已归档，只读"}), 400
    if not _pick_writable(camp):
        return jsonify({"code": 400, "message": "志愿收集期内不可勾选，截止后开放"}), 400
    user = _current_user()
    if not _member_row(sid, user.id, role='mentor'):
        return jsonify({"code": 403, "message": "仅本营导生可操作"}), 403
    d = request.json or {}
    student_id = d.get("student_user_id")
    action = d.get("action") or "pick"
    if not student_id or action not in ("pick", "release"):
        return jsonify({"code": 400, "message": "缺少 student_user_id 或 action 非法"}), 400

    # 行锁串行化：两名导生同时勾同一学员时后到者看到最新归属
    student = (CampMember.query.filter_by(
        camp_session_id=sid, user_id=student_id, role='student')
        .with_for_update().first())
    if not student:
        return jsonify({"code": 404, "message": "学员不在本营"}), 404
    su = UserModel.query.get(student_id)
    su_name = su.username if su else "该学员"

    if action == "pick":
        if student.team_mentor_id == user.id:
            return jsonify({"code": 400, "message": "该学员已在你的名下"}), 400
        if student.team_mentor_id:
            other = UserModel.query.get(student.team_mentor_id)
            return jsonify({"code": 409, "message": f"{su_name} 已被导生 "
                            f"{other.username if other else student.team_mentor_id} 锁定"}), 409
        profile = CampMentorProfile.query.filter_by(
            camp_session_id=sid, user_id=user.id).first()
        if not profile:
            return jsonify({"code": 409, "message": "你未发布名片，无法勾选学员"}), 409
        # 名额 None=不限（09-11），不限时不设满额门槛
        if profile.capacity is not None and _live_matched(sid, user.id) >= profile.capacity:
            return jsonify({"code": 409,
                            "message": f"你的名额已满（{profile.capacity}），如需增加请联系老师"}), 409
        row = CampMentorMatch.query.filter_by(
            camp_session_id=sid, student_user_id=student_id).first()
        if row:
            row.mentor_user_id = user.id
            row.round = None
            row.source = 'mentor_pick'
        else:
            db.session.add(CampMentorMatch(camp_session_id=sid, mentor_user_id=user.id,
                                           student_user_id=student_id, round=None,
                                           source='mentor_pick'))
        student.team_mentor_id = user.id
        _inherit_direction_course(camp, student_id, user.id)   # 方向制继承（09-12）
        create_notification(student_id, "选导生：导生已确认",
                            f"「{camp.name}」导生 {user.username} 已确认你加入其团队。",
                            category='camp', source_type=MS_SOURCE_TYPE,
                            source_id=camp.id, camp_session_id=sid, is_important=True)
        db.session.commit()
        return jsonify({"code": 200, "message": f"已锁定 {su_name}"})

    # release：仅自己勾选的可释放；老师指派/预分配的找管理员改派
    if student.team_mentor_id != user.id:
        return jsonify({"code": 400, "message": "该学员不在你的名下"}), 400
    row = CampMentorMatch.query.filter_by(
        camp_session_id=sid, student_user_id=student_id).first()
    if not row or row.source != 'mentor_pick':
        return jsonify({"code": 400, "message": "老师指派的学员请联系管理员改派"}), 400
    db.session.delete(row)
    student.team_mentor_id = None
    create_notification(student_id, "选导生：导生已释放",
                        f"「{camp.name}」导生 {user.username} 释放了你的归属，你暂未归属任何导生。",
                        category='camp', source_type=MS_SOURCE_TYPE,
                        source_id=camp.id, camp_session_id=sid)
    db.session.commit()
    return jsonify({"code": 200, "message": f"已释放 {su_name}"})


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
    # 单轮化：志愿只看 round==1；chose_r2 / submitted_r2 / r2_enabled 键保留但恒定（前端兼容）
    chose = {r[0]: r[1] for r in db.session.query(
        CampMentorPreference.mentor_user_id, func.count())
        .filter(CampMentorPreference.camp_session_id == sid,
                CampMentorPreference.round == 1)
        .group_by(CampMentorPreference.mentor_user_id).all()}
    submitted = {r[0] for r in db.session.query(CampMentorPreference.student_user_id)
                 .filter(CampMentorPreference.camp_session_id == sid,
                         CampMentorPreference.round == 1).all()}
    users = {u.id: u for u in UserModel.query.filter(UserModel.id.in_(
        [m.user_id for m in mentor_rows + student_rows])).all()}

    mentors = []
    for m in mentor_rows:
        u = users.get(m.user_id)
        p = profiles.get(m.user_id)
        matched = _live_matched(sid, m.user_id)
        # 方向 = 名片 tags[0]（与 _inherit_direction_course 的继承口径一致；无名片/未选为 None）
        direction = None
        if p and p.tags:
            try:
                t = json.loads(p.tags)
                if isinstance(t, list) and t and str(t[0]).strip():
                    direction = str(t[0]).strip()
            except (ValueError, TypeError):
                pass
        mentors.append({
            "user_id": m.user_id, "username": u.username if u else "",
            "has_profile": p is not None,
            "direction": direction,
            "capacity": p.capacity if p else 0,
            "chose_r1": chose.get(m.user_id, 0),
            "chose_r2": 0,
            "matched": matched,
            "remaining": (None if (p and p.capacity is None)
                          else max(0, (p.capacity if p else 0) - matched)),
        })
    students = []
    for s in student_rows:
        u = users.get(s.user_id)
        mu = users.get(s.team_mentor_id) if s.team_mentor_id else None
        students.append({
            "user_id": s.user_id, "username": u.username if u else "",
            "matched": s.team_mentor_id is not None,
            "mentor_name": mu.username if mu else None,
            "submitted_r1": s.user_id in submitted,
            "submitted_r2": False,
        })
    matched_n = sum(1 for s in student_rows if s.team_mentor_id)
    return jsonify({"code": 200, "phase": phase,
                    "config_error": bool(not (camp.ms_preference_start
                                              and camp.ms_preference_deadline)),
                    "deadlines": {
                        "preference_start": _fmt_dt(camp.ms_preference_start),
                        "preference_deadline": _fmt_dt(camp.ms_preference_deadline),
                        "round1_deadline": _fmt_dt(camp.ms_round1_deadline),
                        "round2_deadline": _fmt_dt(camp.ms_round2_deadline),
                    },
                    "mentors": mentors, "students": students,
                    "stats": {"students": len(student_rows), "matched": matched_n,
                              "unmatched": len(student_rows) - matched_n,
                              "r2_enabled": False}})


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
    # 名额 None=不限（09-11）不设满额门槛；无名片仍按 0 拦（allow_over 可越过）
    if cap is not None and not d.get("allow_over") and _live_matched(sid, mentor_id) >= cap:
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
    _inherit_direction_course(camp, student_id, mentor_id)   # 方向制继承（09-12）
    create_notification(student_id, "选导生：导生已指派",
                        f"老师已将你指派给「{camp.name}」导生 {mu.username if mu else ''}。",
                        category='camp', source_type=MS_SOURCE_TYPE,
                        source_id=camp.id, camp_session_id=sid, is_important=True)
    db.session.commit()
    return jsonify({"code": 200, "message": "已指派"})


@bp.route("/<int:sid>/export")
@jwt_required()
@camp_role()
@audit_log(operation="导出选导生志愿")
def export_preferences(sid):
    """导出学员志愿 CSV（utf-8-sig 带 BOM，Excel 可直接打开）：老师线下协调用。
    每学员一行；志愿按 rank 1-3 填导生名（缺位留空），留言汇总取 rank1 的 note，
    当前已分配导生取 live 链接 team_mentor_id。只含本营 student 成员。"""
    camp, err = _camp_or_404(sid)
    if err:
        return err
    if not camp.mentor_selection_enabled:
        return jsonify({"code": 400, "message": "该营期未启用选导生"}), 400
    students = CampMember.query.filter_by(camp_session_id=sid, role='student').all()
    rows = (CampMentorPreference.query
            .filter_by(camp_session_id=sid, round=1)
            .order_by(CampMentorPreference.rank).all())
    prefs = {}
    for r in rows:
        prefs.setdefault(r.student_user_id, []).append(r)
    user_ids = {r.mentor_user_id for r in rows}
    for s in students:
        user_ids.add(s.user_id)
        if s.team_mentor_id:
            user_ids.add(s.team_mentor_id)
    users = {u.id: u for u in UserModel.query.filter(UserModel.id.in_(user_ids)).all()} \
        if user_ids else {}

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["学员ID", "学员姓名", "志愿1", "志愿2", "志愿3", "留言汇总", "当前已分配导生"])
    for s in students:
        su = users.get(s.user_id)
        plist = prefs.get(s.user_id, [])
        names = []
        for i in range(3):
            if i < len(plist):
                mu = users.get(plist[i].mentor_user_id)
                names.append(mu.username if mu else str(plist[i].mentor_user_id))
            else:
                names.append("")
        note = (plist[0].note or "") if plist else ""
        tm = users.get(s.team_mentor_id) if s.team_mentor_id else None
        w.writerow([s.user_id, su.username if su else "", *names, note,
                    tm.username if tm else ""])
    resp = Response(buf.getvalue().encode("utf-8-sig"), mimetype="text/csv")
    resp.headers["Content-Disposition"] = f"attachment; filename=camp_{sid}_preferences.csv"
    return resp


@bp.route("/<int:sid>/assign/batch", methods=["POST"])
@jwt_required()
@camp_role()
@audit_log(operation="批量指派导生")
def assign_batch(sid):
    """线下协调结果批量回填：body {pairs:[{student_user_id, mentor_user_id},...]}。
    逐项校验（学员 = 本营 student 且未分配；导师 = 本营 mentor），逐项独立提交，
    单项失败不影响其余：已分配给同一导生 → skipped；已分配给别的导生 → conflict。
    名额不校验（协调本就允许 teacher 决定是否满额）。"""
    camp, err = _camp_or_404(sid)
    if err:
        return err
    if not camp.mentor_selection_enabled:
        return jsonify({"code": 400, "message": "该营期未启用选导生"}), 400
    if not _camp_writable(camp):
        return jsonify({"code": 400, "message": "营期已归档，只读"}), 400
    pairs = (request.json or {}).get("pairs")
    if not isinstance(pairs, list):
        return jsonify({"code": 400, "message": "缺少 pairs 数组"}), 400

    results = []
    for pair in pairs:
        student_id = pair.get("student_user_id") if isinstance(pair, dict) else None
        mentor_id = pair.get("mentor_user_id") if isinstance(pair, dict) else None
        item = {"student_user_id": student_id}
        if not student_id or not mentor_id:
            results.append({**item, "status": "error",
                            "message": "缺少 student_user_id/mentor_user_id"})
            continue
        # 行锁串行化并发指派；账本 upsert + live 链接写法与单人 /assign 一致
        student = (CampMember.query.filter_by(
            camp_session_id=sid, user_id=student_id, role='student')
            .with_for_update().first())
        if not student:
            results.append({**item, "status": "error", "message": "学员不在本营"})
            continue
        mentor = _member_row(sid, mentor_id, role='mentor')
        if not mentor:
            results.append({**item, "status": "error", "message": "导师不在本营或非导生角色"})
            continue
        if student.team_mentor_id == mentor_id:
            results.append({**item, "status": "skipped", "message": "已分配给该导生，跳过"})
            continue
        if student.team_mentor_id:
            other = UserModel.query.get(student.team_mentor_id)
            results.append({**item, "status": "conflict",
                            "message": f"已分配给导生 {other.username if other else student.team_mentor_id}"})
            continue
        mu = UserModel.query.get(mentor_id)
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
        _inherit_direction_course(camp, student_id, mentor_id)   # 方向制继承（09-12）
        create_notification(student_id, "选导生：导生已指派",
                            f"老师已将你指派给「{camp.name}」导生 {mu.username if mu else ''}。",
                            category='camp', source_type=MS_SOURCE_TYPE,
                            source_id=camp.id, camp_session_id=sid, is_important=True)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            results.append({**item, "status": "error",
                            "message": "写入冲突（并发指派），请重试"})
            continue
        results.append({**item, "status": "assigned", "message": "已指派"})
    return jsonify({"code": 200, "results": results})


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

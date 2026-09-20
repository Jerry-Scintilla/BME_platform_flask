"""营期公告蓝图 — 通知方案 §7.3 / API §12.5（2026-09-20，migrate_43）。

公告 vs 通知（两种对象，不共表）：
- 公告：营期内持续展示的公共内容，可置顶/过期/按受众发布，撤下走 status=ended；
- 通知：一次性送达。发布时可选择 notify=true 同时向受众扇出一条站内通知
  （同事务，source_type='camp_announcement' → 深链营工作台）。

权限：发布/编辑/撤下 = camp_access('announcement.publish')（owner+teacher）；
阅读：本营成员按受众过滤（mentors/students 只见自己的 + all），staff 恒可见全部。
"""
from datetime import datetime

from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required

from exts import db
from models import CampSession, CampAnnouncement, CampMember, UserModel

from . import audit_log, _current_user
from .camp_staff import camp_access, camp_staff_row
from .notification import create_notification

bp = Blueprint("camp_announcement", __name__, url_prefix="/camp")

VALID_AUDIENCES = ('all', 'mentors', 'students')


def _camp_or_404(sid):
    camp = CampSession.query.get(sid)
    if not camp or camp.status == 'deleted':
        return None, (jsonify({"code": 404, "message": "营期不存在"}), 404)
    return camp, None


def _ann_dict(a, author_name=None):
    return {
        "id": a.id, "title": a.title, "content": a.content,
        "audience": a.audience, "is_pinned": bool(a.is_pinned),
        "status": a.status,
        "published_at": a.published_at.isoformat() if a.published_at else None,
        "expires_at": a.expires_at.isoformat() if a.expires_at else None,
        "created_by": a.created_by, "author_name": author_name,
    }


@bp.route("/sessions/<int:sid>/announcements")
@jwt_required()
def announcement_list(sid):
    """营期公告列表（受众过滤）：置顶在前、新发布在前。
    staff（owner/teacher）与超管恒见全部（含已撤下/未过期全集，供管理视角）；
    成员按 my_role 过滤 mentors/students/all；过期公告不展示（管理视角除外）。"""
    camp, err = _camp_or_404(sid)
    if err:
        return err
    user = _current_user()
    is_staff = user.is_admin() or camp_staff_row(sid, user.id) is not None
    now = datetime.now()
    q = CampAnnouncement.query.filter_by(camp_session_id=sid)
    if not is_staff:
        member = CampMember.query.filter_by(camp_session_id=sid, user_id=user.id).first()
        if not member:
            # 非成员（报名窗口内的旁观者）只看全员公告——招募期「开放报名」类公告的出口
            q = q.filter_by(audience='all', status='active')
        else:
            auds = ['all', 'mentors'] if member.role == 'mentor' else ['all', 'students']
            q = q.filter(CampAnnouncement.audience.in_(auds), CampAnnouncement.status == 'active')
        q = q.filter((CampAnnouncement.expires_at.is_(None))
                     | (CampAnnouncement.expires_at > now))
    rows = q.order_by(CampAnnouncement.is_pinned.desc(),
                      CampAnnouncement.published_at.desc()).all()
    author_ids = {a.created_by for a in rows}
    names = {u.id: u.username for u in UserModel.query.filter(
        UserModel.id.in_(author_ids))} if author_ids else {}
    return jsonify({"code": 200,
                    "announcements": [_ann_dict(a, names.get(a.created_by)) for a in rows]})


@bp.route("/sessions/<int:sid>/announcements", methods=["POST"])
@jwt_required()
@camp_access('announcement.publish')
@audit_log(operation="发布营期公告")
def announcement_create(sid):
    """发布公告（owner/teacher）。body: {title, content, audience?, is_pinned?,
    notify?}——notify=true 时同事务向受众扇出一条站内通知（方案 §7.3）。"""
    camp, err = _camp_or_404(sid)
    if err:
        return err
    if not camp.status or camp.status == 'deleted':
        return jsonify({"code": 400, "message": "营期不存在或已删除"}), 400
    d = request.json or {}
    title = (d.get("title") or "").strip()
    content = (d.get("content") or "").strip()
    if not title or not content:
        return jsonify({"code": 400, "message": "请填写公告标题与内容"}), 400
    if len(title) > 200:
        return jsonify({"code": 400, "message": "标题过长（≤200 字）"}), 400
    audience = d.get("audience") or 'all'
    if audience not in VALID_AUDIENCES:
        return jsonify({"code": 400, "message": "audience 仅支持 all/mentors/students"}), 400
    user = _current_user()
    a = CampAnnouncement(camp_session_id=sid, title=title, content=content,
                         audience=audience, is_pinned=bool(d.get("is_pinned")),
                         created_by=user.id)
    db.session.add(a)
    db.session.flush()
    if d.get("notify"):
        _fanout_announcement(camp, a, user)
    db.session.commit()
    return jsonify({"code": 200, "message": "已发布", "id": a.id})


def _fanout_announcement(camp, a, operator):
    """向公告受众扇出一条站内通知（只 add，随调用方事务）。
    受众快照取发布时点的成员（方案 §5.6：事件生成时保存受众口径）。"""
    q = CampMember.query.filter_by(camp_session_id=camp.id)
    if a.audience == 'mentors':
        q = q.filter_by(role='mentor')
    elif a.audience == 'students':
        q = q.filter_by(role='student')
    content = f"「{camp.name}」发布了公告「{a.title}」，点击进入营期查看。"
    for m in q.all():
        if m.user_id == operator.id:
            continue                     # 发布人自己不收（staff 双身份时仍见公告本体）
        create_notification(m.user_id, "营期公告", content,
                            category='camp', source_type='camp_announcement',
                            source_id=camp.id, camp_session_id=camp.id)


@bp.route("/sessions/<int:sid>/announcements/<int:aid>", methods=["PUT"])
@jwt_required()
@camp_access('announcement.publish')
@audit_log(operation="编辑营期公告")
def announcement_update(sid, aid):
    """编辑公告（部分更新；撤下状态可在此恢复）。body 键出现即改。"""
    a = CampAnnouncement.query.filter_by(id=aid, camp_session_id=sid).first()
    if not a:
        return jsonify({"code": 404, "message": "公告不存在"}), 404
    d = request.json or {}
    if "title" in d:
        title = (d.get("title") or "").strip()
        if not title or len(title) > 200:
            return jsonify({"code": 400, "message": "标题不能为空（≤200 字）"}), 400
        a.title = title
    if "content" in d:
        content = (d.get("content") or "").strip()
        if not content:
            return jsonify({"code": 400, "message": "内容不能为空"}), 400
        a.content = content
    if "audience" in d:
        if d["audience"] not in VALID_AUDIENCES:
            return jsonify({"code": 400, "message": "audience 仅支持 all/mentors/students"}), 400
        a.audience = d["audience"]
    if "is_pinned" in d:
        a.is_pinned = bool(d["is_pinned"])
    db.session.commit()
    return jsonify({"code": 200, "message": "已更新"})


@bp.route("/sessions/<int:sid>/announcements/<int:aid>/unpublish", methods=["POST"])
@jwt_required()
@camp_access('announcement.publish')
@audit_log(operation="撤下营期公告")
def announcement_unpublish(sid, aid):
    """撤下公告（status=ended，不物理删除——历史可追溯）。"""
    a = CampAnnouncement.query.filter_by(id=aid, camp_session_id=sid).first()
    if not a:
        return jsonify({"code": 404, "message": "公告不存在"}), 404
    if a.status != 'ended':
        a.status = 'ended'
        a.is_pinned = False
        db.session.commit()
    return jsonify({"code": 200, "message": "已撤下"})

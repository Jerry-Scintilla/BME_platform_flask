"""培训营·章节材料蓝图（2026-09-14，migrate_30）：学员按章提交材料（文字+附件），
提交即可见、追加式、无审核流——按章认证本身即验收，避免双流程；导生在团队成员页
查看下载，作为按章认证/评分的依据。

附件本体走 storage 层（STORAGE_BACKEND=minio|local，09-14 双后端），object_key 规则
camp/{sid}/chapter/{chapter_id}/{uuid}{ext}；不做方向硬校验（方向随导生名片可变，
材料归属学员本人，卡死会误伤改派场景）。
"""
import os
import uuid
from datetime import datetime

from flask import Blueprint, request, jsonify, Response
from flask_jwt_extended import jwt_required
from urllib.parse import quote

from exts import db
from storage import storage
from models import CampSession, CampMember, Chapter, CampChapterMaterial, CampChapterMaterialAttachment

from . import audit_log, _current_user
from .camp import _camp_writable, _in_my_team
from .camp_project import _camp_or_404
from .media_sign import media_token_response, resolve_media_request

bp = Blueprint("camp_material", __name__, url_prefix="/camp")

MAX_FILE_MB = 100    # 单文件上限（与项目营交付链同口径）


def _material_dict(m, atts):
    return {
        "id": m.id, "course_id": m.course_id, "chapter_id": m.chapter_id,
        "student_user_id": m.student_user_id, "content": m.content,
        "created_at": m.created_at.isoformat() if m.created_at else None,
        "attachments": [{"id": a.id, "filename": a.filename, "size": a.size} for a in atts],
    }


def _atts_by_material(material_ids):
    out = {mid: [] for mid in material_ids}
    if not material_ids:
        return out
    for a in CampChapterMaterialAttachment.query.filter(
            CampChapterMaterialAttachment.material_id.in_(material_ids)).all():
        out[a.material_id].append(a)
    return out


@bp.route("/sessions/<int:sid>/materials", methods=["POST"])
@jwt_required()
@audit_log(operation="提交章节材料")
def material_create(sid):
    """学员对本营某章节提交材料（multipart：chapter_id 必填 + content 选填 + Files[] 多文件，
    content/Files 至少其一）。追加式、无审核流，提交即对本团队导生与老师可见。"""
    camp, err = _camp_or_404(sid)
    if err:
        return err
    user = _current_user()
    member = CampMember.query.filter_by(camp_session_id=sid, user_id=user.id).first()
    if not user.is_admin() and (not member or member.role != 'student'):
        return jsonify({"code": 403, "message": "仅本营学员可提交材料"}), 403
    if not _camp_writable(camp):
        return jsonify({"code": 400, "message": "CAMP_ARCHIVED_READ_ONLY 营期已归档，只读"}), 400
    try:
        chapter_id = int(request.form.get("chapter_id") or "")
    except ValueError:
        return jsonify({"code": 400, "message": "chapter_id 须为整数"}), 400
    chapter = Chapter.query.get(chapter_id)
    if not chapter:
        return jsonify({"code": 404, "message": "章节不存在"}), 404
    content = (request.form.get("content") or "").strip() or None
    files = [f for f in request.files.getlist("Files") if f.filename]
    if not content and not files:
        return jsonify({"code": 400, "message": "请填写说明或上传附件"}), 400
    m = CampChapterMaterial(camp_session_id=sid, course_id=chapter.course_id,
                            chapter_id=chapter.id, student_user_id=user.id, content=content)
    db.session.add(m)
    db.session.flush()
    for f in files:
        ext = os.path.splitext(f.filename)[1].lower()[:20]
        key = f"camp/{sid}/chapter/{chapter.id}/{uuid.uuid4().hex}{ext}"
        try:
            storage.put_object(key, f.stream, content_type=f.mimetype or 'application/octet-stream')
            size = storage.stat_object(key).size
        except Exception:
            db.session.rollback()
            return jsonify({"code": 500, "message": "附件上传失败（存储服务不可用？），请稍后重试"}), 500
        if size > MAX_FILE_MB * 1024 * 1024:
            db.session.rollback()
            return jsonify({"code": 400, "message": f"附件 {f.filename} 超过 {MAX_FILE_MB}MB 上限"}), 400
        db.session.add(CampChapterMaterialAttachment(
            material_id=m.id, object_key=key, filename=f.filename[:200],
            size=size, content_type=f.mimetype))
    db.session.commit()
    atts = CampChapterMaterialAttachment.query.filter_by(material_id=m.id).all()
    return jsonify({"code": 200, "message": "材料已提交", "material": _material_dict(m, atts)})


@bp.route("/sessions/<int:sid>/materials")
@jwt_required()
def material_list(sid):
    """材料列表（按学员维度）：student_user_id 缺省=请求者本人；查他人须 admin 或本团队导生。
    chapter_id 可选过滤（导生端按章弹层）；响应不回 object_key（下载走代理端点）。"""
    camp, err = _camp_or_404(sid)
    if err:
        return err
    user = _current_user()
    raw_target = request.args.get("student_user_id")
    if raw_target is None:
        target_uid = user.id
    else:
        try:
            target_uid = int(raw_target)
        except ValueError:
            return jsonify({"code": 400, "message": "student_user_id 须为整数"}), 400
        if target_uid != user.id and not user.is_admin():
            member = CampMember.query.filter_by(camp_session_id=sid, user_id=user.id).first()
            if not member or member.role != 'mentor' or not _in_my_team(sid, user, target_uid):
                return jsonify({"code": 403, "message": "仅本团队学员的材料可查看"}), 403
    q = CampChapterMaterial.query.filter_by(camp_session_id=sid, student_user_id=target_uid)
    chapter_id = request.args.get("chapter_id")
    if chapter_id:
        try:
            q = q.filter_by(chapter_id=int(chapter_id))
        except ValueError:
            return jsonify({"code": 400, "message": "chapter_id 须为整数"}), 400
    rows = q.order_by(CampChapterMaterial.created_at, CampChapterMaterial.id).all()
    atts = _atts_by_material([m.id for m in rows])
    return jsonify({"code": 200, "materials": [_material_dict(m, atts[m.id]) for m in rows]})


def _material_or_404(mid):
    m = CampChapterMaterial.query.get(mid)
    if not m:
        return None, jsonify({"code": 404, "message": "材料不存在"}), 404
    return m, None, None


def _can_manage(user, m):
    """删/下载权限：材料本人、admin、本团队导生。"""
    if m.student_user_id == user.id or user.is_admin():
        return True
    member = CampMember.query.filter_by(
        camp_session_id=m.camp_session_id, user_id=user.id).first()
    return bool(member and member.role == 'mentor'
                and _in_my_team(m.camp_session_id, user, m.student_user_id))


@bp.route("/materials/<int:mid>", methods=["DELETE"])
@jwt_required()
@audit_log(operation="删除章节材料")
def material_delete(mid):
    """删材料（本人/本团队导生/admin）：附件对象幂等清理 + 行删除。"""
    m, err, code = _material_or_404(mid)
    if err:
        return err, code
    camp = CampSession.query.get(m.camp_session_id)
    user = _current_user()
    if not _can_manage(user, m):
        return jsonify({"code": 403, "message": "仅材料本人、本团队导生或老师可删除"}), 403
    if not _camp_writable(camp):
        return jsonify({"code": 400, "message": "CAMP_ARCHIVED_READ_ONLY 营期已归档，只读"}), 400
    for a in CampChapterMaterialAttachment.query.filter_by(material_id=m.id).all():
        storage.remove_object(a.object_key)    # 幂等，对象缺失静默
    db.session.delete(m)                        # 附件行随 FK CASCADE
    db.session.commit()
    return jsonify({"code": 200, "message": "已删除"})


@bp.route("/materials/attachments/<int:aid>/token")
@jwt_required()
def material_attachment_token(aid):
    """换取附件短签直连（2026-09-17 修旧链裸链 401：`<a target=_blank>` 带不了
    Authorization 头，前端点击下载时先来换签；权限同下载，2h 多次有效）。"""
    a = CampChapterMaterialAttachment.query.get(aid)
    if not a:
        return jsonify({"code": 404, "message": "附件不存在"}), 404
    m = CampChapterMaterial.query.get(a.material_id)
    if not m:
        return jsonify({"code": 404, "message": "材料不存在"}), 404
    user = _current_user()
    if not _can_manage(user, m):
        return jsonify({"code": 403, "message": "仅材料本人、本团队导生或老师可下载"}), 403
    return media_token_response('material', aid, user.id)


@bp.route("/materials/attachments/<int:aid>")
def material_attachment_download(aid):
    """附件代理下载（存储对象不暴露直链；权限同删除：本人/本团队导生/admin）。
    双通道鉴权：短签（/token 换取）或常规 JWT 均可，见 media_sign.py。"""
    a = CampChapterMaterialAttachment.query.get(aid)
    if not a:
        return jsonify({"code": 404, "message": "附件不存在"}), 404
    m = CampChapterMaterial.query.get(a.material_id)
    if not m:
        return jsonify({"code": 404, "message": "材料不存在"}), 404
    user, auth_err = resolve_media_request('material', aid)
    if auth_err:
        return auth_err
    if not _can_manage(user, m):
        return jsonify({"code": 403, "message": "仅材料本人、本团队导生或老师可下载"}), 403
    try:
        obj = storage.get_object(a.object_key)
    except Exception:
        return jsonify({"code": 500, "message": "附件读取失败（存储服务不可用？）"}), 500
    resp = Response(obj, mimetype=a.content_type or 'application/octet-stream')
    resp.headers["Content-Disposition"] = \
        f"attachment; filename*=UTF-8''{quote(a.filename)}"
    return resp

"""项目广场蓝图（功能扩展轮 §五 MVP，2026-09-12）。

全站项目展示/交流/数字资产板块，双来源：
- camp：营期项目「发布」投影——负责人/admin 显式动作（隐私与审核控制），从 ProjectProfile/
  里程碑/结营档案投影（标题/简介可覆盖），带溯源标；结营冻结时自动置 done + 挂 archive_ref。
- community：用户自发分享的轻量条目（名称/简介/标签/状态/资料区），免审上架+管理员下架。

红线（基本方案 §五）：① 展示不反向驱动营期流程；② community 条目不进营期组织约束；
③ 档案附件引用不复制。评论走 discussion 基建（scope_type='project'）。
"""
import json

from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required

from exts import db
from models import (
    UserModel, ShowcaseProject, ShowcaseFavorite,
    CampUnit, ProjectProfile, CampMember,
)

from . import audit_log, _current_user

bp = Blueprint("showcase", __name__, url_prefix="/showcase")

PROJECT_STATUS = ('idea', 'ongoing', 'done')
SOURCE_TEXT = {'camp': '营期项目', 'community': '自由分享'}
STATUS_TEXT = {'idea': '构思中', 'ongoing': '进行中', 'done': '已完成'}


def _p_dict(p, user=None, favorited=None):
    owner = UserModel.query.get(p.owner_user_id)
    out = {
        "id": p.id, "source": p.source, "source_text": SOURCE_TEXT.get(p.source, p.source),
        "source_ref": p.source_ref, "owner_user_id": p.owner_user_id,
        "owner_name": owner.username if owner else '',
        "title": p.title, "summary": p.summary, "description": p.description,
        "cover": p.cover, "tags": json.loads(p.tags) if p.tags else [],
        "project_status": p.project_status,
        "project_status_text": STATUS_TEXT.get(p.project_status, p.project_status),
        "status": p.status, "view_count": p.view_count,
        "members": json.loads(p.members_json) if p.members_json else [],
        "links": json.loads(p.links_json) if p.links_json else [],
        "archive_ref": p.archive_ref,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }
    if user is not None:
        out["favorited"] = bool(favorited)
        out["can_manage"] = user.is_admin() or p.owner_user_id == user.id
    # camp 溯源：营期名 + 周期
    if p.source == 'camp' and p.source_ref:
        unit = CampUnit.query.get(p.source_ref)
        if unit:
            from models import CampSession
            camp = CampSession.query.get(unit.camp_session_id)
            out["camp_name"] = camp.name if camp else None
            out["camp_cycle"] = camp.cycle.name if camp and camp.cycle else None
            out["camp_status"] = camp.status if camp else None
    return out


def _can_manage(p, user):
    return user.is_admin() or p.owner_user_id == user.id


# ─────────────────────────────────────────────
# 列表 / 详情
# ─────────────────────────────────────────────

@bp.route("/projects")
@jwt_required()
def project_list():
    """展示条目列表（?source=&project_status=&tag=&q=；hidden 条目仅 admin 可见）。
    数据量社团级，MVP 全量返回 + count；分页参数留扩展位。"""
    user = _current_user()
    q = ShowcaseProject.query
    if not user.is_admin():
        q = q.filter(ShowcaseProject.status == 'visible')
    src = request.args.get("source")
    if src in ('camp', 'community'):
        q = q.filter_by(source=src)
    ps = request.args.get("project_status")
    if ps in PROJECT_STATUS:
        q = q.filter_by(project_status=ps)
    tag = request.args.get("tag")
    if tag:
        q = q.filter(ShowcaseProject.tags.like(f'%"{tag}"%'))
    kw = (request.args.get("q") or "").strip()
    if kw:
        like = f"%{kw}%"
        q = q.filter(db.or_(ShowcaseProject.title.like(like),
                            ShowcaseProject.summary.like(like)))
    rows = q.order_by(ShowcaseProject.status, ShowcaseProject.updated_at.desc()).all()
    fav_ids = set()
    if rows:
        fav_ids = {f.project_id for f in ShowcaseFavorite.query.filter(
            ShowcaseFavorite.user_id == user.id,
            ShowcaseFavorite.project_id.in_([r.id for r in rows])).all()}
    return jsonify({"code": 200, "total": len(rows),
                    "projects": [_p_dict(r, user, r.id in fav_ids) for r in rows],
                    "all_tags": sorted({t for r in rows
                                        for t in (json.loads(r.tags) if r.tags else [])})})


@bp.route("/projects/<int:pid>")
@jwt_required()
def project_detail(pid):
    p = ShowcaseProject.query.get(pid)
    user = _current_user()
    if not p or (p.status == 'hidden' and not _can_manage(p, user)):
        return jsonify({"code": 404, "message": "项目不存在或已下架"}), 404
    p.view_count = (p.view_count or 0) + 1
    db.session.commit()
    fav = ShowcaseFavorite.query.filter_by(user_id=user.id, project_id=p.id).first()
    return jsonify({"code": 200, "project": _p_dict(p, user, fav is not None)})


# ─────────────────────────────────────────────
# community：自由分享（免审上架）
# ─────────────────────────────────────────────

@bp.route("/projects", methods=["POST"])
@jwt_required()
@audit_log(operation="分享项目到广场")
def project_create():
    user = _current_user()
    if user.is_admin():
        return jsonify({"code": 400, "message": "管理员用营期侧发布或让同学自行分享"}), 400
    d = request.json or {}
    title = (d.get("title") or "").strip()
    if not title or len(title) > 120:
        return jsonify({"code": 400, "message": "请填写项目名（120 字内）"}), 400
    tags = d.get("tags")
    if tags is not None and (not isinstance(tags, list) or len(tags) > 6
                             or any(not isinstance(t, str) or len(t) > 20 for t in tags)):
        return jsonify({"code": 400, "message": "tags 须为 ≤6 个、每个 ≤20 字的数组"}), 400
    links = d.get("links")
    if links is not None:
        if not isinstance(links, list) or len(links) > 10:
            return jsonify({"code": 400, "message": "links 须为 ≤10 项的数组"}), 400
        for l in links:
            if not isinstance(l, dict) or not (l.get("url") or "").strip():
                return jsonify({"code": 400, "message": "资料链接缺少 url"}), 400
    members = d.get("members")
    if members is not None and (not isinstance(members, list) or len(members) > 20
                                or any(not isinstance(m, str) or len(m) > 30 for m in members)):
        return jsonify({"code": 400, "message": "members 须为 ≤20 个名字的数组"}), 400
    p = ShowcaseProject(
        source='community', owner_user_id=user.id, title=title,
        summary=(d.get("summary") or "").strip()[:300] or None,
        description=d.get("description"),
        tags=json.dumps([t.strip() for t in tags if t.strip()], ensure_ascii=False) if tags else None,
        project_status=d.get("project_status") if d.get("project_status") in PROJECT_STATUS else 'ongoing',
        members_json=json.dumps(members, ensure_ascii=False) if members else None,
        links_json=json.dumps([{"label": (l.get("label") or l.get("url")).strip()[:50],
                                "url": l.get("url").strip()} for l in (links or [])],
                              ensure_ascii=False) if links else None)
    db.session.add(p)
    db.session.commit()
    return jsonify({"code": 200, "message": "已发布到项目广场（免审上架）",
                    "project": _p_dict(p, user, False)})


@bp.route("/projects/<int:pid>", methods=["PUT"])
@jwt_required()
@audit_log(operation="编辑广场项目")
def project_update(pid):
    """编辑（community 创建人 / camp 发布负责人 / admin；camp 条目编辑的是投影覆盖字段，
    不回写营期数据——展示与营期流程单向）。"""
    p = ShowcaseProject.query.get(pid)
    user = _current_user()
    if not p:
        return jsonify({"code": 404, "message": "项目不存在"}), 404
    if not _can_manage(p, user):
        return jsonify({"code": 403, "message": "仅创建人/发布人或管理员可编辑"}), 403
    d = request.json or {}
    if d.get("title"):
        p.title = d["title"].strip()[:120]
    if "summary" in d:
        p.summary = (d.get("summary") or "").strip()[:300] or None
    if "description" in d:
        p.description = d.get("description")
    if "cover" in d:
        p.cover = d.get("cover")
    if d.get("tags") is not None and isinstance(d.get("tags"), list):
        p.tags = json.dumps([t.strip() for t in d["tags"] if t.strip()], ensure_ascii=False)
    if d.get("project_status") in PROJECT_STATUS and p.source == 'community':
        p.project_status = d["project_status"]      # camp 条目状态随营期，手改仅 community
    if d.get("members") is not None and isinstance(d.get("members"), list):
        p.members_json = json.dumps(d["members"], ensure_ascii=False) or None
    if d.get("links") is not None and isinstance(d.get("links"), list):
        p.links_json = json.dumps([{"label": (l.get("label") or l.get("url")).strip()[:50],
                                    "url": (l.get("url") or "").strip()}
                                   for l in d["links"] if (l.get("url") or "").strip()],
                                  ensure_ascii=False) or None
    db.session.commit()
    return jsonify({"code": 200, "message": "已更新", "project": _p_dict(p, user)})


@bp.route("/projects/<int:pid>/status", methods=["PUT"])
@jwt_required()
@audit_log(operation="广场项目上下架")
def project_status(pid):
    """治理：admin 下架/恢复任意条目；创建人可自行下架（软删，可恢复）。"""
    p = ShowcaseProject.query.get(pid)
    user = _current_user()
    if not p:
        return jsonify({"code": 404, "message": "项目不存在"}), 404
    if not _can_manage(p, user):
        return jsonify({"code": 403, "message": "仅创建人/发布人或管理员可操作"}), 403
    st = (request.json or {}).get("status")
    if st not in ('visible', 'hidden'):
        return jsonify({"code": 400, "message": "status 仅支持 visible/hidden"}), 400
    p.status = st
    db.session.commit()
    return jsonify({"code": 200, "message": "已上架" if st == 'visible' else "已下架"})


# ─────────────────────────────────────────────
# camp：营期项目发布（负责人/admin 显式动作）
# ─────────────────────────────────────────────

@bp.route("/projects/publish-camp", methods=["POST"])
@jwt_required()
@audit_log(operation="发布营期项目到广场")
def publish_from_camp():
    """发布营期项目到项目广场：body {unit_id, summary?, description?, tags?, links?}。
    投影自 ProjectProfile（标题/背景/目标），字段可覆盖；发布即已审（申报审核前置）。
    幂等边界：一个营期项目只发一条（UQ）；重复发布 409 提示改走编辑。"""
    user = _current_user()
    d = request.json or {}
    unit = CampUnit.query.filter_by(id=d.get("unit_id"), unit_type='project').first()
    if not unit:
        return jsonify({"code": 404, "message": "项目单元不存在"}), 404
    if not user.is_admin() and unit.owner_user_id != user.id:
        return jsonify({"code": 403, "message": "仅项目负责人或管理员可发布"}), 403
    if ShowcaseProject.query.filter_by(source='camp', source_ref=unit.id).first():
        return jsonify({"code": 409, "message": "该项目已发布过（编辑请到项目广场详情页）"}), 409
    from models import CampSession
    camp = CampSession.query.get(unit.camp_session_id)
    profile = ProjectProfile.query.filter_by(unit_id=unit.id).first()
    desc = (d.get("description") or "").strip() or "、".join(
        x for x in [profile.background, profile.goal, profile.recruit_note] if x) or None
    p = ShowcaseProject(
        source='camp', source_ref=unit.id, owner_user_id=unit.owner_user_id,
        title=(d.get("title") or unit.name).strip()[:120],
        summary=(d.get("summary") or "").strip()[:300] or (profile.goal or '')[:300] or None,
        description=desc,
        tags=json.dumps([t.strip() for t in (d.get("tags") or []) if t.strip()], ensure_ascii=False) or None,
        project_status='done' if camp and camp.status == 'archived' else 'ongoing',
        links_json=json.dumps([{"label": (l.get("label") or l.get("url")).strip()[:50],
                                "url": (l.get("url") or "").strip()}
                               for l in (d.get("links") or []) if (l.get("url") or "").strip()],
                              ensure_ascii=False) or None)
    db.session.add(p)
    db.session.commit()
    return jsonify({"code": 200, "message": "已发布到项目广场", "project": _p_dict(p, user)})


# ─────────────────────────────────────────────
# 收藏（个人便签，同 CampMentorFavorite 模式）
# ─────────────────────────────────────────────

@bp.route("/projects/<int:pid>/favorite", methods=["PUT", "DELETE"])
@jwt_required()
def favorite(pid):
    p = ShowcaseProject.query.get(pid)
    if not p or p.status != 'visible':
        return jsonify({"code": 404, "message": "项目不存在或已下架"}), 404
    user = _current_user()
    row = ShowcaseFavorite.query.filter_by(user_id=user.id, project_id=pid).first()
    if request.method == 'PUT':
        if not row:
            db.session.add(ShowcaseFavorite(user_id=user.id, project_id=pid))
        db.session.commit()
        return jsonify({"code": 200, "favorited": True})
    if row:
        db.session.delete(row)
    db.session.commit()
    return jsonify({"code": 200, "favorited": False})

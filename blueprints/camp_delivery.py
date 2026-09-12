"""项目营交付蓝图（营期升级阶段 4 / 设计方案 v1.3 §3.10-3.11）。

三层解耦的交付层与资产层：
- ProjectTemplate 管共性：负责人三起点创建（空白/平台默认/复制历史）→ 实例化 CampMilestone；
  结营冻结时模板 archived（可复制=资产回流），cloned_from 溯源。
- CampMilestone + CampSubmissionVersion 管交付：模板化多节点；节点级 submit_mode 双模式
  （team=负责人提交老师审 / member=成员各交负责人审，负责人自己份额交老师审——防自审红线）；
  退回重提=新版本（旧版 superseded）；附件走 MinIO。
- CampOutcome 成果核验：未核验不入档案。
- CampArchive 结营冻结（close 迁移自动触发，幂等）+ CampArchiveRevision 受控修正。

红线（v1.3 §3.10）：交付物不进课程域；里程碑可引用课程（软链）；审核绑定期望版本
（仅 status=submitted 的最新版可审，旧版自然不可达）。
"""
import json
import uuid
from datetime import date, datetime
from urllib.parse import quote

from flask import Blueprint, request, jsonify, Response
from flask_jwt_extended import jwt_required

from exts import db
from storage import storage
from models import (
    CampSession, CampMember, CampUnit, CampUnitMember,
    ProjectTemplate, ProjectTemplateNode,
    CampMilestone, CampSubmissionVersion, CampSubmissionAttachment,
    CampOutcome, CampArchive, CampArchiveRevision,
    CampMembershipEvent, UserModel,
)

from . import camp_role, audit_log, _current_user
from .camp import _camp_writable
from .camp_project import _camp_or_404, _unit_or_404, _is_unit_leader, _notify

bp = Blueprint("camp_delivery", __name__, url_prefix="/camp")

MAX_FILE_MB = 100
VALID_SUBMIT_MODES = ('team', 'member')


# ─────────────────────────────────────────────
# 辅助
# ─────────────────────────────────────────────

def _unit_role(unit, user):
    """请求者在单元内的身份：'leader' / 'member'（active）/ None。admin 恒 'leader' 视角（admin 分支单独判断）。"""
    if user.is_admin():
        return 'leader'
    row = CampUnitMember.query.filter_by(
        unit_id=unit.id, user_id=user.id, status='active').first()
    return row.role if row else None


def _active_unit_members(unit_id):
    return CampUnitMember.query.filter_by(unit_id=unit_id, status='active').all()


def _template_dict(t, nodes=None):
    return {
        "id": t.id, "name": t.name, "description": t.description,
        "scope": t.scope, "category": t.category,
        "camp_session_id": t.camp_session_id, "unit_id": t.unit_id,
        "cloned_from_id": t.cloned_from_id, "status": t.status,
        "nodes": [{"id": n.id, "sort_order": n.sort_order, "title": n.title,
                   "description": n.description, "deliverable_req": n.deliverable_req,
                   "material_note": n.material_note,
                   "recommended_course_ids": json.loads(n.recommended_course_ids) if n.recommended_course_ids else [],
                   "submit_mode": n.submit_mode}
                  for n in (nodes if nodes is not None else
                            ProjectTemplateNode.query.filter_by(template_id=t.id)
                            .order_by(ProjectTemplateNode.sort_order, ProjectTemplateNode.id).all())],
    }


def _norm_nodes(payload_nodes):
    """入参节点列表规范化：[{title, description?, deliverable_req?, material_note?, submit_mode?, recommended_course_ids?}]
    → (ok, nodes|message)。sort_order 按数组序生成。"""
    if payload_nodes is None:
        return True, []
    if not isinstance(payload_nodes, list) or len(payload_nodes) > 50:
        return False, "nodes 须为列表（最多 50 个节点）"
    out = []
    for i, n in enumerate(payload_nodes, 1):
        if not isinstance(n, dict) or not (n.get("title") or "").strip():
            return False, f"第 {i} 个节点缺少 title"
        mode = n.get("submit_mode") or 'team'
        if mode not in VALID_SUBMIT_MODES:
            return False, f"第 {i} 个节点 submit_mode 仅支持 team/member"
        cids = n.get("recommended_course_ids")
        if cids is not None and not isinstance(cids, list):
            return False, f"第 {i} 个节点 recommended_course_ids 须为数组"
        out.append({
            "sort_order": i,
            "title": n["title"].strip()[:100],
            "description": n.get("description"),
            "deliverable_req": n.get("deliverable_req"),
            "material_note": n.get("material_note"),
            "submit_mode": mode,
            "recommended_course_ids": json.dumps([int(c) for c in cids]) if cids else None,
        })
    return True, out


def _write_nodes(template_id, nodes):
    ProjectTemplateNode.query.filter_by(template_id=template_id).delete(synchronize_session=False)
    for n in nodes:
        db.session.add(ProjectTemplateNode(template_id=template_id, **n))


def _latest_chain(milestone_id, submitted_by):
    q = CampSubmissionVersion.query.filter_by(milestone_id=milestone_id, submitted_by=submitted_by)
    return q.order_by(CampSubmissionVersion.version.desc()).first()


def _recompute_milestone_status(m):
    """里程碑状态聚合：team=负责人链最新版；member=全员链最新版（全员 approved 才 approved）。"""
    if m.submit_mode == 'team':
        unit = CampUnit.query.get(m.unit_id)
        chain = _latest_chain(m.id, unit.owner_user_id) if unit else None
        m.status = chain.status if chain else 'open'
        return
    members = _active_unit_members(m.unit_id)
    statuses = []
    for mem in members:
        chain = _latest_chain(m.id, mem.user_id)
        statuses.append(chain.status if chain else 'none')
    if statuses and all(s == 'approved' for s in statuses):
        m.status = 'approved'
    elif 'submitted' in statuses:
        m.status = 'submitted'
    elif 'returned' in statuses:
        m.status = 'returned'
    else:
        m.status = 'open'


def _sub_dict(s, with_reviewer=True):
    return {
        "id": s.id, "milestone_id": s.milestone_id, "version": s.version,
        "submitted_by": s.submitted_by, "content": s.content, "status": s.status,
        "review_note": s.review_note, "reviewed_at": s.reviewed_at.isoformat() if s.reviewed_at else None,
        "attachments": [{"id": a.id, "filename": a.filename, "size": a.size, "is_asset": bool(a.is_asset)}
                        for a in CampSubmissionAttachment.query.filter_by(submission_id=s.id).all()],
        "created_at": s.created_at.isoformat() if s.created_at else None,
    }


def _ms_dict(m, viewer_chains=None, names=None):
    """viewer_chains：请求者可见的版本链（member=自己；leader/admin=全部，按 submitted_by 分组）"""
    names = names or {}
    return {
        "id": m.id, "unit_id": m.unit_id, "node_id": m.node_id,
        "title": m.title, "description": m.description, "requirement": m.requirement,
        "due_date": m.due_date.isoformat() if m.due_date else None,
        "order_no": m.order_no, "submit_mode": m.submit_mode, "status": m.status,
        "submissions": [{**_sub_dict(s),
                         "submitted_by_name": names.get(s.submitted_by, str(s.submitted_by))}
                        for s in (viewer_chains or [])],
    }


# ─────────────────────────────────────────────
# 平台默认模板（admin 维护；负责人选起点时可读 active 列表）
# ─────────────────────────────────────────────

@bp.route("/project-templates")
@jwt_required()
def platform_template_list():
    status = request.args.get("status", "active")
    q = ProjectTemplate.query.filter_by(scope='platform')
    if not (_current_user().is_admin() and request.args.get("all") == '1'):
        q = q.filter_by(status='active')
    else:
        q = q.filter(ProjectTemplate.status.in_(('active', 'archived')))
    rows = q.order_by(ProjectTemplate.category, ProjectTemplate.id).all()
    return jsonify({"code": 200, "templates": [_template_dict(t) for t in rows]})


@bp.route("/project-templates", methods=["POST"])
@jwt_required()
@camp_role()
@audit_log(operation="创建平台项目模板")
def platform_template_create():
    d = request.json or {}
    name = (d.get("name") or "").strip()
    if not name:
        return jsonify({"code": 400, "message": "缺少模板名"}), 400
    ok, nodes = _norm_nodes(d.get("nodes"))
    if not ok:
        return jsonify({"code": 400, "message": nodes}), 400
    t = ProjectTemplate(name=name[:100], description=d.get("description"),
                        scope='platform', category=(d.get("category") or None),
                        created_by=_current_user().id)
    db.session.add(t)
    db.session.flush()
    _write_nodes(t.id, nodes)
    db.session.commit()
    return jsonify({"code": 200, "message": "平台模板已创建", "template": _template_dict(t)})


@bp.route("/project-templates/<int:tid>", methods=["PUT"])
@jwt_required()
@camp_role()
@audit_log(operation="更新平台项目模板")
def platform_template_update(tid):
    t = ProjectTemplate.query.filter_by(id=tid, scope='platform').first()
    if not t:
        return jsonify({"code": 404, "message": "平台模板不存在"}), 404
    d = request.json or {}
    if d.get("name"):
        t.name = d["name"].strip()[:100]
    if "description" in d:
        t.description = d.get("description")
    if "category" in d:
        t.category = d.get("category")
    if d.get("status") in ('active', 'archived'):
        t.status = d["status"]
    if d.get("nodes") is not None:
        ok, nodes = _norm_nodes(d.get("nodes"))
        if not ok:
            return jsonify({"code": 400, "message": nodes}), 400
        _write_nodes(t.id, nodes)
    db.session.commit()
    return jsonify({"code": 200, "message": "已更新", "template": _template_dict(t)})


@bp.route("/sessions/<int:sid>/template-sources")
@jwt_required()
def template_sources(sid):
    """可复制模板源（三起点的 clone 半边）：已归档模板（往届，任意营）+ 本营其他项目模板。
    供负责人创建模板时选择；防越权——进行中他营模板不出现。"""
    camp, err = _camp_or_404(sid)
    if err:
        return err
    user = _current_user()
    if not user.is_admin() and not CampMember.query.filter_by(
            camp_session_id=sid, user_id=user.id).first():
        return jsonify({"code": 403, "message": "仅营期成员可查看模板源"}), 403
    q = ProjectTemplate.query.filter(ProjectTemplate.scope == 'unit').filter(
        db.or_(ProjectTemplate.status == 'archived', ProjectTemplate.camp_session_id == sid))
    out = []
    for t in q.order_by(ProjectTemplate.updated_at.desc()).limit(100).all():
        unit = CampUnit.query.get(t.unit_id) if t.unit_id else None
        if not unit:
            continue
        out.append({
            "template_id": t.id, "unit_id": t.unit_id, "name": t.name,
            "camp_name": camp.name if t.camp_session_id == sid else
            (CampSession.query.get(t.camp_session_id).name if t.camp_session_id else ''),
            "archived": t.status == 'archived',
            "cloned_from_id": t.cloned_from_id,
            "node_count": ProjectTemplateNode.query.filter_by(template_id=t.id).count(),
        })
    return jsonify({"code": 200, "sources": out})


# ─────────────────────────────────────────────
# 项目模板（unit）：三起点创建 + 实例化 / 编辑
# ─────────────────────────────────────────────

@bp.route("/units/<int:uid>/template")
@jwt_required()
def unit_template_get(uid):
    unit, camp, err = _unit_or_404(uid)
    if err:
        return err
    user = _current_user()
    if not _unit_role(unit, user):
        return jsonify({"code": 403, "message": "仅项目成员可查看模板"}), 403
    t = ProjectTemplate.query.filter_by(unit_id=unit.id).first()
    if not t:
        return jsonify({"code": 200, "template": None, "instantiated":
                        CampMilestone.query.filter_by(unit_id=unit.id).count() > 0})
    return jsonify({"code": 200, "template": _template_dict(t),
                    "instantiated": CampMilestone.query.filter_by(unit_id=unit.id).count() > 0})


@bp.route("/units/<int:uid>/template", methods=["POST"])
@jwt_required()
@audit_log(operation="创建项目模板")
def unit_template_create(uid):
    """负责人创建项目模板（三起点：blank 空白 / platform 平台默认 / clone 复制历史项目模板）。
    创建后自动实例化里程碑（仅当项目还没有里程碑时）。"""
    unit, camp, err = _unit_or_404(uid)
    if err:
        return err
    user = _current_user()
    if not _is_unit_leader(unit, user):
        return jsonify({"code": 403, "message": "UNIT_MANAGEMENT_FORBIDDEN 仅项目负责人可创建模板"}), 403
    if not _camp_writable(camp):
        return jsonify({"code": 400, "message": "CAMP_ARCHIVED_READ_ONLY 营期已归档，只读"}), 400
    if ProjectTemplate.query.filter_by(unit_id=unit.id).first():
        return jsonify({"code": 409, "message": "本项目已有模板（可编辑，不可重复创建）"}), 409
    d = request.json or {}
    mode = d.get("mode", 'blank')
    nodes, cloned_from = [], None
    if mode == 'platform':
        src = ProjectTemplate.query.filter_by(id=d.get("source_template_id"), scope='platform',
                                              status='active').first()
        if not src:
            return jsonify({"code": 400, "message": "平台模板不存在或已下线"}), 400
        cloned_from = src.id
        nodes = [{"sort_order": n.sort_order, "title": n.title, "description": n.description,
                  "deliverable_req": n.deliverable_req, "material_note": n.material_note,
                  "submit_mode": n.submit_mode, "recommended_course_ids": n.recommended_course_ids}
                 for n in ProjectTemplateNode.query.filter_by(template_id=src.id)
                 .order_by(ProjectTemplateNode.sort_order)]
    elif mode == 'clone':
        src_t = ProjectTemplate.query.filter_by(unit_id=d.get("source_unit_id")).first()
        # 资产回流：可复制=已归档模板（往届）或同营模板；防越权翻看未归档他营模板
        if not src_t or src_t.scope != 'unit' or not (
                src_t.status == 'archived' or src_t.camp_session_id == camp.id):
            return jsonify({"code": 400, "message": "可复制的模板须为已结题归档模板或本营项目模板"}), 400
        cloned_from = src_t.id
        nodes = [{"sort_order": n.sort_order, "title": n.title, "description": n.description,
                  "deliverable_req": n.deliverable_req, "material_note": n.material_note,
                  "submit_mode": n.submit_mode, "recommended_course_ids": n.recommended_course_ids}
                 for n in ProjectTemplateNode.query.filter_by(template_id=src_t.id)
                 .order_by(ProjectTemplateNode.sort_order)]
    elif mode != 'blank':
        return jsonify({"code": 400, "message": "mode 仅支持 blank/platform/clone"}), 400
    # blank（或起点之上）允许随请求覆盖节点
    if d.get("nodes") is not None:
        ok, nodes = _norm_nodes(d.get("nodes"))
        if not ok:
            return jsonify({"code": 400, "message": nodes}), 400
    t = ProjectTemplate(
        name=(d.get("name") or unit.name)[:100], description=d.get("description"),
        scope='unit', camp_session_id=camp.id, unit_id=unit.id,
        created_by=user.id, cloned_from_id=cloned_from)
    db.session.add(t)
    db.session.flush()
    _write_nodes(t.id, nodes)
    instantiated = 0
    if not CampMilestone.query.filter_by(unit_id=unit.id).count():
        for n in _template_dict(t)["nodes"]:
            db.session.add(CampMilestone(
                camp_session_id=camp.id, unit_id=unit.id, node_id=n["id"],
                title=n["title"], description=n["description"], requirement=n["deliverable_req"],
                order_no=n["sort_order"], submit_mode=n["submit_mode"]))
            instantiated += 1
    db.session.commit()
    return jsonify({"code": 200, "message": f"模板已创建，实例化 {instantiated} 个里程碑",
                    "template": _template_dict(t), "instantiated": instantiated})


@bp.route("/units/<int:uid>/template", methods=["PUT"])
@jwt_required()
@audit_log(operation="编辑项目模板")
def unit_template_update(uid):
    """编辑模板（施工图打磨——结题归档后他人复制的就是这份蓝图）。不回写已实例化的里程碑。"""
    unit, camp, err = _unit_or_404(uid)
    if err:
        return err
    user = _current_user()
    if not _is_unit_leader(unit, user):
        return jsonify({"code": 403, "message": "UNIT_MANAGEMENT_FORBIDDEN 仅项目负责人可编辑模板"}), 403
    if not _camp_writable(camp):
        return jsonify({"code": 400, "message": "CAMP_ARCHIVED_READ_ONLY 营期已归档，只读"}), 400
    t = ProjectTemplate.query.filter_by(unit_id=unit.id).first()
    if not t:
        return jsonify({"code": 404, "message": "本项目还没有模板"}), 404
    d = request.json or {}
    if d.get("name"):
        t.name = d["name"].strip()[:100]
    if "description" in d:
        t.description = d.get("description")
    if d.get("nodes") is not None:
        ok, nodes = _norm_nodes(d.get("nodes"))
        if not ok:
            return jsonify({"code": 400, "message": nodes}), 400
        _write_nodes(t.id, nodes)
    db.session.commit()
    return jsonify({"code": 200, "message": "模板已更新（不影响已实例化的里程碑）", "template": _template_dict(t)})


# ─────────────────────────────────────────────
# 里程碑 CRUD（负责人；实例化后可增删调时）
# ─────────────────────────────────────────────

@bp.route("/units/<int:uid>/milestones")
@jwt_required()
def milestone_list(uid):
    """里程碑列表（带请求者可见的版本链：member=自己；leader/admin=全部链）。"""
    unit, camp, err = _unit_or_404(uid)
    if err:
        return err
    user = _current_user()
    role = _unit_role(unit, user)
    if not role and not CampMember.query.filter_by(camp_session_id=camp.id, user_id=user.id).first():
        return jsonify({"code": 403, "message": "CAMP_PARTICIPANT_REQUIRED 仅项目成员/营期成员可查看"}), 403
    ms = CampMilestone.query.filter_by(unit_id=unit.id).order_by(
        CampMilestone.order_no, CampMilestone.id).all()
    chains_all = CampSubmissionVersion.query.filter(
        CampSubmissionVersion.milestone_id.in_([m.id for m in ms])).all() if ms else []
    names = _names({s.submitted_by for s in chains_all})
    out = []
    for m in ms:
        chains = [s for s in chains_all if s.milestone_id == m.id]
        chains.sort(key=lambda s: (s.submitted_by, s.version))
        # member（非负责人）只见自己的链；leader/admin 见全部
        visible = [s for s in chains if user.is_admin() or unit.owner_user_id == user.id
                   or s.submitted_by == user.id]
        out.append(_ms_dict(m, visible, names))
    return jsonify({"code": 200, "milestones": out,
                    "my_role": 'leader' if (user.is_admin() or unit.owner_user_id == user.id) else 'member'})


@bp.route("/units/<int:uid>/milestones", methods=["POST"])
@jwt_required()
@audit_log(operation="新增里程碑")
def milestone_create(uid):
    unit, camp, err = _unit_or_404(uid)
    if err:
        return err
    user = _current_user()
    if not _is_unit_leader(unit, user):
        return jsonify({"code": 403, "message": "UNIT_MANAGEMENT_FORBIDDEN 仅项目负责人可管理里程碑"}), 403
    if not _camp_writable(camp):
        return jsonify({"code": 400, "message": "CAMP_ARCHIVED_READ_ONLY 营期已归档，只读"}), 400
    if unit.status != 'active':
        return jsonify({"code": 400, "message": f"项目状态为 {unit.status}，不可管理里程碑"}), 400
    d = request.json or {}
    title = (d.get("title") or "").strip()
    if not title:
        return jsonify({"code": 400, "message": "缺少节点标题"}), 400
    mode = d.get("submit_mode") or 'team'
    if mode not in VALID_SUBMIT_MODES:
        return jsonify({"code": 400, "message": "submit_mode 仅支持 team/member"}), 400
    base = db.session.query(db.func.max(CampMilestone.order_no)).filter_by(unit_id=unit.id).scalar() or 0
    m = CampMilestone(camp_session_id=camp.id, unit_id=unit.id, title=title[:100],
                      description=d.get("description"), requirement=d.get("requirement"),
                      due_date=date.fromisoformat(d["due_date"]) if d.get("due_date") else None,
                      order_no=base + 1, submit_mode=mode)
    db.session.add(m)
    db.session.commit()
    return jsonify({"code": 200, "message": "里程碑已添加", "milestone": _ms_dict(m)})


@bp.route("/milestones/<int:mid>", methods=["PUT"])
@jwt_required()
@audit_log(operation="编辑里程碑")
def milestone_update(mid):
    m = CampMilestone.query.get(mid)
    if not m:
        return jsonify({"code": 404, "message": "里程碑不存在"}), 404
    unit, camp, err = _unit_or_404(m.unit_id)
    if err:
        return err
    user = _current_user()
    if not _is_unit_leader(unit, user):
        return jsonify({"code": 403, "message": "UNIT_MANAGEMENT_FORBIDDEN 仅项目负责人可管理里程碑"}), 403
    if not _camp_writable(camp):
        return jsonify({"code": 400, "message": "CAMP_ARCHIVED_READ_ONLY 营期已归档，只读"}), 400
    if unit.status != 'active':
        return jsonify({"code": 400, "message": f"项目状态为 {unit.status}，不可管理里程碑"}), 400
    d = request.json or {}
    has_submissions = CampSubmissionVersion.query.filter_by(milestone_id=m.id).count() > 0
    if d.get("title"):
        m.title = d["title"].strip()[:100]
    for k in ("description", "requirement"):
        if k in d:
            setattr(m, k, d.get(k))
    if d.get("due_date"):
        try:
            m.due_date = date.fromisoformat(d["due_date"])
        except ValueError:
            return jsonify({"code": 400, "message": "due_date 格式错误（YYYY-MM-DD）"}), 400
    if "due_date" in d and d.get("due_date") is None:
        m.due_date = None
    if "order_no" in d and isinstance(d.get("order_no"), int):
        m.order_no = d["order_no"]
    if d.get("submit_mode"):
        if d["submit_mode"] not in VALID_SUBMIT_MODES:
            return jsonify({"code": 400, "message": "submit_mode 仅支持 team/member"}), 400
        if has_submissions:
            return jsonify({"code": 400, "message": "已有提交，提交主体不可再改（防审核链混乱）"}), 400
        m.submit_mode = d["submit_mode"]
    db.session.commit()
    return jsonify({"code": 200, "message": "已更新", "milestone": _ms_dict(m)})


@bp.route("/milestones/<int:mid>", methods=["DELETE"])
@jwt_required()
@audit_log(operation="删除里程碑")
def milestone_delete(mid):
    m = CampMilestone.query.get(mid)
    if not m:
        return jsonify({"code": 404, "message": "里程碑不存在"}), 404
    unit, camp, err = _unit_or_404(m.unit_id)
    if err:
        return err
    user = _current_user()
    if not _is_unit_leader(unit, user):
        return jsonify({"code": 403, "message": "UNIT_MANAGEMENT_FORBIDDEN 仅项目负责人可管理里程碑"}), 403
    if not _camp_writable(camp):
        return jsonify({"code": 400, "message": "CAMP_ARCHIVED_READ_ONLY 营期已归档，只读"}), 400
    if CampSubmissionVersion.query.filter_by(milestone_id=m.id).count():
        return jsonify({"code": 400, "message": "已有提交记录的里程碑不可删（历史交付保留）"}), 400
    db.session.delete(m)
    db.session.commit()
    return jsonify({"code": 200, "message": "已删除"})


# ─────────────────────────────────────────────
# 提交与审核（版本化；双模式；防自审）
# ─────────────────────────────────────────────

@bp.route("/milestones/<int:mid>/submissions", methods=["POST"])
@jwt_required()
@audit_log(operation="提交里程碑材料")
def submission_create(mid):
    """版本化提交（multipart：content 文本 + Files[] 附件）。
    team 模式=负责人提交；member 模式=成员各自提交（负责人亦交自己的份额，由老师审）。
    退回重提=新版本，旧版自动 superseded。"""
    m = CampMilestone.query.get(mid)
    if not m:
        return jsonify({"code": 404, "message": "里程碑不存在"}), 404
    unit, camp, err = _unit_or_404(m.unit_id)
    if err:
        return err
    user = _current_user()
    if user.is_admin():
        return jsonify({"code": 400, "message": "管理员不提交材料（审核方）"}), 400
    role = _unit_role(unit, user)
    if not role:
        return jsonify({"code": 403, "message": "仅项目成员可提交"}), 403
    if not _camp_writable(camp):
        return jsonify({"code": 400, "message": "CAMP_ARCHIVED_READ_ONLY 营期已归档，只读"}), 400
    if unit.status != 'active':
        return jsonify({"code": 400, "message": f"项目状态为 {unit.status}，材料提交已冻结"}), 400
    if m.status == 'approved':
        return jsonify({"code": 400, "message": "该里程碑已验收通过，不再接受提交"}), 400
    if m.submit_mode == 'team' and role != 'leader':
        return jsonify({"code": 403, "message": "本节点为整队交付，由项目负责人统一提交"}), 403
    content = (request.form.get("content") or "").strip() or None
    files = [f for f in request.files.getlist("Files") if f.filename]
    if not content and not files:
        return jsonify({"code": 400, "message": "请填写说明或上传附件"}), 400
    prev = _latest_chain(m.id, user.id)
    version = (prev.version + 1) if prev else 1
    if prev and prev.status == 'submitted':
        prev.status = 'superseded'          # 重提覆盖待审版本
    s = CampSubmissionVersion(milestone_id=m.id, version=version,
                              submitted_by=user.id, content=content)
    db.session.add(s)
    db.session.flush()
    for f in files:
        import os as _os
        ext = _os.path.splitext(f.filename)[1].lower()[:20]
        key = f"camp/{camp.id}/milestone/{m.id}/v{version}/{uuid.uuid4().hex}{ext}"
        try:
            storage.put_object(key, f.stream, content_type=f.mimetype or 'application/octet-stream')
            size = storage.stat_object(key).size
        except Exception:
            db.session.rollback()
            return jsonify({"code": 500, "message": "附件上传失败（存储服务不可用？），请稍后重试"}), 500
        if size > MAX_FILE_MB * 1024 * 1024:
            db.session.rollback()
            return jsonify({"code": 400, "message": f"附件 {f.filename} 超过 {MAX_FILE_MB}MB 上限"}), 400
        db.session.add(CampSubmissionAttachment(
            submission_id=s.id, object_key=key, filename=f.filename[:200],
            size=size, content_type=f.mimetype))
    _recompute_milestone_status(m)
    db.session.commit()
    # 通知审核人（team=老师不逐个通知 admin，通知负责人以外成员省略；member 模式通知负责人审）
    if m.submit_mode == 'member' and role == 'member':
        _notify(unit.owner_user_id, "有材料待你审核",
                f"「{unit.name}」成员 {user.username} 提交了节点「{m.title}」第 {version} 版，请审核。",
                camp.id, source_id=m.id)
    return jsonify({"code": 200, "message": f"已提交（第 {version} 版）", "submission": _sub_dict(s)})


@bp.route("/milestones/<int:mid>/submissions")
@jwt_required()
def submission_list(mid):
    """版本链列表：负责人/admin 看全部链，成员看自己的链。"""
    m = CampMilestone.query.get(mid)
    if not m:
        return jsonify({"code": 404, "message": "里程碑不存在"}), 404
    unit, camp, err = _unit_or_404(m.unit_id)
    if err:
        return err
    user = _current_user()
    role = _unit_role(unit, user)
    if not role:
        return jsonify({"code": 403, "message": "仅项目成员可查看提交"}), 403
    chains = CampSubmissionVersion.query.filter_by(milestone_id=m.id).order_by(
        CampSubmissionVersion.submitted_by, CampSubmissionVersion.version).all()
    visible = [s for s in chains if user.is_admin() or unit.owner_user_id == user.id
               or s.submitted_by == user.id]
    names = _names({s.submitted_by for s in visible})
    data = [{**_sub_dict(s), "submitted_by_name": names.get(s.submitted_by, str(s.submitted_by))}
            for s in visible]
    return jsonify({"code": 200, "submissions": data})


def _names(ids):
    if not ids:
        return {}
    return {u.id: u.username for u in UserModel.query.filter(UserModel.id.in_(ids)).all()}


@bp.route("/submissions/<int:sid>/review", methods=["POST"])
@jwt_required()
@audit_log(operation="审核里程碑材料")
def submission_review(sid):
    """审核（绑定期望版本：仅 status=submitted 可审，superseded/已审自然不可达）。
    team 模式=老师(admin)审；member 模式=负责人审成员材料（负责人自己的份额由老师审）。"""
    s = CampSubmissionVersion.query.get(sid)
    if not s:
        return jsonify({"code": 404, "message": "提交不存在"}), 404
    m = CampMilestone.query.get(s.milestone_id)
    unit, camp, err = _unit_or_404(m.unit_id)
    if err:
        return err
    user = _current_user()
    if not _camp_writable(camp):
        return jsonify({"code": 400, "message": "CAMP_ARCHIVED_READ_ONLY 营期已归档，只读"}), 400
    # 审核人判定：负责人不可自审（红线）
    if s.submitted_by == unit.owner_user_id:
        if not user.is_admin():
            return jsonify({"code": 403, "message": "负责人提交的材料由老师审核（不可自审）"}), 403
    else:
        if not (user.is_admin() or unit.owner_user_id == user.id):
            return jsonify({"code": 403, "message": "仅负责人或管理员可审核"}), 403
    if s.status != 'submitted':
        return jsonify({"code": 400, "message": f"该版本不可审（当前状态 {s.status}；如系退回后重提请审最新版）"}), 400
    d = request.json or {}
    action = d.get("action")
    note = (d.get("note") or "").strip()
    if action == 'approve':
        s.status, s.reviewed_by, s.reviewed_at = 'approved', user.id, datetime.now()
        s.review_note = note or None
    elif action == 'return':
        if not note:
            return jsonify({"code": 400, "message": "退回须填写说明（提交人重提时可见）"}), 400
        s.status, s.reviewed_by, s.reviewed_at, s.review_note = 'returned', user.id, datetime.now(), note[:500]
    else:
        return jsonify({"code": 400, "message": "action 仅支持 approve/return"}), 400
    _recompute_milestone_status(m)
    db.session.commit()
    sub_name = _names({s.submitted_by}).get(s.submitted_by, '')
    tip = "已验收通过" if action == 'approve' else f"被退回：{note}"
    _notify(s.submitted_by, f"材料审核结果：{m.title}",
            f"你在「{unit.name}」节点「{m.title}」提交的第 {s.version} 版材料{tip}。",
            camp.id, source_id=m.id, important=(action == 'return'))
    return jsonify({"code": 200, "message": "已通过" if action == 'approve' else "已退回",
                    "milestone_status": m.status})


@bp.route("/submissions/attachments/<int:aid>")
@jwt_required()
def attachment_download(aid):
    """附件代理下载（MinIO 对象不暴露直链；仅项目成员/admin）。"""
    a = CampSubmissionAttachment.query.get(aid)
    if not a:
        return jsonify({"code": 404, "message": "附件不存在"}), 404
    s = CampSubmissionVersion.query.get(a.submission_id)
    m = CampMilestone.query.get(s.milestone_id)
    unit, camp, err = _unit_or_404(m.unit_id)
    if err:
        return err
    user = _current_user()
    if not _unit_role(unit, user):
        return jsonify({"code": 403, "message": "仅项目成员可下载附件"}), 403
    try:
        obj = storage.get_object(a.object_key)
    except Exception:
        return jsonify({"code": 500, "message": "附件读取失败（存储服务不可用？）"}), 500
    resp = Response(obj, mimetype=a.content_type or 'application/octet-stream')
    resp.headers["Content-Disposition"] = \
        f"attachment; filename*=UTF-8''{quote(a.filename)}"
    return resp


# ─────────────────────────────────────────────
# 成果（负责人登记 → admin 核验；未核验不入档案）
# ─────────────────────────────────────────────

@bp.route("/units/<int:uid>/outcomes")
@jwt_required()
def outcome_list(uid):
    unit, camp, err = _unit_or_404(uid)
    if err:
        return err
    user = _current_user()
    if not _unit_role(unit, user) and not CampMember.query.filter_by(
            camp_session_id=camp.id, user_id=user.id).first():
        return jsonify({"code": 403, "message": "仅项目/营期成员可查看成果"}), 403
    rows = CampOutcome.query.filter_by(unit_id=unit.id).order_by(CampOutcome.created_at).all()
    names = _names(set(sum([json.loads(r.contributor_ids or '[]') for r in rows], [])))
    return jsonify({"code": 200, "outcomes": [{
        "id": r.id, "title": r.title, "description": r.description,
        "contributor_ids": json.loads(r.contributor_ids or '[]'),
        "contributors": [names.get(i, str(i)) for i in json.loads(r.contributor_ids or '[]')],
        "status": r.status, "reject_reason": r.reject_reason,
        "created_at": r.created_at.isoformat() if r.created_at else None} for r in rows]})


@bp.route("/units/<int:uid>/outcomes", methods=["POST"])
@jwt_required()
@audit_log(operation="登记项目成果")
def outcome_create(uid):
    unit, camp, err = _unit_or_404(uid)
    if err:
        return err
    user = _current_user()
    if not _is_unit_leader(unit, user):
        return jsonify({"code": 403, "message": "UNIT_MANAGEMENT_FORBIDDEN 仅项目负责人可登记成果"}), 403
    if not _camp_writable(camp):
        return jsonify({"code": 400, "message": "CAMP_ARCHIVED_READ_ONLY 营期已归档，只读"}), 400
    d = request.json or {}
    title = (d.get("title") or "").strip()
    if not title:
        return jsonify({"code": 400, "message": "缺少成果标题"}), 400
    cids = d.get("contributor_ids")
    if cids is not None and not isinstance(cids, list):
        return jsonify({"code": 400, "message": "contributor_ids 须为数组"}), 400
    r = CampOutcome(unit_id=unit.id, title=title[:200], description=d.get("description"),
                    contributor_ids=json.dumps([int(c) for c in (cids or [])]),
                    submitted_by=user.id)
    db.session.add(r)
    db.session.commit()
    return jsonify({"code": 200, "message": "成果已登记，等待管理员核验", "outcome_id": r.id})


@bp.route("/outcomes/<int:oid>/verify", methods=["POST"])
@jwt_required()
@camp_role()
@audit_log(operation="核验项目成果")
def outcome_verify(oid):
    """admin 核验（verify/reject）——未核验不入个人档案与结营快照。"""
    r = CampOutcome.query.get(oid)
    if not r:
        return jsonify({"code": 404, "message": "成果不存在"}), 404
    unit, camp, err = _unit_or_404(r.unit_id)
    if err:
        return err
    if r.status == 'verified':
        return jsonify({"code": 400, "message": "该成果已核验"}), 400
    d = request.json or {}
    action = d.get("action")
    admin = _current_user()
    if action == 'verify':
        r.status, r.verified_by, r.verified_at = 'verified', admin.id, datetime.now()
        r.reject_reason = None
    elif action == 'reject':
        reason = (d.get("reason") or "").strip()
        if not reason:
            return jsonify({"code": 400, "message": "驳回须填写原因"}), 400
        r.status, r.reject_reason = 'rejected', reason[:500]
    else:
        return jsonify({"code": 400, "message": "action 仅支持 verify/reject"}), 400
    db.session.commit()
    _notify(unit.owner_user_id, "成果核验结果",
            f"「{unit.name}」成果「{r.title}」{'已核验通过' if action == 'verify' else '被驳回：' + r.reject_reason}。",
            camp.id, source_id=unit.id, important=(action == 'reject'))
    return jsonify({"code": 200, "message": "已核验" if action == 'verify' else "已驳回"})


@bp.route("/sessions/<int:sid>/delivery-admin")
@jwt_required()
@camp_role()
def delivery_admin(sid):
    """admin 交付工作台聚合：待老师审的材料队列（team 模式全部 + member 模式负责人份额）
    + 全营成果列表（核验入口）。老师审核动作走 /submissions/<id>/review 与 /outcomes/<id>/verify。"""
    camp, err = _camp_or_404(sid)
    if err:
        return err
    units = CampUnit.query.filter_by(camp_session_id=sid, unit_type='project').all()
    unit_map = {u.id: u for u in units}
    ms = (CampMilestone.query.filter(CampMilestone.unit_id.in_(list(unit_map)))
          .order_by(CampMilestone.unit_id, CampMilestone.order_no).all()) if units else []
    pending = []
    for m in ms:
        if m.submit_mode == 'member':
            continue        # member 模式成员材料由负责人在用户端审
        for s in CampSubmissionVersion.query.filter_by(
                milestone_id=m.id, status='submitted').all():
            pending.append({"submission_id": s.id, "milestone_id": m.id,
                            "milestone_title": m.title, "unit_id": m.unit_id,
                            "unit_name": unit_map[m.unit_id].name if m.unit_id in unit_map else '',
                            "version": s.version, "submitted_by": s.submitted_by,
                            "content": s.content,
                            "attachments": [{"id": a.id, "filename": a.filename} for a in
                                            CampSubmissionAttachment.query.filter_by(submission_id=s.id).all()],
                            "created_at": s.created_at.isoformat() if s.created_at else None})
    # member 模式中负责人份额（submitted_by==owner）也待老师审
    for m in ms:
        if m.submit_mode != 'member':
            continue
        u = unit_map.get(m.unit_id)
        if not u:
            continue
        for s in CampSubmissionVersion.query.filter_by(
                milestone_id=m.id, status='submitted', submitted_by=u.owner_user_id).all():
            pending.append({"submission_id": s.id, "milestone_id": m.id,
                            "milestone_title": m.title, "unit_id": m.unit_id,
                            "unit_name": u.name, "version": s.version,
                            "submitted_by": s.submitted_by, "content": s.content,
                            "attachments": [], "created_at": s.created_at.isoformat() if s.created_at else None})
    outcomes = []
    for u in units:
        for o in CampOutcome.query.filter_by(unit_id=u.id).order_by(CampOutcome.created_at).all():
            outcomes.append({"id": o.id, "unit_id": o.unit_id, "unit_name": u.name,
                             "title": o.title, "description": o.description,
                             "status": o.status, "reject_reason": o.reject_reason,
                             "created_at": o.created_at.isoformat() if o.created_at else None})
    names = _names({p["submitted_by"] for p in pending})
    for p in pending:
        p["submitted_by_name"] = names.get(p["submitted_by"], str(p["submitted_by"]))
    return jsonify({"code": 200, "pending_reviews": pending, "outcomes": outcomes,
                    "archived": camp.status == 'archived'})


# ─────────────────────────────────────────────
# 结营档案（冻结=close 迁移自动触发；此处读取+受控修正）
# ─────────────────────────────────────────────

def freeze_camp_archive(camp, operator_id):
    """幂等冻结结营档案（camp.py close 迁移调用；也可手动补触发）。返回 archive 或 None（已存在）。
    快照=关键事实 JSON；同时执行资产回流：unit 模板 status→archived（可复制），
    approved 提交的附件 is_asset→True。"""
    if CampArchive.query.filter_by(camp_session_id=camp.id).first():
        return None
    names = {}
    member_rows = CampMember.query.filter_by(camp_session_id=camp.id).all()
    for u in UserModel.query.filter(UserModel.id.in_(
            {m.user_id for m in member_rows})).all():
        names[u.id] = u.username
    units = CampUnit.query.filter_by(camp_session_id=camp.id, unit_type='project').all()
    unit_snapshots = []
    for unit in units:
        um_rows = CampUnitMember.query.filter_by(unit_id=unit.id).all()
        ms_rows = CampMilestone.query.filter_by(unit_id=unit.id).order_by(CampMilestone.order_no).all()
        ms_snap = []
        for m in ms_rows:
            finals = [s for s in CampSubmissionVersion.query.filter_by(milestone_id=m.id).all()
                      if s.status == 'approved']
            ms_snap.append({"title": m.title, "submit_mode": m.submit_mode,
                            "status": m.status, "due_date": m.due_date.isoformat() if m.due_date else None,
                            "approved_versions": [{"submitted_by": s.submitted_by,
                                                   "submitted_by_name": names.get(s.submitted_by, str(s.submitted_by)),
                                                   "version": s.version} for s in finals]})
        outcomes = [o for o in CampOutcome.query.filter_by(unit_id=unit.id).all()
                    if o.status == 'verified']
        tpl = ProjectTemplate.query.filter_by(unit_id=unit.id).first()
        if tpl:
            tpl.status = 'archived'                       # 资产回流：模板可被复制起步
        for s in CampSubmissionVersion.query.filter(
                CampSubmissionVersion.milestone_id.in_([m.id for m in ms_rows]),
                CampSubmissionVersion.status == 'approved').all():
            CampSubmissionAttachment.query.filter_by(submission_id=s.id).update(
                {CampSubmissionAttachment.is_asset: True})   # 资产回流：验收附件打标
        unit_snapshots.append({
            "name": unit.name, "status": unit.status,
            "leader": names.get(unit.owner_user_id, str(unit.owner_user_id)),
            "members": [{"user": names.get(r.user_id, str(r.user_id)), "role": r.role,
                         "status": r.status} for r in um_rows],
            "milestones": ms_snap,
            "verified_outcomes": [{"title": o.title, "description": o.description,
                                   "contributors": [names.get(i, str(i))
                                                    for i in json.loads(o.contributor_ids or '[]')]}
                                  for o in outcomes],
            "template": {"name": tpl.name, "cloned_from_id": tpl.cloned_from_id} if tpl else None,
        })
    snapshot = {
        "camp": {"id": camp.id, "name": camp.name, "category": camp.category,
                 "start_date": camp.start_date.isoformat(), "end_date": camp.end_date.isoformat()},
        "members": [{"user": names.get(m.user_id, str(m.user_id)), "role": m.role} for m in member_rows],
        "units": unit_snapshots,
        "event_count": CampMembershipEvent.query.filter_by(camp_session_id=camp.id).count(),
        "frozen_note": "close 迁移自动冻结；修正走 archive/revisions",
    }
    archive = CampArchive(camp_session_id=camp.id, snapshot=json.dumps(snapshot, ensure_ascii=False),
                          frozen_by=operator_id)
    db.session.add(archive)
    db.session.commit()
    # 项目广场联动（功能扩展轮 §五）：已发布的营期条目随结营自动标「已完成」并挂档案引用（引用不复制）
    from models import ShowcaseProject
    for sp in ShowcaseProject.query.filter(
            ShowcaseProject.source == 'camp',
            ShowcaseProject.source_ref.in_([u.id for u in units])).all():
        sp.project_status = 'done'
        sp.archive_ref = archive.id
    db.session.commit()
    return archive


@bp.route("/sessions/<int:sid>/archive")
@jwt_required()
def archive_get(sid):
    """读取结营档案（营期成员/admin；未冻结=404 提示先结营）。"""
    camp, err = _camp_or_404(sid)
    if err:
        return err
    user = _current_user()
    if not user.is_admin() and not CampMember.query.filter_by(
            camp_session_id=sid, user_id=user.id).first():
        return jsonify({"code": 403, "message": "仅营期成员可查看档案"}), 403
    a = CampArchive.query.filter_by(camp_session_id=sid).first()
    if not a:
        return jsonify({"code": 404, "message": "该营期尚未结营归档（无档案）"}), 404
    revisions = CampArchiveRevision.query.filter_by(archive_id=a.id).order_by(
        CampArchiveRevision.version).all()
    return jsonify({"code": 200,
                    "archive": {"id": a.id, "version": a.version,
                                "frozen_at": a.frozen_at.isoformat() if a.frozen_at else None,
                                "snapshot": json.loads(a.snapshot or '{}')},
                    "revisions": [{"version": r.version, "reason": r.reason,
                                   "patch": json.loads(r.patch) if r.patch else None,
                                   "created_at": r.created_at.isoformat() if r.created_at else None}
                                  for r in revisions]})


@bp.route("/sessions/<int:sid>/archive/revisions", methods=["POST"])
@jwt_required()
@camp_role()
@audit_log(operation="修正结营档案")
def archive_revise(sid):
    """受控修正：专门权限（admin）+原因必填+期望版本（expected_version）→版本递增。
    快照原文不可变——修正以 patch 描述留痕（展示层叠加）。"""
    camp, err = _camp_or_404(sid)
    if err:
        return err
    a = CampArchive.query.filter_by(camp_session_id=sid).first()
    if not a:
        return jsonify({"code": 404, "message": "该营期尚未结营归档（无档案可修正）"}), 404
    d = request.json or {}
    reason = (d.get("reason") or "").strip()
    patch = d.get("patch")
    if not reason:
        return jsonify({"code": 400, "message": "修正原因必填（留痕）"}), 400
    if not isinstance(d.get("expected_version"), int) or d["expected_version"] != a.version:
        return jsonify({"code": 409, "message": "SELECTION_VERSION_CONFLICT 档案版本已变化，请刷新后重试",
                        "current_version": a.version}), 409
    a.version += 1
    db.session.add(CampArchiveRevision(archive_id=a.id, version=a.version, reason=reason[:500],
                                       patch=json.dumps(patch, ensure_ascii=False) if patch else None,
                                       operator_id=_current_user().id))
    db.session.commit()
    return jsonify({"code": 200, "message": f"修正已登记（档案版本 {a.version}）", "version": a.version})

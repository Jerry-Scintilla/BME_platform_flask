"""
大模型（LiteLLM）服务蓝图

两类消费主体：
  1. 项目级：管理员创建项目（LiteLLM Team）+ 无限额 key，仅监控用量。
  2. 用户级：平台用户自建 key（挂在 LiteLLM internal user 上），有基础配额，
     可查用量、申请增额；管理员配置默认配额、审批、监控。
"""
import json
from datetime import datetime, timedelta

from flask import Blueprint, request, jsonify, current_app
from flask_jwt_extended import jwt_required, get_jwt_identity

from exts import db, redis_client
from models import (
    UserModel,
    LLMProjectModel,
    LLMUserKeyModel,
    LLMQuotaConfigModel,
    LLMQuotaRequestModel,
)
import litellm_client as llm
from litellm_client import LiteLLMError

from . import check_permission, audit_log

bp = Blueprint("llm", __name__, url_prefix="/llm")

# 管理员接口统一所需权限
LLM_PERMISSION = 'llm_management'


# ==================== 工具函数 ====================

def _current_user():
    """根据 JWT 解析当前用户"""
    email = get_jwt_identity()
    return UserModel.query.filter_by(email=email).first()


def _mask_key(key):
    """脱敏展示 key，仅保留前后片段"""
    if not key:
        return None
    if len(key) <= 12:
        return key[:3] + "****"
    return f"{key[:7]}...{key[-4:]}"


def _llm_error_response(e):
    """将 LiteLLMError 转为可读响应"""
    return jsonify({
        "code": 502,
        "message": f"大模型服务调用失败：{e.message}",
        "detail": e.detail,
    }), 502


def _get_quota_config():
    """获取（或初始化）默认配额配置单例"""
    config = LLMQuotaConfigModel.query.get(1)
    if not config:
        config = LLMQuotaConfigModel(
            id=1,
            default_max_budget=current_app.config.get("LITELLM_DEFAULT_MAX_BUDGET", 5.0),
            budget_duration=current_app.config.get("LITELLM_DEFAULT_BUDGET_DURATION", "30d"),
        )
        db.session.add(config)
        db.session.commit()
    return config


def _parse_duration_days(duration_str):
    """'30d'->30, '7d'->7, '1mo'->30；解析失败默认 30"""
    if not duration_str:
        return 30
    s = duration_str.strip().lower()
    try:
        if s.endswith('mo'):
            return int(s[:-2]) * 30
        if s.endswith('d'):
            return int(s[:-1])
    except ValueError:
        pass
    return 30


def _models_list(models_str):
    """逗号分隔字符串 -> 列表（空返回 None，表示不限制）"""
    if not models_str:
        return None
    return [m.strip() for m in models_str.split(",") if m.strip()]


# ---------- 用量查询的 Redis 短缓存 ----------
# LiteLLM 的 spend 是实时累加的，看板/用量接口高频读取会给 LiteLLM 的库带来压力，
# 这里加一层很短的缓存（默认 30s）削峰；额度变更处主动失效，保证及时性。
USAGE_CACHE_TTL = 60          # 用户/项目用量缓存秒数
GLOBAL_CACHE_TTL = 60         # 全局报表缓存秒数


def _cache_get(key):
    try:
        raw = redis_client.get(key)
        if raw:
            return json.loads(raw.decode('utf-8') if isinstance(raw, bytes) else raw)
    except Exception:
        pass
    return None


def _cache_set(key, value, ttl=USAGE_CACHE_TTL):
    try:
        redis_client.setex(key, ttl, json.dumps(value))
    except Exception:
        pass


def _cache_delete(key):
    try:
        redis_client.delete(key)
    except Exception:
        pass


def _invalidate_user_usage(user_id):
    """额度/用量发生变化后，清除该用户的用量缓存"""
    _cache_delete(f"llm:usage:user:{user_id}")


def _revert_expired_overrides(user_id):
    """检查并回滚已到期的临时增额，恢复至申请前的原始预算。
    若 LiteLLM 不可用则跳过，不标记已回滚，等下次触发重试。"""
    now = datetime.now()
    expired = LLMQuotaRequestModel.query.filter(
        LLMQuotaRequestModel.user_id == user_id,
        LLMQuotaRequestModel.status == LLMQuotaRequestModel.STATUS_APPROVED,
        LLMQuotaRequestModel.override_expires_at.isnot(None),
        LLMQuotaRequestModel.override_expires_at <= now,
        LLMQuotaRequestModel.reverted_at.is_(None),
    ).all()

    if not expired:
        return

    # 多条同时到期时取最小的基准预算，保证回到最保守的原始值
    revert_to = min(
        (r.current_budget for r in expired if r.current_budget is not None),
        default=0.0,
    )
    try:
        llm.update_user_budget(user_id, max_budget=revert_to)
        _invalidate_user_usage(user_id)
    except Exception:
        return

    for r in expired:
        r.reverted_at = now
    db.session.commit()


def _sum_spend_from_keys(user_id):
    """从各 key 的 /key/info 汇总 spend，用于 LiteLLM user record 不存在时的降级取数。"""
    keys = LLMUserKeyModel.query.filter_by(user_id=user_id, is_active=True).all()
    total_spend = 0.0
    has_any = False
    for k in keys:
        try:
            ki = llm.key_info(k.litellm_key)
            key_spend = (ki.get("info") or ki).get("spend")
            if key_spend is not None:
                total_spend += float(key_spend)
                has_any = True
        except Exception:
            pass
    return total_spend if has_any else None


def _reprovision_user(user_id):
    """在 LiteLLM user record 丢失时（如 LiteLLM DB 重置）重新 provision，
    预算从本地配置默认值恢复，不干扰已有的 key。"""
    try:
        config = LLMQuotaConfigModel.query.get(1)
        u = UserModel.query.get(user_id)
        llm.provision_user(
            user_id,
            user_email=u.email if u else None,
            max_budget=config.default_max_budget if config else None,
            budget_duration=config.budget_duration if config else None,
        )
        current_app.logger.info(f"[llm] re-provisioned user_id={user_id} in LiteLLM")
    except Exception as e:
        current_app.logger.warning(f"[llm] re-provision failed for user_id={user_id}: {e}")


def _safe_user_spend(user_id, use_cache=True):
    """安全获取某用户在 LiteLLM 的用量与预算，失败返回 None 字段。
    成功结果缓存 USAGE_CACHE_TTL 秒；失败不缓存，便于 LiteLLM 恢复后立即生效。"""
    cache_key = f"llm:usage:user:{user_id}"
    if use_cache:
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached
    try:
        info = llm.user_info(user_id)
        # user_info 键存在但值可能为 None（理论上不应出现，防御性处理）
        user_info_obj = info.get("user_info") if isinstance(info, dict) else None
        if not isinstance(user_info_obj, dict):
            current_app.logger.warning(
                f"[llm] user_info is null for user_id={user_id}, falling back to key-level spend sum."
            )
            spend = _sum_spend_from_keys(user_id)
            result = {"spend": spend, "max_budget": None, "budget_duration": None}
            _cache_set(cache_key, result)
            return result

        result = {
            "spend": user_info_obj.get("spend"),
            "max_budget": user_info_obj.get("max_budget"),
            "budget_duration": user_info_obj.get("budget_duration"),
        }
        _cache_set(cache_key, result)
        return result
    except LiteLLMError as e:
        if e.status_code == 404:
            # user 在 LiteLLM 侧不存在（如 LiteLLM DB 被重置）
            # 1. 异步 re-provision，让后续请求能正常聚合 spend
            _reprovision_user(user_id)
            # 2. 从各 key 取当前 spend 作为本次返回值
            spend = _sum_spend_from_keys(user_id)
            config = LLMQuotaConfigModel.query.get(1)
            result = {
                "spend": spend,
                "max_budget": config.default_max_budget if config else None,
                "budget_duration": config.budget_duration if config else None,
            }
            return result
        current_app.logger.warning(f"[llm] _safe_user_spend failed for user_id={user_id}: {e}")
        return {"spend": None, "max_budget": None, "budget_duration": None}
    except Exception as e:
        current_app.logger.warning(f"[llm] _safe_user_spend failed for user_id={user_id}: {e}")
        return {"spend": None, "max_budget": None, "budget_duration": None}


def _safe_project_usage(team_id, use_cache=True):
    """安全获取项目(team)的用量与预算，成功结果缓存。"""
    cache_key = f"llm:usage:team:{team_id}"
    if use_cache:
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached
    try:
        info = llm.team_info(team_id)
        ti = info.get("team_info", info) if isinstance(info, dict) else {}
        result = {"spend": ti.get("spend"), "max_budget": ti.get("max_budget")}
        _cache_set(cache_key, result)
        return result
    except LiteLLMError:
        return {"spend": None, "max_budget": None}


# ==================== 管理员：项目 ====================

@bp.route("/admin/projects", methods=["POST"])
@jwt_required()
@check_permission(LLM_PERMISSION)
@audit_log(operation="创建大模型项目")
def create_project():
    data = request.get_json() or {}
    name = (data.get("name") or "").strip()
    description = data.get("description")
    models = (data.get("models") or "").strip()

    if not name:
        return jsonify({"code": 400, "message": "缺少项目名 name"}), 400

    if LLMProjectModel.query.filter_by(name=name).first():
        return jsonify({"code": 400, "message": "项目名已存在"}), 400

    user = _current_user()
    models_list = _models_list(models)

    try:
        team = llm.new_team(team_alias=name, models=models_list)
        team_id = team.get("team_id")
        key_resp = llm.generate_key(key_alias=f"project-{name}", team_id=team_id, models=models_list)
        litellm_key = key_resp.get("key")
    except LiteLLMError as e:
        return _llm_error_response(e)

    project = LLMProjectModel(
        name=name,
        description=description,
        litellm_team_id=team_id,
        litellm_key=litellm_key,
        models=models or None,
        created_by=user.id if user else None,
    )
    db.session.add(project)
    db.session.commit()

    return jsonify({
        "code": 200,
        "message": "项目创建成功",
        "data": {
            "id": project.id,
            "name": project.name,
            "litellm_team_id": team_id,
            "litellm_key": litellm_key,  # 仅创建时明文返回一次
        }
    })


@bp.route("/admin/projects", methods=["GET"])
@jwt_required()
@check_permission(LLM_PERMISSION)
def list_projects():
    projects = LLMProjectModel.query.order_by(LLMProjectModel.created_at.desc()).all()
    result = []
    for p in projects:
        spend = None
        max_budget = None
        if p.litellm_team_id:
            usage = _safe_project_usage(p.litellm_team_id)
            spend = usage["spend"]
            max_budget = usage["max_budget"]
        result.append({
            "id": p.id,
            "name": p.name,
            "description": p.description,
            "models": p.models,
            "litellm_key": _mask_key(p.litellm_key),
            "litellm_team_id": p.litellm_team_id,
            "spend": spend,
            "max_budget": max_budget,
            "is_active": p.is_active,
            "created_at": p.created_at.strftime('%Y-%m-%d %H:%M:%S') if p.created_at else None,
        })
    return jsonify({"code": 200, "data": result})


@bp.route("/admin/projects/<int:project_id>", methods=["GET"])
@jwt_required()
@check_permission(LLM_PERMISSION)
def get_project(project_id):
    p = LLMProjectModel.query.get(project_id)
    if not p:
        return jsonify({"code": 404, "message": "项目不存在"}), 404

    usage = None
    if p.litellm_team_id:
        try:
            usage = llm.team_info(p.litellm_team_id)
        except LiteLLMError as e:
            usage = {"error": e.message}

    return jsonify({
        "code": 200,
        "data": {
            "id": p.id,
            "name": p.name,
            "description": p.description,
            "models": p.models,
            "litellm_key": _mask_key(p.litellm_key),
            "litellm_team_id": p.litellm_team_id,
            "is_active": p.is_active,
            "created_at": p.created_at.strftime('%Y-%m-%d %H:%M:%S') if p.created_at else None,
            "usage": usage,
        }
    })


@bp.route("/admin/projects/<int:project_id>/reveal-key", methods=["GET"])
@jwt_required()
@check_permission(LLM_PERMISSION)
def reveal_project_key(project_id):
    p = LLMProjectModel.query.get(project_id)
    if not p:
        return jsonify({"code": 404, "message": "项目不存在"}), 404

    return jsonify({"code": 200, "data": {"litellm_key": p.litellm_key}})


@bp.route("/admin/projects/<int:project_id>/regenerate-key", methods=["POST"])
@jwt_required()
@check_permission(LLM_PERMISSION)
@audit_log(operation="重置项目大模型Key")
def regenerate_project_key(project_id):
    p = LLMProjectModel.query.get(project_id)
    if not p:
        return jsonify({"code": 404, "message": "项目不存在"}), 404

    try:
        if p.litellm_key:
            try:
                llm.delete_key(p.litellm_key)
            except LiteLLMError:
                pass  # 旧 key 删除失败不阻断
        key_resp = llm.generate_key(
            key_alias=f"project-{p.name}",
            team_id=p.litellm_team_id,
            models=_models_list(p.models),
        )
        new_key = key_resp.get("key")
    except LiteLLMError as e:
        return _llm_error_response(e)

    p.litellm_key = new_key
    db.session.commit()
    return jsonify({
        "code": 200,
        "message": "Key 已重置",
        "data": {"litellm_key": new_key},  # 明文返回一次
    })


@bp.route("/admin/projects/<int:project_id>", methods=["DELETE"])
@jwt_required()
@check_permission(LLM_PERMISSION)
@audit_log(operation="删除大模型项目")
def delete_project(project_id):
    p = LLMProjectModel.query.get(project_id)
    if not p:
        return jsonify({"code": 404, "message": "项目不存在"}), 404

    # 尽力清理 LiteLLM 侧资源，失败不阻断本地删除
    try:
        if p.litellm_key:
            llm.delete_key(p.litellm_key)
    except LiteLLMError:
        pass
    try:
        if p.litellm_team_id:
            llm.delete_team(p.litellm_team_id)
    except LiteLLMError:
        pass

    db.session.delete(p)
    db.session.commit()
    return jsonify({"code": 200, "message": "项目已删除"})


# ==================== 管理员：默认配额配置 ====================

@bp.route("/admin/quota-config", methods=["GET"])
@jwt_required()
@check_permission(LLM_PERMISSION)
def get_quota_config():
    config = _get_quota_config()
    return jsonify({
        "code": 200,
        "data": {
            "default_max_budget": config.default_max_budget,
            "budget_duration": config.budget_duration,
            "allowed_models": config.allowed_models,
            "updated_at": config.updated_at.strftime('%Y-%m-%d %H:%M:%S') if config.updated_at else None,
        }
    })


@bp.route("/admin/quota-config", methods=["PUT"])
@jwt_required()
@check_permission(LLM_PERMISSION)
@audit_log(operation="更新大模型默认配额")
def update_quota_config():
    data = request.get_json() or {}
    config = _get_quota_config()
    user = _current_user()

    if "default_max_budget" in data:
        config.default_max_budget = float(data["default_max_budget"])
    if "budget_duration" in data:
        config.budget_duration = data["budget_duration"]
    if "allowed_models" in data:
        config.allowed_models = data["allowed_models"]
    config.updated_by = user.id if user else None
    db.session.commit()

    return jsonify({"code": 200, "message": "默认配额已更新"})


# ==================== 管理员：用户用量看板 ====================

@bp.route("/admin/users", methods=["GET"])
@jwt_required()
@check_permission(LLM_PERMISSION)
def list_user_usage():
    """列出已开通大模型服务（有 key）的平台用户及其用量/预算"""
    page = request.args.get('page', 1, type=int)
    per_page = min(request.args.get('per_page', 20, type=int), 100)

    # 仅统计拥有 key 的用户
    user_ids = [row.user_id for row in db.session.query(LLMUserKeyModel.user_id).distinct().all()]
    query = UserModel.query.filter(UserModel.id.in_(user_ids)) if user_ids else UserModel.query.filter(db.false())
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)

    result = []
    for u in pagination.items:
        usage = _safe_user_spend(u.id)
        key_count = LLMUserKeyModel.query.filter_by(user_id=u.id, is_active=True).count()
        result.append({
            "user_id": u.id,
            "username": u.username,
            "email": u.email,
            "key_count": key_count,
            "spend": usage["spend"],
            "max_budget": usage["max_budget"],
            "budget_duration": usage["budget_duration"],
        })

    return jsonify({
        "code": 200,
        "data": {
            "users": result,
            "pagination": {
                "page": pagination.page,
                "per_page": pagination.per_page,
                "total": pagination.total,
                "pages": pagination.pages,
            }
        }
    })


@bp.route("/admin/users/<int:user_id>", methods=["GET"])
@jwt_required()
@check_permission(LLM_PERMISSION)
def get_user_usage(user_id):
    u = UserModel.query.get(user_id)
    if not u:
        return jsonify({"code": 404, "message": "用户不存在"}), 404

    usage = _safe_user_spend(user_id)
    keys = LLMUserKeyModel.query.filter_by(user_id=user_id).all()
    keys_data = [{
        "id": k.id,
        "key_alias": k.key_alias,
        "litellm_key": _mask_key(k.litellm_key),
        "is_active": k.is_active,
        "created_at": k.created_at.strftime('%Y-%m-%d %H:%M:%S') if k.created_at else None,
    } for k in keys]

    return jsonify({
        "code": 200,
        "data": {
            "user_id": u.id,
            "username": u.username,
            "email": u.email,
            "usage": usage,
            "keys": keys_data,
        }
    })


@bp.route("/admin/users/<int:user_id>/quota", methods=["PUT"])
@jwt_required()
@check_permission(LLM_PERMISSION)
@audit_log(operation="调整用户大模型配额")
def set_user_quota(user_id):
    u = UserModel.query.get(user_id)
    if not u:
        return jsonify({"code": 404, "message": "用户不存在"}), 404

    data = request.get_json() or {}
    max_budget = data.get("max_budget")
    budget_duration = data.get("budget_duration")
    if max_budget is None:
        return jsonify({"code": 400, "message": "缺少 max_budget"}), 400

    try:
        llm.update_user_budget(user_id, max_budget=float(max_budget), budget_duration=budget_duration)
    except LiteLLMError as e:
        return _llm_error_response(e)

    _invalidate_user_usage(user_id)
    return jsonify({"code": 200, "message": "配额已更新"})


# ==================== 管理员：增额申请审批 ====================

@bp.route("/admin/quota-requests", methods=["GET"])
@jwt_required()
@check_permission(LLM_PERMISSION)
def list_quota_requests():
    status = request.args.get('status', type=str)
    page = request.args.get('page', 1, type=int)
    per_page = min(request.args.get('per_page', 20, type=int), 100)

    query = LLMQuotaRequestModel.query
    if status:
        query = query.filter_by(status=status)
    pagination = query.order_by(LLMQuotaRequestModel.created_at.desc()).paginate(
        page=page, per_page=per_page, error_out=False)

    result = []
    for r in pagination.items:
        applicant = UserModel.query.get(r.user_id)
        result.append({
            "id": r.id,
            "user_id": r.user_id,
            "username": applicant.username if applicant else None,
            "email": applicant.email if applicant else None,
            "current_budget": r.current_budget,
            "requested_budget": r.requested_budget,
            "reason": r.reason,
            "status": r.status,
            "review_comment": r.review_comment,
            "created_at": r.created_at.strftime('%Y-%m-%d %H:%M:%S') if r.created_at else None,
            "reviewed_at": r.reviewed_at.strftime('%Y-%m-%d %H:%M:%S') if r.reviewed_at else None,
            "override_expires_at": r.override_expires_at.strftime('%Y-%m-%d %H:%M:%S') if r.override_expires_at else None,
            "reverted_at": r.reverted_at.strftime('%Y-%m-%d %H:%M:%S') if r.reverted_at else None,
        })

    return jsonify({
        "code": 200,
        "data": {
            "requests": result,
            "pagination": {
                "page": pagination.page,
                "per_page": pagination.per_page,
                "total": pagination.total,
                "pages": pagination.pages,
            }
        }
    })


@bp.route("/admin/quota-requests/<int:request_id>/review", methods=["POST"])
@jwt_required()
@check_permission(LLM_PERMISSION)
@audit_log(operation="审批大模型增额申请")
def review_quota_request(request_id):
    r = LLMQuotaRequestModel.query.get(request_id)
    if not r:
        return jsonify({"code": 404, "message": "申请不存在"}), 404
    if r.status != LLMQuotaRequestModel.STATUS_PENDING:
        return jsonify({"code": 400, "message": "该申请已处理"}), 400

    data = request.get_json() or {}
    action = data.get("action")  # approve / reject
    comment = data.get("review_comment")
    reviewer = _current_user()

    if action not in ("approve", "reject"):
        return jsonify({"code": 400, "message": "action 必须为 approve 或 reject"}), 400

    if action == "approve":
        config = _get_quota_config()
        expires_days = _parse_duration_days(config.budget_duration)
        try:
            llm.update_user_budget(
                r.user_id,
                max_budget=float(r.requested_budget),
                budget_duration=config.budget_duration,  # 重置周期，单次有效
            )
        except LiteLLMError as e:
            return _llm_error_response(e)
        _invalidate_user_usage(r.user_id)
        r.status = LLMQuotaRequestModel.STATUS_APPROVED
        r.override_expires_at = datetime.now() + timedelta(days=expires_days)
    else:
        r.status = LLMQuotaRequestModel.STATUS_REJECTED

    r.review_comment = comment
    r.reviewed_by = reviewer.id if reviewer else None
    r.reviewed_at = datetime.now()
    db.session.commit()

    return jsonify({"code": 200, "message": "审批完成"})


# ==================== 管理员：全局看板 ====================

@bp.route("/admin/dashboard", methods=["GET"])
@jwt_required()
@check_permission(LLM_PERMISSION)
def admin_dashboard():
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')

    project_count = LLMProjectModel.query.count()
    active_user_count = db.session.query(LLMUserKeyModel.user_id).distinct().count()
    pending_requests = LLMQuotaRequestModel.query.filter_by(
        status=LLMQuotaRequestModel.STATUS_PENDING).count()

    cache_key = f"llm:usage:global:{start_date or ''}:{end_date or ''}"
    global_spend = _cache_get(cache_key)
    if global_spend is None:
        try:
            global_spend = llm.global_spend_report(start_date=start_date, end_date=end_date)
            _cache_set(cache_key, global_spend, ttl=GLOBAL_CACHE_TTL)
        except LiteLLMError as e:
            global_spend = {"error": e.message}

    return jsonify({
        "code": 200,
        "data": {
            "project_count": project_count,
            "active_user_count": active_user_count,
            "pending_requests": pending_requests,
            "global_spend": global_spend,
        }
    })


# ==================== 平台用户接口 ====================

@bp.route("/service-info", methods=["GET"])
@jwt_required()
def get_service_info():
    """返回 LiteLLM 服务接入信息：base_url、OpenAI /chat/completions、Claude API /v1/messages、可用模型列表"""
    base_url = current_app.config.get("LITELLM_BASE_URL", "").rstrip("/")
    chat_url = f"{base_url}/chat/completions"
    messages_url = f"{base_url}/v1/messages"

    config = _get_quota_config()
    if config.allowed_models:
        model_ids = _models_list(config.allowed_models) or []
    else:
        try:
            data = llm.list_models()
            model_ids = [m.get("id") for m in data.get("data", [])] if isinstance(data, dict) else []
        except LiteLLMError:
            model_ids = []

    models = [{"id": mid} for mid in model_ids if mid]

    return jsonify({
        "code": 200,
        "data": {
            "base_url": base_url,
            "chat_url": chat_url,
            "messages_url": messages_url,
            "models": models,
        }
    })


@bp.route("/models", methods=["GET"])
@jwt_required()
def get_available_models():
    config = _get_quota_config()
    # 优先返回管理员配置的允许模型；否则尝试从 LiteLLM 拉取
    if config.allowed_models:
        models = _models_list(config.allowed_models)
        return jsonify({"code": 200, "data": models})
    try:
        data = llm.list_models()
        models = [m.get("id") for m in data.get("data", [])] if isinstance(data, dict) else []
    except LiteLLMError:
        models = []
    return jsonify({"code": 200, "data": models})


@bp.route("/keys", methods=["POST"])
@jwt_required()
@audit_log(operation="创建个人大模型Key")
def create_user_key():
    user = _current_user()
    if not user:
        return jsonify({"code": 401, "message": "用户未认证"}), 401

    data = request.get_json() or {}
    key_alias = (data.get("key_alias") or f"{user.username}-key").strip()

    config = _get_quota_config()
    models_list = _models_list(config.allowed_models)

    try:
        existing_key_count = LLMUserKeyModel.query.filter_by(user_id=user.id).count()
        is_first_key = existing_key_count == 0
        if is_first_key:
            # 首次：创建 LiteLLM user 并设置默认预算
            llm.provision_user(
                user.id,
                user_email=user.email,
                max_budget=config.default_max_budget,
                budget_duration=config.budget_duration,
                models=models_list,
            )
        else:
            # 非首次：仅确保 LiteLLM user 记录存在（避免 LiteLLM 重置后 user 消失导致 usage 无法聚合）
            # 不传 max_budget，provision_user 在 user 已存在时 fallback 的 update_user_budget 会跳过预算字段
            llm.provision_user(user.id, user_email=user.email, models=models_list)
        key_resp = llm.generate_key(
            key_alias=key_alias,
            user_id=user.id,
            models=models_list,
        )
        litellm_key = key_resp.get("key")
    except LiteLLMError as e:
        return _llm_error_response(e)

    if is_first_key:
        _invalidate_user_usage(user.id)

    record = LLMUserKeyModel(user_id=user.id, key_alias=key_alias, litellm_key=litellm_key)
    db.session.add(record)
    db.session.commit()

    return jsonify({
        "code": 200,
        "message": "Key 创建成功",
        "data": {
            "id": record.id,
            "key_alias": key_alias,
            "litellm_key": litellm_key,  # 仅创建时明文返回一次
        }
    })


@bp.route("/keys", methods=["GET"])
@jwt_required()
def list_user_keys():
    user = _current_user()
    if not user:
        return jsonify({"code": 401, "message": "用户未认证"}), 401

    keys = LLMUserKeyModel.query.filter_by(user_id=user.id).order_by(
        LLMUserKeyModel.created_at.desc()).all()
    result = [{
        "id": k.id,
        "key_alias": k.key_alias,
        "litellm_key": _mask_key(k.litellm_key),
        "is_active": k.is_active,
        "created_at": k.created_at.strftime('%Y-%m-%d %H:%M:%S') if k.created_at else None,
    } for k in keys]
    return jsonify({"code": 200, "data": result})


@bp.route("/keys/<int:key_id>/reveal", methods=["GET"])
@jwt_required()
def reveal_user_key(key_id):
    user = _current_user()
    if not user:
        return jsonify({"code": 401, "message": "用户未认证"}), 401

    k = LLMUserKeyModel.query.filter_by(id=key_id, user_id=user.id).first()
    if not k:
        return jsonify({"code": 404, "message": "Key 不存在"}), 404

    return jsonify({"code": 200, "data": {"litellm_key": k.litellm_key}})


@bp.route("/keys/<int:key_id>", methods=["DELETE"])
@jwt_required()
@audit_log(operation="删除个人大模型Key")
def delete_user_key(key_id):
    user = _current_user()
    if not user:
        return jsonify({"code": 401, "message": "用户未认证"}), 401

    k = LLMUserKeyModel.query.filter_by(id=key_id, user_id=user.id).first()
    if not k:
        return jsonify({"code": 404, "message": "Key 不存在"}), 404

    try:
        llm.delete_key(k.litellm_key)
    except LiteLLMError:
        pass  # LiteLLM 侧删除失败不阻断本地删除

    db.session.delete(k)
    db.session.commit()
    return jsonify({"code": 200, "message": "Key 已删除"})


@bp.route("/usage", methods=["GET"])
@jwt_required()
def get_my_usage():
    user = _current_user()
    if not user:
        return jsonify({"code": 401, "message": "用户未认证"}), 401

    _revert_expired_overrides(user.id)
    force_refresh = request.args.get("refresh", "0") == "1"
    usage = _safe_user_spend(user.id, use_cache=not force_refresh)
    if force_refresh:
        print(f"[llm/usage refresh] user_id={user.id} result={usage}")
    spend = usage["spend"]
    max_budget = usage["max_budget"]
    remaining = None
    if spend is not None and max_budget is not None:
        remaining = round(max_budget - spend, 6)

    return jsonify({
        "code": 200,
        "data": {
            "spend": spend,
            "max_budget": max_budget,
            "remaining": remaining,
            "budget_duration": usage["budget_duration"],
        }
    })


@bp.route("/quota-requests", methods=["POST"])
@jwt_required()
@audit_log(operation="提交大模型增额申请")
def create_quota_request():
    user = _current_user()
    if not user:
        return jsonify({"code": 401, "message": "用户未认证"}), 401

    data = request.get_json() or {}
    requested_budget = data.get("requested_budget")
    reason = data.get("reason")
    if requested_budget is None:
        return jsonify({"code": 400, "message": "缺少 requested_budget"}), 400

    # 是否已有待审批申请
    existing = LLMQuotaRequestModel.query.filter_by(
        user_id=user.id, status=LLMQuotaRequestModel.STATUS_PENDING).first()
    if existing:
        return jsonify({"code": 400, "message": "已有待审批的申请，请勿重复提交"}), 400

    current_budget = _safe_user_spend(user.id)["max_budget"]

    req = LLMQuotaRequestModel(
        user_id=user.id,
        current_budget=current_budget,
        requested_budget=float(requested_budget),
        reason=reason,
    )
    db.session.add(req)
    db.session.commit()

    return jsonify({"code": 200, "message": "申请已提交，等待审批"})


ACTIVITY_CACHE_TTL = 120  # 活动趋势数据缓存 2 分钟
MODEL_ALIAS_CACHE_TTL = 300  # 模型别名映射缓存 5 分钟


def _get_model_alias_map():
    """获取 provider→alias 映射，带 Redis 缓存。失败时返回空字典。"""
    cache_key = "llm:model_alias_map"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    mapping = llm.get_model_alias_map()
    _cache_set(cache_key, mapping, ttl=MODEL_ALIAS_CACHE_TTL)
    return mapping


def _apply_model_aliases(results):
    """
    将 breakdown.models 中 provider-prefixed key（含 '/'）替换为 alias 名称。
    alias routing 条目（不含 '/'，spend/token 均为 0）直接丢弃，避免重复展示。
    若 alias 映射不存在，则保留 '/' 后的部分作为 display name。
    """
    alias_map = _get_model_alias_map()
    for day in (results or []):
        raw = (day.get("breakdown") or {}).get("models")
        if not raw:
            continue
        merged = {}
        for key, val in raw.items():
            if "/" not in key:
                # alias routing 条目：只有请求路由记录，spend/token 数据在 provider 条目中
                continue
            display = alias_map.get(key) or key.split("/", 1)[-1]
            if display not in merged:
                merged[display] = val
            else:
                # 同一 alias 被多个 provider 支撑时，合并 metrics
                base_m = merged[display].get("metrics") if isinstance(merged[display], dict) and "metrics" in merged[display] else merged[display]
                extra_m = val.get("metrics") if isinstance(val, dict) and "metrics" in val else val
                if isinstance(base_m, dict) and isinstance(extra_m, dict):
                    for field in ("spend", "total_tokens", "prompt_tokens", "completion_tokens",
                                  "api_requests", "successful_requests", "failed_requests",
                                  "cache_read_input_tokens", "cache_creation_input_tokens"):
                        base_m[field] = (base_m.get(field) or 0) + (extra_m.get(field) or 0)
        day["breakdown"]["models"] = merged
    return results


def _default_date_range(days=29):
    """返回 (start_date, end_date) 字符串，默认最近 days+1 天。"""
    end = datetime.now()
    start = end - timedelta(days=days)
    return start.strftime('%Y-%m-%d'), end.strftime('%Y-%m-%d')


# ==================== 管理员：活动趋势 ====================

@bp.route("/admin/users/<int:user_id>/activity", methods=["GET"])
@jwt_required()
@check_permission(LLM_PERMISSION)
def get_user_activity(user_id):
    """查询某用户按日活动趋势：spend / token / 请求量 / 模型分布。"""
    u = UserModel.query.get(user_id)
    if not u:
        return jsonify({"code": 404, "message": "用户不存在"}), 404
    default_start, default_end = _default_date_range()
    start_date = request.args.get('start_date', default_start)
    end_date = request.args.get('end_date', default_end)
    cache_key = f"llm:activity:user:{user_id}:{start_date}:{end_date}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return jsonify({"code": 200, "data": cached})
    try:
        data = llm.user_daily_activity(user_id, start_date=start_date, end_date=end_date)
        _apply_model_aliases(data.get("results") or data.get("data") or [])
        _cache_set(cache_key, data, ttl=ACTIVITY_CACHE_TTL)
        return jsonify({"code": 200, "data": data})
    except LiteLLMError as e:
        return _llm_error_response(e)


@bp.route("/admin/projects/<int:project_id>/activity", methods=["GET"])
@jwt_required()
@check_permission(LLM_PERMISSION)
def get_project_activity(project_id):
    """查询某项目（team）按日活动趋势。"""
    p = LLMProjectModel.query.get(project_id)
    if not p:
        return jsonify({"code": 404, "message": "项目不存在"}), 404
    default_start, default_end = _default_date_range()
    start_date = request.args.get('start_date', default_start)
    end_date = request.args.get('end_date', default_end)
    cache_key = f"llm:activity:team:{p.litellm_team_id}:{start_date}:{end_date}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return jsonify({"code": 200, "data": cached})
    try:
        data = llm.team_daily_activity(p.litellm_team_id, start_date=start_date, end_date=end_date)
        _apply_model_aliases(data.get("results") or data.get("data") or [])
        _cache_set(cache_key, data, ttl=ACTIVITY_CACHE_TTL)
        return jsonify({"code": 200, "data": data})
    except LiteLLMError as e:
        return _llm_error_response(e)


@bp.route("/admin/model-stats", methods=["GET"])
@jwt_required()
@check_permission(LLM_PERMISSION)
def get_model_stats():
    """全局按模型分组的用量统计，用于管理员看板。"""
    default_start, default_end = _default_date_range()
    start_date = request.args.get('start_date', default_start)
    end_date = request.args.get('end_date', default_end)
    cache_key = f"llm:model_stats:{start_date}:{end_date}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return jsonify({"code": 200, "data": cached})
    try:
        data = llm.model_spend_report(start_date=start_date, end_date=end_date)
        _cache_set(cache_key, data, ttl=ACTIVITY_CACHE_TTL)
        return jsonify({"code": 200, "data": data})
    except LiteLLMError as e:
        return _llm_error_response(e)


# ==================== 平台用户：我的活动趋势 ====================

@bp.route("/my-activity", methods=["GET"])
@jwt_required()
def get_my_activity():
    """当前用户查询自己的按日活动数据。"""
    user = _current_user()
    if not user:
        return jsonify({"code": 401, "message": "用户未认证"}), 401
    default_start, default_end = _default_date_range()
    start_date = request.args.get('start_date', default_start)
    end_date = request.args.get('end_date', default_end)
    cache_key = f"llm:activity:user:{user.id}:{start_date}:{end_date}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return jsonify({"code": 200, "data": cached})
    try:
        data = llm.user_daily_activity(user.id, start_date=start_date, end_date=end_date)
        _apply_model_aliases(data.get("results") or data.get("data") or [])
        _cache_set(cache_key, data, ttl=ACTIVITY_CACHE_TTL)
        return jsonify({"code": 200, "data": data})
    except LiteLLMError as e:
        return _llm_error_response(e)


@bp.route("/quota-requests", methods=["GET"])
@jwt_required()
def list_my_quota_requests():
    user = _current_user()
    if not user:
        return jsonify({"code": 401, "message": "用户未认证"}), 401

    reqs = LLMQuotaRequestModel.query.filter_by(user_id=user.id).order_by(
        LLMQuotaRequestModel.created_at.desc()).all()
    result = [{
        "id": r.id,
        "current_budget": r.current_budget,
        "requested_budget": r.requested_budget,
        "reason": r.reason,
        "status": r.status,
        "review_comment": r.review_comment,
        "created_at": r.created_at.strftime('%Y-%m-%d %H:%M:%S') if r.created_at else None,
        "reviewed_at": r.reviewed_at.strftime('%Y-%m-%d %H:%M:%S') if r.reviewed_at else None,
        "override_expires_at": r.override_expires_at.strftime('%Y-%m-%d %H:%M:%S') if r.override_expires_at else None,
        "reverted_at": r.reverted_at.strftime('%Y-%m-%d %H:%M:%S') if r.reverted_at else None,
    } for r in reqs]
    return jsonify({"code": 200, "data": result})

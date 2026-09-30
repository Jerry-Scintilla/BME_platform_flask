"""内部工作台·私有文件蓝图（/work/files，设计方案 §9/§13，M4）。

上传：单请求直传 + 服务端交叉校验 + 配额原子预占（413 超限 / 415 类型拒绝，
伪装扩展名隔离）；下载：JWT 会话 + 按本次关联判权 + 流式回传（A07 撤权即拒）。
"""
import io

from flask import Blueprint, jsonify, request, send_file
from flask_jwt_extended import jwt_required

from exts import db, limiter
from blueprints import _current_user
from services.work import access, files as files_service
from services.work.access import WorkApiError

bp = Blueprint("work_files", __name__, url_prefix="/work")


@bp.errorhandler(WorkApiError)
def _handle_work_files_error(err):
    return jsonify({"code": err.code, "message": err.message, "data": None}), err.code


def _require_user():
    user = _current_user()
    if not user:
        raise WorkApiError(401, '用户未认证')
    return user


@bp.route("/items/<int:item_id>/files", methods=["POST"])
@jwt_required()
@limiter.limit("10/minute")
def upload_file(item_id):
    """上传附件（multipart: file；body 可带 file_id 表示为既有文件传新版本）。"""
    user = _require_user()
    file_storage = request.files.get("file")
    if not file_storage:
        return jsonify({"code": 400, "message": "缺少 file 字段"}), 400
    file_id = request.form.get("file_id") or None
    wf, version = files_service.upload_version(
        user, item_id, file_storage,
        file_id=access.int_or_400(file_id, 'file_id') if file_id else None)
    db.session.commit()
    return jsonify({"code": 200, "message": "已上传",
                    "data": {"file_id": wf.id, "version_id": version.id,
                             "version_no": version.version_no}})


@bp.route("/files", methods=["GET"])
@jwt_required()
def search_files():
    """「工作资料」附件索引：跨我的可见事项聚合，文件名受限检索。"""
    user = _require_user()
    data = files_service.search_files(user, q=request.args.get("q"),
                                      page=request.args.get("page", 1),
                                      page_size=request.args.get("page_size", 20))
    return jsonify({"code": 200, "message": "ok", "data": data})


@bp.route("/files/<int:file_id>/versions", methods=["GET"])
@jwt_required()
def file_versions(file_id):
    user = _require_user()
    return jsonify({"code": 200, "message": "ok",
                    "data": files_service.list_versions(user, file_id)})


@bp.route("/files/<int:file_id>/download", methods=["GET"])
@jwt_required()
def download_file(file_id):
    """鉴权下载：带 link_id（附件关联或交付固定版本）；404 统一形态防探测（A07）。"""
    from storage import storage
    user = _require_user()
    wf, version = files_service.download(user, file_id, request.args.get("link_id"))
    # 紧急介入下载的服务层留痕（emergency_access 事件）在此提交（#6）；
    # 常规路径无待提交变更，commit 为空操作
    db.session.commit()
    try:
        obj = storage.get_object(version.object_key)
        data = obj.read()
        obj.close()
    except Exception:
        return jsonify({"code": 500, "message": "文件存储暂不可用"}), 500
    # 前端 fetch-blob 保存；禁公共缓存（§9.3）
    resp = send_file(io.BytesIO(data), as_attachment=True,
                     download_name=wf.display_name,
                     mimetype=version.content_type or 'application/octet-stream',
                     max_age=0)
    resp.headers['Cache-Control'] = 'no-store'
    return resp

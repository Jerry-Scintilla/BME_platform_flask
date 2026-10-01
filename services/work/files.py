"""内部工作台·私有文件服务（设计方案 §9，M4）。

文件属于事项而非个人网盘：本体走 storage 私有命名空间 `work/files/{item}/{file}/v{n}_{uuid}{ext}`
（不经 /media 公开链路）；下载一律 JWT + 当前访问权 + 按「本次关联」判权（§9.3，
短签不作隐私保证）。版本不可变：上传新版本生成新行，旧版本永不被覆盖（B05：
已提交的交付固定引用当时的版本）。

校验链（§9.2）：扩展名 ∩ 魔数 ∩ 声明类型交叉（C02）；单文件/单事项配额在
FOR UPDATE 事项行内原子预占（C01 防并发突破）；任何一步失败即隔离并清理对象，
不产生可下载的半成品。格式校验与病毒扫描状态分列（首期未启用扫描=not_required，
状态位先行）。24h 临时区/两步 finalize 属大文件断点续传设计，首期单请求直传 +
校验后落正式键即为等效安全（无临时区则无临时清理），偏差已记录于计划。
"""
import io
import os
import uuid
from datetime import datetime

from exts import db
from models import UserModel, WorkEvent, WorkFile, WorkFileLink, WorkFileVersion, WorkItem
from services.work import access
from services.work.access import WorkApiError
from services.work.events import record_event

# 允许类型白名单（§9.2：首期有限类型；Office 待病毒检测能力就绪后启用）
ALLOWED_TYPES = {
    'pdf': ('application/pdf',),
    'png': ('image/png',),
    'jpg': ('image/jpeg',),
    'jpeg': ('image/jpeg',),
    'txt': ('text/plain',),
    'md': ('text/plain', 'text/markdown'),
}
# 魔数（二进制类型必检；txt/md 走文本启发式：可 UTF-8 解码且无 NUL）
MAGIC_BYTES = {
    'pdf': b'%PDF',
    'png': b'\x89PNG\r\n\x1a\n',
    'jpg': b'\xff\xd8\xff',
    'jpeg': b'\xff\xd8\xff',
}
DETECT_RULES_VERSION = '1'

MAX_FILE_MB = int(os.getenv('WORK_FILE_MAX_MB', '25'))
ITEM_QUOTA_MB = int(os.getenv('WORK_ITEM_QUOTA_MB', '200'))
MAX_DISPLAY_NAME_LEN = 200


def _ext_of(filename):
    name = (filename or '').rsplit('/', 1)[-1]
    return name.rsplit('.', 1)[-1].lower() if '.' in name else ''


def _looks_text(sample):
    """文本启发式：无 NUL 且可按 UTF-8 解码（容忍头部采样在多字节字符中间截断）。"""
    if b'\x00' in sample:
        return False
    for trim in range(4):          # UTF-8 字符最长 4 字节，尾部截断容忍
        try:
            (sample[:len(sample) - trim] if trim else sample).decode('utf-8')
            return True
        except UnicodeDecodeError:
            continue
    return False


def _check_format(ext, head_bytes):
    """扩展名 ∩ 魔数/文本启发式 交叉（C02：伪装扩展拒绝）。"""
    if ext not in ALLOWED_TYPES:
        return False, '类型不在允许范围（pdf/png/jpg/jpeg/txt/md）'
    magic = MAGIC_BYTES.get(ext)
    if magic is not None:
        if not head_bytes.startswith(magic):
            return False, '文件内容与扩展名不符'
    elif not _looks_text(head_bytes):
        return False, '文本文件包含非文本内容'
    return True, ''


def _object_key(item_id, file_id, version_no, ext):
    return f'work/files/{item_id}/{file_id}/v{version_no}_{uuid.uuid4().hex}{("." + ext) if ext else ""}'


def _quota_used(item_id, exclude_file_id=None):
    """事项当前活跃版本字节和。

    配额口径（#4 对齐注释）：只计各文件 current_version_id 指向的 passed 版本
    ——历史版本不计活跃配额（B05 版本永留不放大占用）；为既有文件传新版本
    （exclude_file_id）时，该文件即将被新版本替换的现 current 也不计入预占。
    隔离/移除文件不计。"""
    query = (db.session.query(WorkFileVersion.size)
             .join(WorkFile, WorkFile.id == WorkFileVersion.file_id)
             .filter(WorkFile.item_id == item_id,
                     WorkFile.status == 'active',
                     WorkFileVersion.id == WorkFile.current_version_id,
                     WorkFileVersion.format_check == 'passed'))
    if exclude_file_id is not None:
        query = query.filter(WorkFile.id != exclude_file_id)
    return sum(r[0] for r in query.all())


def _require_upload_access(user, item_id):
    item = (WorkItem.query.filter_by(id=item_id).with_for_update().first())
    item, item_access = access.require_read(user, item)
    if item.status == 'draft':
        raise WorkApiError(409, '草稿不能上传附件')
    return item


def upload_version(user, item_id, file_storage, *, file_id=None):
    """上传新附件（file_id=None）或已有文件的新版本。

    事务段（持有事项行锁串行化配额预占，C01）：校验 → 建行(uploading/pending) →
    落对象 → stat 复核大小 → 交叉校验 → 通过置 active + 附件关联 + 事件；
    置 quarantined 的失败路径先 commit 隔离记录再拒绝（#39：拒绝上传留审计
    痕迹，对象已清不可下载）。文件移除端点（释放配额）待后续批次，现状
    removed 状态暂无写入方。"""
    from storage import storage

    item = _require_upload_access(user, item_id)

    display_name = (file_storage.filename or '未命名')[:MAX_DISPLAY_NAME_LEN]
    ext = _ext_of(display_name)
    # 声明类型交叉：浏览器给的 content_type 须与白名单一致（text/* 宽容 markdown 变体）
    declared = (file_storage.mimetype or '').split(';')[0].strip().lower()
    if ext in ALLOWED_TYPES and declared and declared not in ('application/octet-stream',):
        if declared not in ALLOWED_TYPES[ext] and not (
                ext in ('txt', 'md') and declared.startswith('text/')):
            raise WorkApiError(415, '声明的文件类型与扩展名不符')

    head = file_storage.stream.read(16)
    file_storage.stream.seek(0)
    ok, why = _check_format(ext, head)
    if not ok:
        raise WorkApiError(415, why)          # 未落任何行/对象，直接拒绝

    # 配额原子预占：持事项行锁下核算（并发上传串行化；传新版本时被替换的
    # 现 current 不计入，#4 口径）
    used = _quota_used(item.id, exclude_file_id=file_id)
    content_length = file_storage.content_length or 0
    single_limit = MAX_FILE_MB * 1024 * 1024
    quota_limit = ITEM_QUOTA_MB * 1024 * 1024
    if content_length > single_limit:
        raise WorkApiError(413, f'单文件不能超过 {MAX_FILE_MB}MB')

    wf = None
    if file_id is not None:
        wf = WorkFile.query.filter_by(id=file_id, item_id=item.id).first()
        if not wf:
            raise WorkApiError(404, '文件不存在')
        if wf.status == 'removed':
            raise WorkApiError(409, '该文件已移除')
        last_version = (WorkFileVersion.query.filter_by(file_id=wf.id)
                        .order_by(WorkFileVersion.version_no.desc()).first())
        version_no = (last_version.version_no + 1) if last_version else 1
        prev_id = last_version.id if last_version else None
    else:
        wf = WorkFile(workspace_id=item.workspace_id, item_id=item.id,
                      display_name=display_name, status='uploading',
                      created_by=user.id)
        db.session.add(wf)
        db.session.flush()
        version_no, prev_id = 1, None

    version = WorkFileVersion(
        file_id=wf.id, version_no=version_no,
        object_key=_object_key(item.id, wf.id, version_no, ext),
        size=0, content_type=declared or ALLOWED_TYPES[ext][0],
        format_check='pending', scan_status='not_required',
        detect_rules_version=DETECT_RULES_VERSION,
        uploaded_by=user.id, prev_version_id=prev_id)
    db.session.add(version)
    db.session.flush()

    # 落对象（真实流式；stat 复核实际大小，不信客户端声明）
    try:
        storage.put_object(version.object_key, file_storage.stream,
                           content_type=version.content_type)
        actual = storage.stat_object(version.object_key).size
    except Exception:
        db.session.rollback()
        raise WorkApiError(500, '文件存储暂不可用，请稍后重试')
    version.size = actual
    if actual > single_limit:
        _quarantine(storage, version, wf)
        db.session.commit()          # 隔离记录先落库再拒绝（#39：审计痕迹）
        raise WorkApiError(413, f'单文件不能超过 {MAX_FILE_MB}MB')
    # 配额只按各文件 current 版本口径核算（见 _quota_used 注释，#4）
    if used + actual > quota_limit:
        _remove_object(storage, version.object_key)
        db.session.rollback()
        raise WorkApiError(413, f'该事项附件总量将超过 {ITEM_QUOTA_MB}MB 配额')

    # 交叉复检（对象已落盘后的最终判定）
    try:
        obj = storage.get_object(version.object_key, offset=0, length=16)
        head_actual = obj.read()
        obj.close()
    except Exception:
        head_actual = head
    ok, why = _check_format(ext, head_actual)
    if not ok:
        _quarantine(storage, version, wf)
        db.session.commit()          # 隔离记录先落库再拒绝（#39：审计痕迹）
        raise WorkApiError(415, why)

    version.format_check = 'passed'
    wf.status = 'active'
    wf.current_version_id = version.id
    wf.display_name = display_name if file_id is not None else wf.display_name
    db.session.flush()
    if file_id is None:
        db.session.add(WorkFileLink(file_id=wf.id, version_id=None, target_type='item',
                                    target_id=item.id, purpose='attachment',
                                    created_by=user.id))
    item.last_activity_at = datetime.now()
    record_event(item, 'file_attached', actor_user_id=user.id,
                 diff={'file_id': wf.id, 'version_no': version_no,
                       'name': display_name, 'size': actual})
    return wf, version


def _quarantine(storage, version, wf):
    """拒绝上传的隔离落库准备（#39）：版本行标 failed + 对象清除。

    无既有可用版本的文件（新文件半成品）整体置 quarantined；已有 current 的
    既有文件保持 active——坏的新版本只隔离自身，不毁掉可用的旧版本。"""
    version.format_check = 'failed'
    if not wf.current_version_id:
        wf.status = 'quarantined'
    _remove_object(storage, version.object_key)


def _remove_object(storage, key):
    try:
        storage.remove_object(key)
    except Exception:
        pass          # 清理失败不阻塞主流程（隔离态已不可下载）


# ── 查询与下载 ──────────────────────────────────────────────

def _version_brief(v):
    return {
        'id': v.id, 'version_no': v.version_no, 'size': v.size,
        'content_type': v.content_type,
        'format_check': v.format_check, 'scan_status': v.scan_status,
        'uploaded_by': v.uploaded_by,
        'created_at': v.created_at.strftime('%Y-%m-%d %H:%M') if v.created_at else None,
    }


def file_dict(wf, versions=None, current=None):
    """文件序列化；current 版本可由调用方批量预取后传入（#33：免逐文件查询）。"""
    if current is None and wf.current_version_id:
        current = WorkFileVersion.query.get(wf.current_version_id)
    data = {
        'id': wf.id, 'item_id': wf.item_id, 'display_name': wf.display_name,
        'status': wf.status,
        'current_version': _version_brief(current) if current else None,
    }
    if versions is not None:
        data['versions'] = [_version_brief(v) for v in versions]
    return data


def _has_emergency_access(user, item_id):
    """治理紧急介入是否已留痕（§5.4）：事件不可变，存在该治理者对本事项的
    emergency_access 事件即视为持有有效介入权（下载沿介入读取语义放行，#6）。"""
    return bool(WorkEvent.query.filter_by(
        item_id=item_id, event_type='emergency_access',
        actor_user_id=user.id).first())


def require_file_read(user, file_id, *, emergency_ok=False):
    """文件读取权 = 所属事项读取权（附件与正文同一对象权限，§13）。

    emergency_ok：下载/版本链路对治理身份的补充放行——常规读取权不通过时，
    持本事项有效紧急介入记录的治理者可读（#6：紧急介入「看得见也拿得到」，
    与 §5.4 介入读取语义对齐；列表/详情链路不走本参数维持原口径）。"""
    wf = WorkFile.query.filter_by(id=file_id).first()
    if not wf:
        raise WorkApiError(404, '文件不存在')
    item = WorkItem.query.get(wf.item_id)
    try:
        access.require_read(user, item)
    except WorkApiError:
        if not emergency_ok or item is None:
            raise
        if not access.is_governance(user) or not _has_emergency_access(user, item.id):
            raise
    return wf


def list_versions(user, file_id):
    wf = require_file_read(user, file_id, emergency_ok=True)
    versions = (WorkFileVersion.query.filter_by(file_id=wf.id)
                .order_by(WorkFileVersion.version_no.desc()).all())
    current = next((v for v in versions if v.id == wf.current_version_id), None)
    return file_dict(wf, versions, current=current)


def download(user, file_id, link_id):
    """鉴权下载（§9.3）：JWT 会话 + 当前访问权 + 按「本次关联」判权；
    撤权后新请求必须失败（A07）；404 统一形态防探测。

    紧急介入放行（#6）：治理者持本事项有效 emergency_access 事件时可下载，
    下载同样以 emergency_access 事件留痕（purpose=file_download）。"""
    from storage import storage as _storage  # noqa: F401  供蓝图流式读取

    try:
        link_id = int(link_id)
    except (TypeError, ValueError):
        raise WorkApiError(404, '文件不存在')   # link_id 沿 404 统一形态防探测
    link = WorkFileLink.query.filter_by(id=link_id).first()

    # 跨组交付包分支（跨组方案 §4.2，X1）：目标组成员可下载被交付的固定版本，
    # 判权=handoff 已接单/完成 且 目标工作区在本人授权内——与源事项正文无关
    if link and link.target_type == 'handoff':
        from models import WorkHandoff
        handoff = WorkHandoff.query.get(link.target_id)
        if not handoff or handoff.status not in ('accepted', 'done'):
            raise WorkApiError(404, '文件不存在')
        if handoff.to_workspace_id not in access.workspace_access(user):
            raise WorkApiError(404, '文件不存在')
        wf = WorkFile.query.filter_by(id=link.file_id).first()
        if not wf:
            raise WorkApiError(404, '文件不存在')
        version = (WorkFileVersion.query.get(link.version_id)
                   if link.version_id else None) or \
            WorkFileVersion.query.get(wf.current_version_id)
        if not version or version.format_check != 'passed':
            raise WorkApiError(404, '文件不存在')
        return wf, version

    wf = require_file_read(user, file_id, emergency_ok=True)
    link = WorkFileLink.query.filter_by(id=link_id, file_id=wf.id).first()
    if not link:
        raise WorkApiError(404, '文件不存在')
    # 关联目标须仍是当前可访问事项（submission 关联沿其事项判权）
    item = WorkItem.query.get(wf.item_id)
    via_emergency = False
    try:
        access.require_read(user, item)
    except WorkApiError:
        if not access.is_governance(user) or not _has_emergency_access(user, item.id):
            raise
        via_emergency = True

    version = None
    if link.version_id is not None:
        version = WorkFileVersion.query.filter_by(id=link.version_id, file_id=wf.id).first()
    if version is None:
        version = WorkFileVersion.query.get(wf.current_version_id)
    if not version or version.format_check != 'passed':
        raise WorkApiError(404, '文件不存在')
    if via_emergency:
        # 介入下载留痕：复用 emergency_access 事件（最小改动），diff 标注用途
        item = (WorkItem.query.filter_by(id=item.id).with_for_update().first())
        record_event(item, 'emergency_access', actor_user_id=user.id,
                     diff={'by': 'governance', 'purpose': 'file_download',
                           'file_id': wf.id, 'version_id': version.id})
    return wf, version


def bind_submission_files(item, submission, version_ids, actor):
    """提交绑定固定版本（B05）：验收绑定具体提交，后续新版本不影响既有交付。"""
    if not isinstance(version_ids, list) or not version_ids:
        return []
    if len(set(version_ids)) != len(version_ids) or len(version_ids) > 20:
        raise WorkApiError(400, 'file_version_ids 参数非法')
    links = []
    for vid in version_ids:
        version = (WorkFileVersion.query.filter_by(id=vid).first())
        if not version:
            raise WorkApiError(400, f'文件版本 #{vid} 不存在')
        wf = WorkFile.query.get(version.file_id)
        if not wf or wf.item_id != item.id or wf.status != 'active':
            raise WorkApiError(400, f'文件版本 #{vid} 不属于本事项')
        if version.format_check != 'passed':
            raise WorkApiError(400, f'文件版本 #{vid} 不可用')
        link = WorkFileLink(file_id=wf.id, version_id=version.id,
                            target_type='submission', target_id=submission.id,
                            purpose='submission_result', created_by=actor.id)
        db.session.add(link)
        db.session.flush()
        links.append(link)
    return links


def item_files(item_id):
    """事项附件列表（详情聚合用，读取权由调用方已校验；#43 去掉未用的 user 形参）。
    附 item 关联 link_id（下载按本次关联判权，§9.3）；current 版本一次预取（#33）。"""
    rows = WorkFile.query.filter_by(item_id=item_id).order_by(WorkFile.id).all()
    links = {l.file_id: l.id for l in WorkFileLink.query.filter_by(
        target_type='item', target_id=item_id).all()}
    rows = [wf for wf in rows if wf.status != 'quarantined']
    version_map = {}
    if rows:
        version_map = {v.id: v for v in WorkFileVersion.query.filter(
            WorkFileVersion.id.in_([wf.current_version_id for wf in rows
                                   if wf.current_version_id])).all()}
    out = []
    for wf in rows:
        data = file_dict(wf, current=version_map.get(wf.current_version_id))
        data['link_id'] = links.get(wf.id)
        out.append(data)
    return out


def search_files(user, q=None, page=1, page_size=20):
    """「工作资料」附件索引：跨我的可见事项聚合，文件名受限检索（§12.3）。"""
    page = max(1, access.int_or_400(page, 'page', default=1))
    page_size = min(100, max(1, access.int_or_400(page_size, 'page_size', default=20)))
    query = (db.session.query(WorkFile)
             .join(WorkItem, WorkItem.id == WorkFile.item_id)
             .filter(WorkFile.status == 'active', WorkFile.current_version_id.isnot(None)))
    query = access.filter_items_query(query, user)
    if q:
        # 通配符转义（#38）：检索词中的 %/_ 按字面匹配（与 items._like_pattern 同规则）
        text = (str(q) or '').strip()[:50]
        escaped = (text.replace('\\', '\\\\')
                   .replace('%', '\\%').replace('_', '\\_'))
        query = query.filter(WorkFile.display_name.like(f'%{escaped}%', escape='\\'))
    total = query.count()
    rows = (query.order_by(WorkFile.updated_at.desc(), WorkFile.id.desc())
            .offset((page - 1) * page_size).limit(page_size).all())
    names = _names({w.created_by for w in rows})
    items_map = {i.id: i for i in WorkItem.query.filter(
        WorkItem.id.in_({w.item_id for w in rows})).all()} if rows else {}
    version_map = {}
    if rows:
        version_map = {v.id: v for v in WorkFileVersion.query.filter(
            WorkFileVersion.id.in_([w.current_version_id for w in rows])).all()}
    files = []
    for w in rows:
        item = items_map.get(w.item_id)
        version = version_map.get(w.current_version_id)
        files.append({
            'id': w.id, 'display_name': w.display_name,
            'size': version.size if version else 0,
            'uploaded_by': names.get(w.created_by),
            'updated_at': w.updated_at.strftime('%Y-%m-%d %H:%M') if w.updated_at else None,
            'item_id': w.item_id, 'item_title': item.title if item else None,
        })
    return {'files': files, 'total': total, 'page': page, 'page_size': page_size}


def _names(uids):
    uids = {u for u in uids if u} - {None}
    if not uids:
        return {}
    return {u.id: u.username for u in UserModel.query.filter(UserModel.id.in_(list(uids))).all()}

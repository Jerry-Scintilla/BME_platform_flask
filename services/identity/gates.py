"""职务/权限挂载的核验门槛接线助手（R0，2026-10-02 收紧批）。

enforcement.py 刻意保持零 flask 依赖；本模块补蓝图侧错误响应构造，
返回 (resp, 403) 元组与 club_admin._resolve_position 等既有惯例一致。
统一契约：403 + machine='IDENTITY_VERIFICATION_REQUIRED' + gate + verification_status，
前端可按 verification_status 换引导文案（未核验/审核中/争议/撤销）。
"""
from flask import jsonify

from services.identity import enforcement

MACHINE = 'IDENTITY_VERIFICATION_REQUIRED'


def verify_reject_reason(user, gate, scope_id=None):
    """enforce 拒绝时的行内原因文本（批量端点逐项回报用）。返回 None=放行。"""
    ok, reason, _status = enforcement.check_verified(user, gate, scope_id)
    return None if ok else reason


def ensure_verified_target(user, gate, action_desc=None, scope_id=None):
    """挂载职务/权限前对目标账号判定。通过返回 None；拒绝返回 (resp, 403)。

    action_desc 如「任命为组长」，拼进人话 message 供管理端直接 toast。"""
    ok, reason, status = enforcement.check_verified(user, gate, scope_id)
    if ok:
        return None
    who = getattr(user, 'username', None) or f'#{user.id}'
    action = f'，不能{action_desc}' if action_desc else ''
    guide = ('审核通过后即可操作' if status == 'pending'
             else '请先引导其在用户端「身份与账号」页完成实名核验')
    return jsonify({
        'code': 403,
        'machine': MACHINE,
        'gate': gate,
        'verification_status': status,
        'message': f'{who}：{reason}{action}——{guide}',
    }), 403

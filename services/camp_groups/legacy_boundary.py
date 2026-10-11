"""旧课程小组与营期小组的命名空间边界；不按数字 ID 自动猜测或迁移对象。"""
from flask import request, jsonify

# 这些旧业务接口仍可能有外部调用；无营期上下文的旧请求保持原语义。
LEGACY_BLUEPRINTS = {'course_group', 'task'}
LEGACY_ENDPOINTS = {
    'user.group_add', 'user.group', 'user.group_list', 'user.group_delete',
    'user.attendence_yesterday', 'user.attendence_by_date',
    'learningProgress.group', 'learningProgress.group_through_courseid',
    'information.leave_add', 'information.leave_delete', 'information.leave_query',
    'information.leave_approve', 'information.task_add', 'information.task_delete', 'information.task_query',
    'information.notice_add', 'information.notice_delete', 'information.notice_query',
    'information.information_query_all', 'information.homework_add', 'information.homework_delete',
    'information.homework_query', 'information.homework_download', 'information.homework_grade',
    'medal.user_medal_delete',
}
CAMP_KEYS = {'camp_session_id', 'camp_id', 'sid', 'unit_id', 'camp_group_id'}


def install(app):
    @app.before_request
    def reject_camp_context_in_legacy_groups():
        if request.method == 'OPTIONS' or (request.blueprint not in LEGACY_BLUEPRINTS and request.endpoint not in LEGACY_ENDPOINTS):
            return
        payload = request.get_json(silent=True)
        keys = set(request.args) | set(request.form)
        if isinstance(payload, dict):
            keys |= set(payload)
        if keys & CAMP_KEYS:
            return jsonify(code=409, machine='LEGACY_GROUP_CONTEXT_UNSUPPORTED',
                message='旧课程小组接口不接受营期或营期小组上下文，请从营期入口操作'), 409

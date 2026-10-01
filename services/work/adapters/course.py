"""课程域投影适配器（X2 通用投影架构）。

课程为平台公开目录对象：标题/状态对登录用户可见（已删除除外）——权限口径
平移自 integrations.py 原 course 分支，非新规则。stewardable=True：课程是
「责任物」，工作区可认领维护责任。
"""
from models import CourseModel
from services.work.projections import WorkProjectionProvider, register_projection

COURSE_STATUS_LABELS = {'normal': '在架', 'off_shelf': '已下架', 'deleted': '已删除'}


class CourseProvider(WorkProjectionProvider):
    source_type = 'course'
    label = '课程'
    stewardable = True

    def exists(self, source_id):
        course = CourseModel.query.get(source_id)
        return course is not None and course.status != CourseModel.STATUS_DELETED

    def summarize(self, user, source_id):
        course = CourseModel.query.get(source_id)
        if not course or course.status == CourseModel.STATUS_DELETED:
            return None
        return {
            'title': course.title,
            '状态': COURSE_STATUS_LABELS.get(course.status, course.status),
            '章节数': course.chapters,
        }


register_projection(CourseProvider())

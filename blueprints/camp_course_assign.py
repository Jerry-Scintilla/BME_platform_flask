"""营期课程分配（CampCourseAssignment）共享助手（2026-09-20 B2，migrate_47）。

《通知方案》§3.4：营期维度入课事实与 UserCourse（用户×课程全局唯一学习关系）解耦——
同课跨营各落一行 assignment，不再覆盖 UserCourse.camp_session_id。本模块集中：

  - upsert_assignment：入课写入（方向继承/传播/手动共用；幂等，复职可复活 ended 行）
  - end_member_assignments：成员移除时结束该营分配（学习历史行不回收）
  - active_scope_camp：进度读写的营期口径分流（原 learningProgress._camp_scope 的
    营戳判定改为 assignment 优先；同课跨营多活营时最新分配优先=旧覆盖行为语义；
    仅当 (user, course) 完全无 assignment 行时回退 legacy 营戳——回填后基本不走）

纯函数模块（无路由），camp_ms / camp / learningProgress 三方 import；只依赖 models
与 exts.db，不反向依赖任何蓝图，避免环。
"""
from datetime import datetime

from exts import db
from models import CampCourseAssignment, CampSession, UserCourseModel


def upsert_assignment(camp, student_uid, course_id,
                      source_type='direction', source_ref_id=None):
    """入课分配落行（幂等，不 commit——随调用方事务）。
    已有 active 行：不动（保留首次分配的来源/时间，重复继承零副作用）；
    已有 ended 行（成员移除后复职/重新归属）：复活——status/时间/来源重打；
    UserCourse 全局行由调用方保证存在（UQ user×course），本函数不碰它。"""
    row = CampCourseAssignment.query.filter_by(
        camp_session_id=camp.id, student_user_id=student_uid,
        course_id=course_id).first()
    if row:
        if row.status == CampCourseAssignment.STATUS_ENDED:
            row.status = CampCourseAssignment.STATUS_ACTIVE
            row.source_type = source_type
            row.source_ref_id = source_ref_id
            row.assigned_at = datetime.now()
            row.ended_at = None
        return row
    row = CampCourseAssignment(
        camp_session_id=camp.id, student_user_id=student_uid, course_id=course_id,
        source_type=source_type, source_ref_id=source_ref_id,
        status=CampCourseAssignment.STATUS_ACTIVE)
    db.session.add(row)
    return row


def end_member_assignments(sid, uid):
    """成员移除（软删除）时结束其在营分配（status=ended + ended_at，不 commit）。
    课程学习历史（UserCourse/进度/认证行）不回收——与 release 导生不回收旧课同口径。"""
    now = datetime.now()
    for row in CampCourseAssignment.query.filter_by(
            camp_session_id=sid, student_user_id=uid,
            status=CampCourseAssignment.STATUS_ACTIVE).all():
        row.status = CampCourseAssignment.STATUS_ENDED
        row.ended_at = now


def active_scope_camp(user_id, course_id, prefer_sid=None):
    """(user, course) 的当前营期口径（进度快照读写分流，learningProgress 用）。
    返回 CampSession 或 None（None=走全局表）。判定序：
    1. prefer_sid（前端从营内入口带参）：该营有 active 分配且营非 archived → 该营；
    2. active 分配行中营非 archived 的最新一条（同课跨营多活营：最新分配=当前学习
       语境，等价旧「覆盖营戳」的行为语义但不破坏旧营 linkage）；
    3. (user, course) 完全无 assignment 行（任意状态）→ 回退 legacy 营戳（回填漏网
       兜底；有行但全 ended/全 archived 则返回 None——分配表一旦有据即权威）。"""
    rows = CampCourseAssignment.query.filter_by(
        student_user_id=user_id, course_id=course_id).all()
    if not rows:
        uc = UserCourseModel.query.filter_by(
            user_id=user_id, course_id=course_id).first()
        if uc and uc.camp_session_id:
            camp = CampSession.query.get(uc.camp_session_id)
            if camp and camp.status != 'archived':
                return camp
        return None
    active = [r for r in rows if r.status == CampCourseAssignment.STATUS_ACTIVE]
    active.sort(key=lambda r: (r.assigned_at is None, r.assigned_at, r.id),
                reverse=True)
    if prefer_sid is not None:
        for row in active:
            if row.camp_session_id == prefer_sid:
                camp = CampSession.query.get(prefer_sid)
                if camp and camp.status != 'archived':
                    return camp
                return None            # 显式点名已归档/无效的营：不偷换别的营
        # prefer_sid 无分配行：忽略，继续按最新分配推导
    seen_ids = {r.camp_session_id for r in active}
    camps = [c for c in CampSession.query.filter(CampSession.id.in_(seen_ids)).all()
             if c.status != 'archived']
    if not camps:
        return None                     # 有分配行但全 ended/archived：不回退戳
    newest = active[0].camp_session_id
    for camp in camps:                  # 最新分配的营优先，其余按 id 稳定排序
        if camp.id == newest:
            return camp
    return sorted(camps, key=lambda c: c.id)[-1]

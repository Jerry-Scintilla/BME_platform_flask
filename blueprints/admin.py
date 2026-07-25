"""
管理员聚合看板蓝图

GET /admin/overview —— 后台首页一次性聚合各业务域统计
（用户总数 / 今日新增 / 今日打卡 / 文章 / 课程 / 勋章发放 / 小组 / 营期 / 待审批配额），
供 admin 管理面板首页（DashboardComponent）渲染真实数据，替代原先的硬编码假数据。

只读接口：挂 @jwt_required + @check_permission('system_management')；super_admin 经 is_admin_like() 直通。
"""
from datetime import date

from flask import Blueprint, jsonify
from flask_jwt_extended import jwt_required
from sqlalchemy import func

from exts import db
from models import (
    UserModel,
    ArticleModel,
    CourseModel,
    MedalUserModel,
    CourseGroup,
    CampSession,
    CheckRecord,
    LLMQuotaRequestModel,
)
from . import check_permission

bp = Blueprint("admin", __name__, url_prefix="/admin")


@bp.route("/overview", methods=["GET"])
@jwt_required()
@check_permission('system_management')
def admin_overview():
    """后台首页聚合统计"""
    today = date.today()

    user_total = UserModel.query.count()
    user_new_today = UserModel.query.filter(
        func.date(UserModel.join_time) == today).count()
    checkin_today = db.session.query(CheckRecord.user_id).filter(
        CheckRecord.date == today).distinct().count()
    article_count = ArticleModel.query.count()
    course_count = CourseModel.query.filter(
        CourseModel.status != CourseModel.STATUS_DELETED).count()
    medal_granted = MedalUserModel.query.count()
    group_count = CourseGroup.query.count()
    camp_count = CampSession.query.count()
    pending_quota = LLMQuotaRequestModel.query.filter_by(
        status=LLMQuotaRequestModel.STATUS_PENDING).count()

    return jsonify({
        "code": 200,
        "data": {
            "user_total": user_total,
            "user_new_today": user_new_today,
            "checkin_today": checkin_today,
            "article_count": article_count,
            "course_count": course_count,
            "medal_granted": medal_granted,
            "group_count": group_count,
            "camp_count": camp_count,
            "pending_quota": pending_quota,
        }
    })

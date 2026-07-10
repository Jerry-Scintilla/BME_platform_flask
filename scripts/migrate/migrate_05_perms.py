"""迁移 05：权限规范化（幂等）。

- 创建 17 个下划线权限（含 camp_* / seat_management / llm_management / attendance_report_recipient）
- 删除旧点号权限（course.manage 等）及其 user_permission 授权
- 给 role='super_admin' 的用户授全部权限（虽 super_admin 直通，留作一致性）

依赖 migrate_01 把 admin 回填为 role='super_admin'。用法（项目根）：python scripts/migrate/migrate_05_perms.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from app import app          # noqa: E402
from exts import db          # noqa: E402
from models import PermissionModel, UserPermissionModel, UserModel  # noqa: E402

PERMS = [
    ("course_management", "课程管理"), ("user_management", "用户管理"),
    ("article_management", "文章管理"), ("group_management", "小组管理"),
    ("medal_management", "勋章管理"), ("task_management", "任务管理"),
    ("discussion_management", "讨论区管理"), ("checkin_management", "签到管理"),
    ("seat_management", "座位管理"), ("system_management", "系统管理"),
    ("llm_management", "大模型服务管理"),
    ("attendance_report_recipient", "接收每日出勤汇总邮件"),
    ("camp_management", "营期管理"), ("camp_attendance_view", "营期考勤看板"),
    ("camp_leave_approve", "营期请假审批"), ("camp_reward_issue", "营期奖励发放"),
    ("camp_seat_assign", "营期座位分配"),
]

with app.app_context():
    created = 0
    for name, desc in PERMS:
        if not PermissionModel.query.filter_by(name=name).first():
            db.session.add(PermissionModel(name=name, description=desc))
            created += 1
    db.session.flush()

    dotted = PermissionModel.query.filter(PermissionModel.name.like('%.%')).all()
    for p in dotted:
        UserPermissionModel.query.filter_by(permission_id=p.id).delete()
        db.session.delete(p)

    supers = UserModel.query.filter_by(role='super_admin').all()
    granted = 0
    for u in supers:
        for name, _ in PERMS:
            p = PermissionModel.query.filter_by(name=name).first()
            if p and not UserPermissionModel.query.filter_by(user_id=u.id, permission_id=p.id).first():
                db.session.add(UserPermissionModel(user_id=u.id, permission_id=p.id))
                granted += 1

    db.session.commit()
    print(f"[+] created {created} perms | deleted {len(dotted)} dotted | granted {granted} to {len(supers)} super_admin(s)")
    print("[i] all perms:", sorted(p.name for p in PermissionModel.query.all()))
    print("[done] migrate_05 完成")

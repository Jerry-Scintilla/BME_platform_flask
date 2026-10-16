"""显式启用的项目到期提醒；按来源、接收人和日期去重。"""
from datetime import timedelta
from exts import db
from models import Project,ProjectReminder,UserModel,PersonModel,GroupMembership,CampGroup
from . import core,calendar,enabled


def run(*,apply=False,days=0):
    if type(days) is not int or not 0<=days<=30:raise core.ProjectError('提前天数须为0至30',status=400)
    if not enabled():return []
    output=[]
    for project in Project.query.filter_by(status='active',migration_ready=True).order_by(Project.id):
        members=GroupMembership.query.filter_by(group_id=project.group_id,status='active').all()
        recipients=sorted({m.user_id for m in members})
        ids=sorted(set(recipients)|{project.created_by})
        if apply:
            users=UserModel.query.filter(UserModel.id.in_(ids)).order_by(UserModel.id).with_for_update().all()
            pids=sorted({u.person_id for u in users if u.person_id})
            if pids:PersonModel.query.filter(PersonModel.id.in_(pids)).order_by(PersonModel.id).with_for_update().all()
            CampGroup.query.filter_by(id=project.group_id).with_for_update().one()
            project=Project.query.filter_by(id=project.id).populate_existing().with_for_update().one()
            if project.status!='active' or not project.migration_ready:
                db.session.commit();continue
        target=calendar.today(project)+timedelta(days=days)
        for source,row in calendar.rows(project):
            if row.status!='planned' or calendar.due(row,project)!=target:continue
            for uid in recipients:
                user=db.session.get(UserModel,uid)
                if not core.role(project,user,fresh=apply):continue
                key=core._digest([project.id,source,row.id,uid,target.isoformat()])
                if db.session.get(ProjectReminder,key):continue
                output.append({'project_id':project.id,'source':source,'id':row.id,'user_id':uid,'due_on':target.isoformat()})
                if apply:
                    from blueprints.notification import create_notification
                    change=core.event(project,db.session.get(UserModel,project.created_by),'reminder','项目节点到期提醒',after=calendar.item(row,project,source));db.session.flush()
                    create_notification(uid,'项目节点到期提醒',f'「{project.title}」有节点计划于{target.isoformat()}到期。',category='system',source_type='project_core',source_id=change.id)
                    db.session.add(ProjectReminder(key=key,project_id=project.id,user_id=uid))
        if apply:db.session.commit()
    return output

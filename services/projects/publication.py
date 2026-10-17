"""项目广场可选适配；项目主档不依赖展示模块的存在。"""
from sqlalchemy import inspect,update,select,or_,and_
from exts import db
from models import ShowcaseProject,ProjectLegacyMap


def synchronize_status(project):
    if not inspect(db.session.connection()).has_table(ShowcaseProject.__tablename__):return
    old_units=select(ProjectLegacyMap.unit_id).where(ProjectLegacyMap.project_id==project.id,ProjectLegacyMap.write_model=='project')
    db.session.execute(update(ShowcaseProject.__table__).where(or_(
        and_(ShowcaseProject.source=='project',ShowcaseProject.source_ref==project.id),
        and_(ShowcaseProject.source=='camp',ShowcaseProject.source_ref.in_(old_units))
    )).values(project_status='done' if project.status in ('completed','terminated','archived') else 'ongoing'))

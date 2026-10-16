import os,sys,json,hashlib,subprocess
from pathlib import Path
from datetime import date,datetime,timedelta
from dotenv import dotenv_values
import pymysql
root=Path(__file__).resolve().parents[1]
os.chdir(root);sys.path.insert(0,str(root));(root/'log').mkdir(exist_ok=True)
common=Path(subprocess.check_output(['git','rev-parse','--git-common-dir'],text=True).strip()).resolve()
env=dotenv_values(root/'.env' if (root/'.env').exists() else common.parent/'.env')
assert env.get('DB_HOST','127.0.0.1') in ('127.0.0.1','localhost'),'Local preview must use local database'
for key in ['DB_HOST','DB_PORT','DB_USERNAME','DB_PASSWORD','REDIS_URL']:
 if env.get(key):os.environ[key]=str(env[key])
os.environ['JWT_SECRET']='local-workspace-preview-only-secret-20261016'
os.environ['DEV_TEST_ACCOUNTS']='on';os.environ['AUTH_REFRESH_COOKIE_ENABLED']='false'
os.environ['POINTS_CENTER_ENABLED']='false';os.environ['IDENTITY_ENFORCEMENT_MODE']='shadow'
os.environ['PROJECT_CORE_ENABLED']='true'
os.environ['IDENTITY_VERIFY_GATE_DEFAULT']='shadow';os.environ['STORAGE_BACKEND']='local'
os.environ['DATA_ROOT']=str(root/'data/workspace-preview')
database='bme_workspace_preview_20261016'
c=pymysql.connect(host=env.get('DB_HOST') or '127.0.0.1',port=int(env.get('DB_PORT') or 3306),user=env.get('DB_USERNAME'),password=env.get('DB_PASSWORD'))
with c.cursor() as cur:cur.execute('CREATE DATABASE IF NOT EXISTS `'+database+'` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci')
c.close()
from flask import Flask,jsonify
from flask_cors import CORS
from flask_jwt_extended import JWTManager
from sqlalchemy.engine import URL
import config
from exts import db,mail,limiter,redis_client
from storage import storage
app=Flask(__name__);app.config.from_object(config)
app.config.update(SQLALCHEMY_DATABASE_URI=URL.create('mysql+pymysql',username=env.get('DB_USERNAME'),password=env.get('DB_PASSWORD'),host=env.get('DB_HOST') or '127.0.0.1',port=int(env.get('DB_PORT') or 3306),database=database,query={'charset':'utf8mb4'}),RATELIMIT_ENABLED=False)
db.init_app(app);mail.init_app(app);limiter.init_app(app);redis_client.init_app(app);storage.init_app(app);JWTManager(app)
CORS(app,origins=['http://127.0.0.1:18096','http://localhost:18096','http://127.0.0.1:15196'],supports_credentials=True)
from blueprints import (auth_bp,user_bp,camp_bp,camp_staff_bp,camp_project_bp,camp_delivery_bp,camp_meeting_bp,camp_announcement_bp,notification_bp,medal_bp,project_core_bp)
from blueprints.points import bp as points_bp
for bp in [auth_bp,user_bp,camp_bp,camp_staff_bp,camp_project_bp,camp_delivery_bp,camp_meeting_bp,camp_announcement_bp,notification_bp,medal_bp,points_bp,project_core_bp]:app.register_blueprint(bp)
from services.request_guard import enforce_request_access
app.before_request(enforce_request_access)
from services.projects.legacy import install as install_project_fence
install_project_fence(app)
import models as m
from services.identity.person import create_provisional
from services.camp_groups import projects
with app.app_context():
 from sqlalchemy import inspect,text
 if inspect(db.engine).has_table('project_core_project') and 'migration_ready' not in {c['name'] for c in inspect(db.engine).get_columns('project_core_project')}:
  with db.engine.begin() as connection:connection.execute(text('ALTER TABLE project_core_project ADD COLUMN migration_ready BOOLEAN NOT NULL DEFAULT 1'))
 db.create_all()
 if not m.CampSession.query.filter_by(name='项目工作台 · 本地试用').first():
  def user(name,email,admin=False):
   u=m.UserModel(username=name,email=email,role='super_admin' if admin else 'user',status='active',lifecycle='active',account_kind='standard');u.set_password(hashlib.md5(b'12345678').hexdigest());db.session.add(u);db.session.flush();p,_created=create_provisional(u);p.verification_status='verified';p.verified_name=name;return u
  admin=user('预览管理员','workspace-admin@seed.dev',True)
  leader=user('负责人演示','workspace-leader@seed.dev')
  member=user('成员演示','workspace-member@seed.dev')
  other=user('另一位负责人','workspace-other@seed.dev')
  teacher=user('管理老师演示','workspace-teacher@seed.dev')
  dual=user('双身份演示','workspace-dual@seed.dev')
  camp=m.CampSession(name='项目工作台 · 本地试用',category='project',status='selecting',start_date=date(2026,10,1),end_date=date(2027,1,18),policy=m.CampPolicy(capabilities=json.dumps({'attendance':False,'leave':False,'seat':False})))
  db.session.add(camp);db.session.flush()
  db.session.add_all([m.CampStaff(camp_session_id=camp.id,user_id=teacher.id,role='teacher',status='active',assigned_by=admin.id),m.CampStaff(camp_session_id=camp.id,user_id=dual.id,role='owner',status='active',assigned_by=admin.id)])
  def unit(name,owner,people):
   u=m.CampUnit(camp_session_id=camp.id,unit_type='project',name=name,owner_user_id=owner.id,status='active',visibility='camp');db.session.add(u);db.session.flush();projects.bind_new_project(u,admin.id)
   db.session.add(m.ProjectProfile(unit_id=u.id,goal='完成需求、样机验证与结题成果。'))
   for person in people:
    projects.ensure_camp_participant(camp.id,person.id,admin.id,'本地试用数据');projects.add_member(u,person.id,'member','admin_import',admin.id,'本地试用数据')
   return u
  first=unit('智能肺康复口琴',leader,[member,dual]);second=unit('质子治疗计划优化',other,[leader,member]);solo=unit('单人研发项目',other,[])
  for u in [first,second,solo]:
   for i,(title,delta) in enumerate([('需求规格书',-2),('样机测试',7),('结题报告',30)],1):
    n=m.CampMilestone(camp_session_id=camp.id,unit_id=u.id,title=title,order_no=i,due_date=date.today()+timedelta(days=delta),requirement='提交本阶段工作说明与验证材料。',status='open');db.session.add(n);db.session.flush()
    if u.id==first.id:
     for person in [member,dual]:db.session.add(m.CampSubmissionVersion(milestone_id=n.id,submitted_by=person.id,version=1,content=f'{title}：本地演示材料，可在此追加新版本。',status='submitted'))
     if i==1:
      for person in [member,dual]:db.session.add(m.CampNodeEvaluation(camp_session_id=camp.id,unit_id=u.id,milestone_id=n.id,member_user_id=person.id,leader_user_id=leader.id,score=85,comment='需求材料完成'))
  db.session.add(m.CampMeeting(camp_session_id=camp.id,scope='unit',unit_id=first.id,title='方案与验证讨论',meeting_date=date.today(),content='本周补充样机测试记录，材料提交到相应交付节点。',created_by=leader.id))
  db.session.add(m.CampAnnouncement(camp_session_id=camp.id,title='本地工作台试用',content='这是隔离的本地试用营期，提交与评价只写入演示数据库。',created_by=teacher.id))
  camp.status='running';db.session.commit()
 leader=m.UserModel.query.filter_by(email='workspace-leader@seed.dev').one()
 teacher=m.UserModel.query.filter_by(email='workspace-teacher@seed.dev').one()
 admin=m.UserModel.query.filter_by(email='workspace-admin@seed.dev').one()
 training=m.CampSession.query.filter_by(name='培训营 · 本地兼容检查').first()
 if training is None:
  training=m.CampSession(name='培训营 · 本地兼容检查',category='learning',status='running',start_date=date(2026,10,1),end_date=date(2027,1,18),policy=m.CampPolicy(capabilities=json.dumps({'attendance':False,'leave':False,'seat':False})))
  db.session.add(training);db.session.flush();db.session.add(m.CampMember(camp_session_id=training.id,user_id=leader.id,role='student',status='active'));db.session.add(m.CampStaff(camp_session_id=training.id,user_id=teacher.id,role='teacher',status='active',assigned_by=admin.id));db.session.commit()
 archived=m.CampSession.query.filter_by(name='项目营 · 结营复盘演示').first()
 if archived is None:
  archived=m.CampSession(name='项目营 · 结营复盘演示',category='project',status='archived',start_date=date(2026,8,1),end_date=date(2026,9,1))
  db.session.add(archived);db.session.flush();db.session.add(m.CampMember(camp_session_id=archived.id,user_id=leader.id,role='member',status='active'))
  snapshot={'camp':{'id':archived.id,'name':archived.name},'members':[{'user':leader.username,'role':'member'}],'units':[{'unit_id':999,'name':'历史项目','leader':leader.username,'leader_user_id':leader.id,'status':'active','members':[{'user':leader.username,'user_id':leader.id,'role':'leader','status':'active'}],'milestones':[{'id':999,'title':'历史交付记录','status':'approved','node_complete':True,'evaluations':[]}],'verified_outcomes':[{'title':'历史结题报告','description':'归档时冻结的成果。'}]}]}
  db.session.add(m.CampArchive(camp_session_id=archived.id,snapshot=json.dumps(snapshot,ensure_ascii=False),frozen_by=admin.id));db.session.commit()
 camp=m.CampSession.query.filter_by(name='项目工作台 · 本地试用').one()
 from services.projects import core as project_core
 standalone=m.Project.query.filter_by(title='独立项目 · 日历试用').first()
 if standalone is None:
  standalone_id=project_core.create(leader,{'title':'独立项目 · 日历试用','goal':'不依赖营期，持续记录研发节点和项目历史。','idempotency_key':'preview-standalone'})['project']['id']
  standalone=db.session.get(m.Project,standalone_id)
  project_core.change_member(leader,standalone_id,{'action':'add','user_id':m.UserModel.query.filter_by(email='workspace-member@seed.dev').one().id,'expected_version':standalone.version,'reason':'本地独立项目试用','idempotency_key':'preview-standalone-member'})
  db.session.commit()
 if standalone.zero_date is None:
  from services.projects import calendar as project_calendar
  project_calendar.change_zero(leader,standalone.id,{'zero_date':'2026-10-16','confirm':True,'reason':'本地演示启动日','expected_version':standalone.version,'idempotency_key':'preview-project-zero'})
  db.session.commit()
 if not m.ProjectMilestone.query.filter_by(project_id=standalone.id).first():
  from services.projects import calendar as project_calendar
  for index,(title,offset) in enumerate([('需求评审',7),('首版样机验证',21),('阶段成果确认',45)]):
   project_calendar.save_item(leader,standalone.id,'milestone',{'title':title,'schedule_mode':'relative','offset_days':offset,'reason':'本地演示计划','expected_version':standalone.version,'idempotency_key':'preview-research-node-'+str(index)})
  db.session.commit()
 preview_camp_ids={'running':camp.id,'training':training.id,'archived':archived.id}
 core_camp=m.CampSession.query.filter_by(name='项目营 · 独立项目关联演示').first()
 if core_camp is None:
  core_camp=m.CampSession(name='项目营 · 独立项目关联演示',category='project',status='selecting',is_featured=True,start_date=date.today()-timedelta(days=7),end_date=date.today()+timedelta(days=90),policy=m.CampPolicy(capabilities=json.dumps({'attendance':False,'leave':False,'seat':False})))
  db.session.add(core_camp);db.session.flush();db.session.add(m.CampStaff(camp_session_id=core_camp.id,user_id=teacher.id,role='teacher',status='active',assigned_by=admin.id))
  from services.projects import camps as project_camps
  request_data=project_camps.request_binding(admin,standalone.id,{'camp_id':core_camp.id,'user_ids':[leader.id,m.UserModel.query.filter_by(email='workspace-member@seed.dev').one().id],'reason':'独立项目参营演示','expected_version':standalone.version,'idempotency_key':'preview-core-binding'})
  binding_data=request_data['binding']
  approved=project_camps.operate(admin,standalone.id,binding_data['id'],{'action':'approve','expected_version':standalone.version,'expected_binding_version':binding_data['version'],'reason':'本地试用审批','idempotency_key':'preview-core-approve'})
  mapped_unit=approved['binding']['unit_id']
  node=m.CampMilestone(camp_session_id=core_camp.id,unit_id=mapped_unit,title='本期阶段汇报',due_date=date.today()+timedelta(days=14))
  db.session.add(node);db.session.flush()
  db.session.add(m.ProjectAssessmentLink(camp_milestone_id=node.id,binding_id=binding_data['id']))
  db.session.commit()
 preview_camp_ids['independent']=core_camp.id
 member=m.UserModel.query.filter_by(email='workspace-member@seed.dev').one()
 dual=m.UserModel.query.filter_by(email='workspace-dual@seed.dev').one()
 newcomer=m.UserModel.query.filter_by(email='workspace-newcomer@seed.dev').first()
 if newcomer is None:
  newcomer=m.UserModel(username='待入营演示',email='workspace-newcomer@seed.dev',role='user',status='active',lifecycle='active',account_kind='standard')
  newcomer.set_password(hashlib.md5(b'12345678').hexdigest());db.session.add(newcomer);db.session.flush()
  person,_=create_provisional(newcomer);person.verification_status='verified';person.verified_name=newcomer.username
 for phase,name in [('upcoming','项目营 · 申报期检查'),('selecting','项目营 · 组队期检查'),('weekly','项目营 · 周考勤与请假检查')]:
  sample=m.CampSession.query.filter_by(name=name).first()
  if sample is None:
   sample=m.CampSession(name=name,category='project',status='selecting' if phase!='upcoming' else 'upcoming',start_date=date.today()-timedelta(days=7),end_date=date.today()+timedelta(days=90),policy=m.CampPolicy(attendance_mode='weekly',capabilities=json.dumps({'attendance':phase=='weekly','leave':phase=='weekly','seat':False})))
   db.session.add(sample);db.session.flush()
   db.session.add(m.CampStaff(camp_session_id=sample.id,user_id=teacher.id,role='teacher',status='active',assigned_by=admin.id))
   for participant in [leader,member,dual]:projects.ensure_camp_participant(sample.id,participant.id,admin.id,'本地阶段检查')
   if phase!='upcoming':
    sample_unit=m.CampUnit(camp_session_id=sample.id,unit_type='project',name='组队检查项目' if phase=='selecting' else '周考勤检查项目',owner_user_id=leader.id,status='active',visibility='camp')
    db.session.add(sample_unit);db.session.flush();projects.bind_new_project(sample_unit,admin.id)
    projects.add_member(sample_unit,member.id,'member','admin_import',admin.id,'本地阶段检查')
    db.session.add(m.ProjectProfile(unit_id=sample_unit.id,goal='专项检查用项目。'))
   if phase=='weekly':sample.status='running'
   db.session.commit()
  preview_camp_ids[phase]=sample.id
  if phase=='weekly':
   for participant in m.CampMember.query.filter_by(camp_session_id=sample.id):participant.attendance_required=True
 if not m.CampStaff.query.filter_by(camp_session_id=archived.id,user_id=teacher.id,status='active').first():
  db.session.add(m.CampStaff(camp_session_id=archived.id,user_id=teacher.id,role='teacher',status='active',assigned_by=admin.id))
 db.session.commit()
 print(json.dumps({'camp_id':camp.id,'database':database,'accounts':[u.email for u in m.UserModel.query.all()]},ensure_ascii=False),flush=True)
@app.get('/preview/health')
def health():
 return jsonify({'preview':True,'database':database,'camps':preview_camp_ids})
app.run(host='127.0.0.1',port=5016,threaded=True,use_reloader=False)

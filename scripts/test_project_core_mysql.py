"""本机临时MySQL结构及并发检查；使用随机新库，完成后仅删除本次创建的库。"""
import json,os,sys,subprocess,uuid,threading
from datetime import date
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from dotenv import dotenv_values
import pymysql
from sqlalchemy import create_engine,inspect
from sqlalchemy.engine import URL

root=Path(__file__).resolve().parents[1];os.chdir(root);sys.path.insert(0,str(root))
common=Path(subprocess.check_output(['git','rev-parse','--git-common-dir'],text=True).strip()).resolve()
env=dotenv_values(root/'.env' if (root/'.env').exists() else common.parent/'.env')
assert env.get('DB_HOST','127.0.0.1') in ('127.0.0.1','localhost'),'Only local MySQL is allowed'
os.environ['JWT_SECRET']='isolated-project-core-mysql-qa-secret-20261017'
os.environ['IDENTITY_ENFORCEMENT_MODE']='off';os.environ['IDENTITY_VERIFY_GATE_DEFAULT']='off'
os.environ['PROJECT_CORE_ENABLED']='true'
from flask import Flask
from exts import db
import config,models as m
from project_models import PROJECT_CORE_TABLES
from group_models import GROUP_CORE_TABLES
from services.projects import core,records
from services.groups import GroupError
name='bme_project_core_qa_'+uuid.uuid4().hex[:12]
connection=pymysql.connect(host=env.get('DB_HOST') or '127.0.0.1',port=int(env.get('DB_PORT') or 3306),user=env.get('DB_USERNAME'),password=env.get('DB_PASSWORD'),autocommit=True)
created=False;engine=None
try:
    with connection.cursor() as cursor:cursor.execute('CREATE DATABASE `'+name+'` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci')
    created=True
    uri=URL.create('mysql+pymysql',username=env.get('DB_USERNAME'),password=env.get('DB_PASSWORD'),host=env.get('DB_HOST') or '127.0.0.1',port=int(env.get('DB_PORT') or 3306),database=name,query={'charset':'utf8mb4'})
    app=Flask(__name__);app.config.from_object(config);app.config.update(SQLALCHEMY_DATABASE_URI=uri,PROJECT_CORE_ENABLED=True)
    db.init_app(app)
    with app.app_context():
        engine=db.engine;tables=set()
        def include(table):
            if table in tables:return
            tables.add(table)
            for fk in table.foreign_keys:include(fk.column.table)
        for table in tuple(PROJECT_CORE_TABLES)+tuple(GROUP_CORE_TABLES):include(table)
        include(m.NotificationModel.__table__)
        db.metadata.create_all(engine,tables=list(tables))
        # 再次执行新表的逐表创建，验证发布脚本采用的幂等次序。
        for table in PROJECT_CORE_TABLES:table.create(engine,checkfirst=True)
        owner=m.UserModel(username='并发检查负责人',email='owner@mysql-qa.invalid',password='local-qa-only',role='user',status='active',lifecycle='active',account_kind='standard')
        db.session.add(owner);db.session.commit();uid=owner.id
        pid=core.create(owner,{'title':'并发版本检查','idempotency_key':'initial-create'})['project']['id'];db.session.commit()
    def concurrent(worker,user_ids=None):
        barrier=threading.Barrier(2)
        def wrapped(index):
            with app.app_context():
                user=db.session.get(m.UserModel,(user_ids or [uid,uid])[index])
                barrier.wait(timeout=10)
                try:
                    value=worker(user,index);db.session.commit();return {'ok':True,'value':value}
                except GroupError as exc:db.session.rollback();return {'ok':False,'machine':exc.machine}
                finally:db.session.remove()
        with ThreadPoolExecutor(max_workers=2) as pool:return list(pool.map(wrapped,[0,1]))
    cas=concurrent(lambda u,i:core.update(u,pid,{'title':'版本'+str(i),'expected_version':1,'reason':'真实MySQL并发版本检查','idempotency_key':'cas-'+str(i)}))
    assert sum(r['ok'] for r in cas)==1,cas
    assert any(r.get('machine')=='PROJECT_VERSION_CONFLICT' for r in cas),cas
    replay=concurrent(lambda u,i:core.create(u,{'title':'同键并发创建','idempotency_key':'same-create-key'}))
    assert all(r['ok'] for r in replay),replay
    assert replay[0]['value']['project']['id']==replay[1]['value']['project']['id'],replay
    with app.app_context():
        assert m.Project.query.count()==2
        student=m.UserModel(username='并发检查成员',email='member@mysql-qa.invalid',password='local-qa-only',role='user',status='active',lifecycle='active',account_kind='standard')
        db.session.add(student);db.session.commit();student_id=student.id
        project=db.session.get(m.Project,pid)
        core.change_member(db.session.get(m.UserModel,uid),pid,{'action':'add','user_id':student_id,'expected_version':project.version,'reason':'并发通知检查','idempotency_key':'add-concurrent-member'})
        db.session.commit();version=project.version
    cross_user=concurrent(lambda u,i:records.save(u,pid,{'kind':'record','title':'并发记录'+str(i),'occurred_on':date.today().isoformat(),'reason':'真实MySQL不同成员同时写入及通知','expected_version':version,'idempotency_key':'record-'+str(i)},[],[]),[uid,student_id])
    assert sum(r['ok'] for r in cross_user)==1,cross_user
    assert any(r.get('machine')=='PROJECT_VERSION_CONFLICT' for r in cross_user),cross_user
    with app.app_context():
        assert m.ProjectRecord.query.count()==1
        assert m.NotificationModel.query.filter_by(source_type='project_core').count()==1
        print(json.dumps({'new_tables':len(PROJECT_CORE_TABLES),'dependency_tables':len(tables),'ddl_and_repeat_create':'passed','concurrent_version':'one success, one conflict','concurrent_idempotent_create':'same project returned','concurrent_cross_member_record_and_notification':'passed without deadlock','temporary_database_removed_after_check':True},ensure_ascii=False))
finally:
    if engine:engine.dispose()
    if created:
        with connection.cursor() as cursor:cursor.execute('DROP DATABASE `'+name+'`')
    connection.close()

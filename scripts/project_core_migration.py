"""独立项目映射工具：默认只读预览；准备、切换、撤销需显式--apply及源摘要。"""
import argparse,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--unit-id',type=int,required=True)
parser.add_argument('--actor-id',type=int,required=True)
parser.add_argument('--action',choices=['preview','prepare','switch','cancel'],default='preview')
parser.add_argument('--apply',action='store_true')
parser.add_argument('--source-digest')
parser.add_argument('--project-id',type=int)
parser.add_argument('--reason')
args=parser.parse_args()
if args.action!='preview' and (not args.apply or not args.source_digest or not args.reason):parser.error('写入需--apply、--source-digest和--reason')
from flask import Flask
from sqlalchemy import inspect
import config
from exts import db
import models as m
from project_models import PROJECT_CORE_TABLES
from services.projects import legacy
app=Flask(__name__);app.config.from_object(config);db.init_app(app)
with app.app_context():
    missing=[table.name for table in PROJECT_CORE_TABLES if not inspect(db.engine).has_table(table.name)]
    if missing:raise SystemExit('请先执行迁移79，缺少结构：'+', '.join(missing))
    actor=db.session.get(m.UserModel,args.actor_id)
    if not actor or not actor.is_admin() or actor.status!='active' or actor.lifecycle!='active':raise SystemExit('操作者须为有效平台管理员')
    if args.action=='preview':
        result=legacy.preview(args.unit_id);db.session.rollback()
    else:
        if not config.PROJECT_CORE_ENABLED:raise SystemExit('独立项目开关未启用，仅可只读预览')
        try:
            result=legacy.migrate(actor,args.unit_id,{'action':args.action,'source_digest':args.source_digest,'project_id':args.project_id,'reason':args.reason});db.session.commit()
        except Exception:
            db.session.rollback();raise
    print(json.dumps(result,ensure_ascii=False,indent=2))

"""项目到期提醒：默认只读预览；--apply写入站内通知，不操作积分。"""
import argparse,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from flask import Flask
import config
from exts import db
import models
from services.projects.reminders import run
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--apply',action='store_true');parser.add_argument('--days',type=int,default=0)
args=parser.parse_args()
app=Flask(__name__);app.config.from_object(config);db.init_app(app)
with app.app_context():print(json.dumps(run(apply=args.apply,days=args.days),ensure_ascii=False))

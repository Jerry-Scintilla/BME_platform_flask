"""执行一轮积分校验与待入账重试。需要 POINTS_REWARDS_ENABLED=true。"""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import app
from services.points import run_worker

if __name__ == '__main__':
    with app.app_context():
        if not app.config.get('POINTS_REWARDS_ENABLED'):
            raise SystemExit('积分奖励尚未启用')
        run_worker()

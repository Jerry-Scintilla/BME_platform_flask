"""
座位基础数据初始化（生产部署用）

幂等：重复执行只会补齐缺失的房间/座位，不会重复创建、不会改动已有绑定。
每次部署都可安全重跑 —— 已存在的房间/座位自动跳过。

用法:
    python init_seats.py

新增自习室：在下方 ROOMS 配置里加一项即可，无需改其它代码。
注意: 此脚本只初始化「系统基础数据」（房间+座位），不含任何测试用户/课程，
      生产环境请用本脚本，不要跑 seed.py。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import app
from exts import db
from models import RoomModel, SeatModel

# 房间 → 座位 label 列表
# 命名约定：label 首字母 = 八角形分组（A/B 左列，C/D/E 右列），后接 1-8 = 组内三角形位
ROOMS = {
    "106": [f"{letter}{n}" for letter in "ABCDE" for n in range(1, 9)],  # 5 组 × 8 = 40 座
    # 未来新增自习室在这里加一行即可，例如：
    # "112": [f"{letter}{n}" for letter in "AB" for n in range(1, 9)],
}


def init_seats():
    with app.app_context():
        # 1. 建表（幂等：已存在的表不受影响，只建缺失的）
        db.create_all()
        print("[ok] data tables ready")

        # 2. 按配置初始化房间与座位
        for room_name, labels in ROOMS.items():
            room = RoomModel.query.filter_by(name=room_name).first()
            if not room:
                room = RoomModel(name=room_name, description=f"{room_name} study room")
                db.session.add(room)
                db.session.flush()
                print(f"[+] created room {room_name} (id={room.id})")
            else:
                print(f"[=] room {room_name} exists (id={room.id})")

            existing = {s.label for s in SeatModel.query.filter_by(room_id=room.id).all()}
            added = 0
            for label in labels:
                if label in existing:
                    continue
                db.session.add(SeatModel(room_id=room.id, label=label))
                added += 1
            db.session.commit()

            total = SeatModel.query.filter_by(room_id=room.id).count()
            print(f"[+] room {room_name}: +{added} new, {total} total seats")


if __name__ == "__main__":
    init_seats()

from flask import Blueprint, request, jsonify

from exts import db
from models import RoomModel, SeatModel, UserModel, CheckRecord

from flask_jwt_extended import jwt_required, get_jwt_identity

from . import check_permission, audit_log

bp = Blueprint("seat", __name__, url_prefix="/seat")


# ── 房间 ──

@bp.route("/rooms")
@jwt_required()
def room_list():
    """列出所有自习室（带座位数）"""
    rooms = RoomModel.query.order_by(RoomModel.id).all()
    data = []
    for r in rooms:
        data.append({
            "Room_Id": r.id,
            "Room_Name": r.name,
            "Room_Description": r.description or "",
            "Seat_Count": SeatModel.query.filter_by(room_id=r.id).count(),
        })
    return jsonify({"code": 200, "message": "获取自习室列表成功", "rooms": data})


@bp.route("/room_create", methods=["POST"])
@jwt_required()
@check_permission('seat_management')
@audit_log(operation="创建自习室")
def room_create():
    name = request.json.get("Room_Name")
    description = request.json.get("Room_Description", "")
    if not name:
        return jsonify({"code": 401, "message": "未提供自习室名称"}), 401
    if RoomModel.query.filter_by(name=name).first():
        return jsonify({"code": 402, "message": "自习室已存在"}), 402
    room = RoomModel(name=name, description=description)
    db.session.add(room)
    db.session.commit()
    return jsonify({"code": 200, "message": "自习室创建成功", "Room_Id": room.id})


@bp.route("/room_delete", methods=["POST"])
@jwt_required()
@check_permission('seat_management')
@audit_log(operation="删除自习室")
def room_delete():
    room_id = request.json.get("Room_Id")
    room = RoomModel.query.filter_by(id=room_id).first()
    if not room:
        return jsonify({"code": 401, "message": "自习室不存在"}), 401
    # 先删座位再删房间
    SeatModel.query.filter_by(room_id=room_id).delete()
    db.session.delete(room)
    db.session.commit()
    return jsonify({"code": 200, "message": "自习室删除成功"})


# ── 座位（核心：对外读取 + 管理端增删/绑定） ──

def _occupied_user_ids():
    """当前已打卡（存在 check_out IS NULL 的会话）的用户 id 集合"""
    rows = CheckRecord.query.filter(CheckRecord.check_out.is_(None)).all()
    return {r.user_id for r in rows if r.user_id is not None}


@bp.route("/rooms/<room_name>/seats")
@jwt_required()
def room_seats(room_name):
    """
    某自习室的座位列表（含占用状态）—— 前端座位图就调这个，按房间名（如 '106'）。
    occupied = 该座位的绑定用户当前已打卡（有 open session）。
    """
    room = RoomModel.query.filter_by(name=room_name).first()
    if not room:
        return jsonify({"code": 404, "message": "自习室不存在"}), 404

    seats = SeatModel.query.filter_by(room_id=room.id).order_by(SeatModel.id).all()
    open_user_ids = _occupied_user_ids()
    bound_ids = [s.bound_user_id for s in seats if s.bound_user_id]
    user_map = {u.id: u.username for u in UserModel.query.filter(UserModel.id.in_(bound_ids)).all()} if bound_ids else {}

    data = []
    online = 0
    for s in seats:
        occupied = s.bound_user_id in open_user_ids if s.bound_user_id else False
        if occupied:
            online += 1
        data.append({
            "Seat_Id": s.id,
            "Seat_Label": s.label,
            "Bound_User_Id": s.bound_user_id,
            "Bound_User_Name": user_map.get(s.bound_user_id),
            "Occupied": occupied,
        })

    return jsonify({
        "code": 200,
        "message": "获取座位列表成功",
        "Room_Name": room.name,
        "Total_Seats": len(seats),
        "Online": online,
        "seats": data,
    })


@bp.route("/seat_batch_create", methods=["POST"])
@jwt_required()
@check_permission('seat_management')
@audit_log(operation="批量创建座位")
def seat_batch_create():
    """
    为某自习室批量创建座位。Body: { Room_Id, Labels: ['A1','A2',...] }
    已存在的 label 跳过。
    """
    room_id = request.json.get("Room_Id")
    labels = request.json.get("Labels", [])
    if not room_id or not labels:
        return jsonify({"code": 401, "message": "需要提供 Room_Id 和 Labels"}), 401
    if not RoomModel.query.filter_by(id=room_id).first():
        return jsonify({"code": 404, "message": "自习室不存在"}), 404

    existing = {s.label for s in SeatModel.query.filter_by(room_id=room_id).all()}
    created = 0
    for label in labels:
        if label in existing:
            continue
        db.session.add(SeatModel(room_id=room_id, label=label))
        existing.add(label)
        created += 1
    db.session.commit()
    return jsonify({"code": 200, "message": f"批量创建成功，新增 {created} 个座位", "Created": created})


@bp.route("/seat_bind", methods=["POST"])
@jwt_required()
@check_permission('seat_management')
@audit_log(operation="绑定座位")
def seat_bind():
    """把某用户绑定到某座位（固定座位）。Body: { Seat_Id, User_Id }"""
    seat_id = request.json.get("Seat_Id")
    user_id = request.json.get("User_Id")
    if seat_id is None or user_id is None:
        return jsonify({"code": 401, "message": "需要提供 Seat_Id 和 User_Id"}), 401
    seat = SeatModel.query.filter_by(id=seat_id).first()
    if not seat:
        return jsonify({"code": 404, "message": "座位不存在"}), 404
    if not UserModel.query.filter_by(id=user_id).first():
        return jsonify({"code": 404, "message": "用户不存在"}), 404
    # 该用户是否已绑定别的座位（unique 约束兜底，这里先给友好提示）
    other = SeatModel.query.filter_by(bound_user_id=user_id).first()
    if other and other.id != seat.id:
        return jsonify({"code": 403, "message": f"该用户已绑定座位 {other.label}，请先解绑"}), 403
    seat.bound_user_id = user_id
    db.session.commit()
    return jsonify({"code": 200, "message": "座位绑定成功"})


@bp.route("/seat_unbind", methods=["POST"])
@jwt_required()
@check_permission('seat_management')
@audit_log(operation="解绑座位")
def seat_unbind():
    """解除某座位的用户绑定。Body: { Seat_Id }"""
    seat_id = request.json.get("Seat_Id")
    seat = SeatModel.query.filter_by(id=seat_id).first()
    if not seat:
        return jsonify({"code": 404, "message": "座位不存在"}), 404
    seat.bound_user_id = None
    db.session.commit()
    return jsonify({"code": 200, "message": "座位已解绑"})


@bp.route("/seat_delete", methods=["POST"])
@jwt_required()
@check_permission('seat_management')
@audit_log(operation="删除座位")
def seat_delete():
    seat_id = request.json.get("Seat_Id")
    seat = SeatModel.query.filter_by(id=seat_id).first()
    if not seat:
        return jsonify({"code": 404, "message": "座位不存在"}), 404
    db.session.delete(seat)
    db.session.commit()
    return jsonify({"code": 200, "message": "座位删除成功"})

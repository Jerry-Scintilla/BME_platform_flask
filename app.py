import os

from flask import Flask, redirect, jsonify
from werkzeug.middleware.proxy_fix import ProxyFix
import config
from exts import db, mail, limiter, redis_client
from storage import storage
from flask_migrate import Migrate

# 导入蓝图模块
from blueprints import *
from blueprints.attendance_report import init_scheduler, ensure_recipient_permission
from blueprints.ai_topic import init_ai_topic_scheduler, ensure_ai_topic_account, ensure_ai_topic_schema

from flask_cors import CORS

from flask_jwt_extended import JWTManager

from flasgger import Swagger

from flask_redis import FlaskRedis

app = Flask(__name__,
            static_folder=os.path.join(config.DATA_ROOT, 'avatars'),
            static_url_path='/data/avatars')

# 反代后取真实客户端 IP（2026-09-17 修复）：nginx 已传 X-Forwarded-For，但 Flask
# 不信任代理头 → request.remote_addr 恒为 127.0.0.1，flask-limiter 按它限流，
# 全平台共享一个桶——验证码接口 1/minute 沦为"每分钟全站只发一封"（当晚 429 率
# 94.8%）。x_for=1：仅信任本机 nginx 一跳，伪造 XFF 无法提权限流键。
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# 配置CORS（2026-09-16 安全加固）：来源白名单由 CORS_ORIGINS 指定（逗号分隔，
# 含协议与端口，如 https://xxx.example.edu.cn,http://127.0.0.1:8081）。
# 未配置时放行全部并打警告——存量部署兼容，上线环境必须配置。
_cors_origins = [o.strip() for o in (os.getenv("CORS_ORIGINS") or "").split(",") if o.strip()]
if _cors_origins:
    CORS(app, resources={r"/*": {"origins": _cors_origins}}, supports_credentials=True)
else:
    print("[warn] CORS_ORIGINS 未配置，API 暂对所有来源开放——上线环境必须配置白名单")
    CORS(app, supports_credentials=True)

# 绑定配置文件
app.config.from_object(config)
# 拓展初始化
db.init_app(app)
mail.init_app(app)
limiter.init_app(app)
migrate = Migrate(app, db)
jwt = JWTManager(app)


# ── JWT 吊销 blocklist（2026-09-16 安全加固）──
# 登出/刷新轮换的 jti 由 exts.revoke_token 写入 Redis；此处每个带 token 请求校验一次。
# Redis 异常时 fail-open（放行）：吊销是短时效 access(2h) 的补充防线，不能因缓存故障打死全站
@jwt.token_in_blocklist_loader
def _check_if_token_revoked(jwt_header, jwt_payload):
    try:
        return redis_client.get(f"jwt:blocklist:{jwt_payload['jti']}") is not None
    except Exception:
        print("[auth] JWT blocklist 校验异常（Redis 故障），本次放行")
        return False


# ── JWT 错误统一 401（2026-09-16 加固）──
# flask-jwt-extended 默认对缺失/无效/过期/吊销令牌回 422，而前端约定 401=登录失效；
# 统一 401 后，静默续期（401→refresh→重放）与踢下线逻辑才能咬合
def _jwt_error_response(message):
    return jsonify({"code": 401, "message": message}), 401

@jwt.unauthorized_loader
def _jwt_no_token(_reason):
    return _jwt_error_response("未提供访问令牌")

@jwt.invalid_token_loader
def _jwt_bad_token(_reason):
    return _jwt_error_response("访问令牌无效")

@jwt.expired_token_loader
def _jwt_expired_token(_jwt_header, _jwt_payload):
    return _jwt_error_response("访问令牌已过期，请重新登录")

@jwt.revoked_token_loader
def _jwt_revoked_token(_jwt_header, _jwt_payload):
    return _jwt_error_response("登录已失效，请重新登录")
swagger = Swagger(app)
redis_client.init_app(app)
# 对象存储（懒连接，服务未起不影响启动）
storage.init_app(app)

# 蓝图注册
app.register_blueprint(auth_bp)
app.register_blueprint(user_bp)
app.register_blueprint(article_bp)
app.register_blueprint(article_v2_bp)
app.register_blueprint(course_bp)
app.register_blueprint(course_group_bp)
app.register_blueprint(medal_bp)
app.register_blueprint(codecheck_bp)
app.register_blueprint(learningprogress_bp)
app.register_blueprint(information_bp)
app.register_blueprint(permission_bp)
app.register_blueprint(task_bp)
app.register_blueprint(discussion_bp)
app.register_blueprint(community_bp)
app.register_blueprint(llm_bp)
app.register_blueprint(notification_bp)
app.register_blueprint(seat_bp)
app.register_blueprint(attendance_report_bp)
app.register_blueprint(camp_bp)
app.register_blueprint(camp_staff_bp)
app.register_blueprint(camp_announcement_bp)
app.register_blueprint(camp_ms_bp)
app.register_blueprint(camp_project_bp)
app.register_blueprint(camp_delivery_bp)
app.register_blueprint(camp_material_bp)
app.register_blueprint(camp_meeting_bp)
app.register_blueprint(showcase_bp)
app.register_blueprint(admin_bp)
app.register_blueprint(ai_topic_bp)
app.register_blueprint(gratitude_bp)
app.register_blueprint(officers_bp)
app.register_blueprint(organization_bp)
app.register_blueprint(club_admin_bp)
app.register_blueprint(media_bp)
app.register_blueprint(banner_bp)
app.register_blueprint(resource_center_bp)

# 每日出勤报告：幂等创建收件人权限 + 启动定时任务（多 worker 下仅一个生效）
ensure_recipient_permission(app)
init_scheduler(app)
ensure_ai_topic_account(app)
# 幂等补表（checkfirst，对已有表无副作用）：未跑过 migrate_07 的库缺 article_v2，
# 而 AiTopicLedger 外键指向它，不先建表则启动即 1824 崩（连 migrate 脚本都 import 不了 app）
with app.app_context():
    db.create_all()
ensure_ai_topic_schema(app)
init_ai_topic_scheduler(app)


@app.before_request
def _block_banned_users():
    """封禁即全站失效（2026-09-11 用户管理）：带有效 JWT 的请求统一核对 status。

    - 仅拦「token 有效且属于被封禁用户」；无效 token / 无 token 不在此处理，交给各端点自己的 401 语义
    - /auth/ 前缀放行（登录在 auth.py 内单独拒封禁）
    - 代价：每个带 token 的请求多一次 user 查询，当前规模可接受
    """
    from flask import request, jsonify
    auth_header = request.headers.get('Authorization', '')
    if not auth_header.startswith('Bearer ') or request.path.startswith('/auth/'):
        return None
    try:
        from flask_jwt_extended import verify_jwt_in_request, get_jwt_identity
        verify_jwt_in_request()
        identity = get_jwt_identity()
    except Exception:
        return None
    from models import UserModel
    user = UserModel.query.filter_by(email=identity).first()
    if user and (user.status or 'active') == 'banned':
        return jsonify({"code": 403, "message": "账号已被封禁，请联系管理员"}), 403
    return None


@app.route('/')
def hello_world():  # put application's code here
    return redirect('/apidocs')


if __name__ == '__main__':
    # 只开 reloader（改动即自动重启），不开 debug=True 的交互式调试器：host 0.0.0.0 下
    # 调试器控制台等于把任意代码执行暴露给局域网
    app.run(host='0.0.0.0', port=5001, use_reloader=True)
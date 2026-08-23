from flask import Flask, redirect
import config
from exts import db, mail, limiter, redis_client
from flask_migrate import Migrate

# 导入蓝图模块
from blueprints import *
from blueprints.attendance_report import init_scheduler, ensure_recipient_permission
from blueprints.ai_topic import init_ai_topic_scheduler, ensure_ai_topic_account, ensure_ai_topic_schema

from flask_cors import CORS

from flask_jwt_extended import JWTManager

from flasgger import Swagger

from flask_redis import FlaskRedis

app = Flask(__name__, static_folder='./data/avatars', static_url_path='/data/avatars')

# 配置CORS，允许特定域名访问API
CORS(app, supports_credentials=True)

# 绑定配置文件
app.config.from_object(config)
# 拓展初始化
db.init_app(app)
mail.init_app(app)
limiter.init_app(app)
migrate = Migrate(app, db)
jwt = JWTManager(app)
swagger = Swagger(app)
redis_client.init_app(app)

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
app.register_blueprint(homeCover_bp)
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
app.register_blueprint(camp_ms_bp)
app.register_blueprint(admin_bp)
app.register_blueprint(ai_topic_bp)

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


@app.route('/')
def hello_world():  # put application's code here
    return redirect('/apidocs')


if __name__ == '__main__':
    # app.run(debug = True)
    app.run(host='0.0.0.0', port=5001)
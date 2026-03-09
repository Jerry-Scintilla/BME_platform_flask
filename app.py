from flask import Flask, redirect
import config
from exts import db, mail, limiter, redis_client
from flask_migrate import Migrate

# 导入蓝图模块
from blueprints import *

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
app.register_blueprint(course_bp)
app.register_blueprint(course_group_bp)
app.register_blueprint(medal_bp)
app.register_blueprint(codecheck_bp)
app.register_blueprint(learningprogress_bp)
app.register_blueprint(homeCover_bp)
app.register_blueprint(information_bp)
app.register_blueprint(permission_bp)


@app.route('/')
def hello_world():  # put application's code here
    return redirect('/apidocs')


if __name__ == '__main__':
    # app.run(debug = True)
    app.run(host='0.0.0.0', port=5000)
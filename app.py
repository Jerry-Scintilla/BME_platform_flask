from flask import Flask, redirect
import config
from exts import db, mail, limiter
from flask_migrate import Migrate

# 导入蓝图模块
from blueprints.auth import bp as auth_bp
from blueprints.user import bp as user_bp
from blueprints.article import bp as article_bp
from blueprints.course import bp as course_bp
from blueprints.medal import bp as medal_bp


from flask_cors import CORS

from flask_jwt_extended import JWTManager

from flasgger import Swagger

app = Flask(__name__)

CORS(app)
# 绑定配置文件
app.config.from_object(config)
# 拓展初始化
db.init_app(app)
mail.init_app(app)
limiter.init_app(app)
migrate = Migrate(app, db)
jwt = JWTManager(app)
swagger = Swagger(app)

# 蓝图注册
app.register_blueprint(auth_bp)
app.register_blueprint(user_bp)
app.register_blueprint(article_bp)
app.register_blueprint(course_bp)
app.register_blueprint(medal_bp)


@app.route('/')
def hello_world():  # put application's code here
    return redirect('/apidocs')


if __name__ == '__main__':
    app.run()
    # app.run(host='0.0.0.0', port=5000)

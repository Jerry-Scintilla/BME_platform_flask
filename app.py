from flask import Flask
import config
from exts import db
from flask_migrate import Migrate
from blueprints.auth import bp as auth_bp
from flask_cors import CORS

from flask_jwt_extended import JWTManager

app = Flask(__name__)
CORS(app)
#绑定配置文件
app.config.from_object(config)

db.init_app(app)

migrate = Migrate(app, db)

# 初始化JWT扩展
jwt = JWTManager(app)

app.register_blueprint(auth_bp)



@app.route('/')
def hello_world():  # put application's code here
    return 'Hello World!'


if __name__ == '__main__':
    app.run()

# 数据库配置信息
from datetime import timedelta

HOSTNAME = '127.0.0.1'
PORT = '3306'
DATABASE = 'sysu_bme'
USERNAME = 'root'
PASSWORD = 'root'
DB_URI = 'mysql+pymysql://{}:{}@{}:{}/{}?charset=utf8mb4'.format(USERNAME, PASSWORD, HOSTNAME, PORT, DATABASE)

SQLALCHEMY_DATABASE_URI = DB_URI

# JWT密匙
JWT_SECRET_KEY = 'askdcjhaijbdsadf'
JWT_ACCESS_TOKEN_EXPIRES = timedelta(days=7)

# 邮箱授权码
# MBWa73BLhWMgkEmJ

# 邮箱配置
MAIL_SERVER = "smtp.163.com"
MAIL_USE_SSL = True
MAIL_PORT = 465
MAIL_USERNAME = "jerry_scintilla@163.com"
MAIL_PASSWORD = "MBWa73BLhWMgkEmJ"
MAIL_DEFAULT_SENDER = "jerry_scintilla@163.com"

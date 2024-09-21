#数据库配置信息

HOSTNAME = '127.0.0.1'
PORT = '3306'
DATABASE = 'sysu_bme'
USERNAME = 'root'
PASSWORD = 'root'
DB_URI = 'mysql+pymysql://{}:{}@{}:{}/{}?charset=utf8mb4'.format(USERNAME, PASSWORD, HOSTNAME, PORT, DATABASE)

SQLALCHEMY_DATABASE_URI = DB_URI


# JWT密匙
JWT_SECRET_KEY = 'askdcjhaijbdsadf'
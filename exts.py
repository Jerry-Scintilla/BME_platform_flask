# flask-sqlalchemy
from flask_sqlalchemy import SQLAlchemy
# flask_mail
from flask_mail import Mail
# flask_limiter
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

db = SQLAlchemy()

mail = Mail()

limiter = Limiter(get_remote_address)

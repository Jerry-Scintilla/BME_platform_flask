from flask import Blueprint, request, redirect, jsonify
from wtforms.validators import email

from exts import db

# 导入数据库表
from models import ArticleModel ,UserModel

# 导入表单验证
from .forms import ArticleForm

# 导入token验证模块
from flask_jwt_extended import (create_access_token, get_jwt_identity, jwt_required, JWTManager)
bp = Blueprint("article", __name__, url_prefix="")

@bp.route("/article/public", methods=["POST"])
@jwt_required()
def article_public():
    form = ArticleForm()
    if form.validate():
        title = form.Article_Title.data
        introduction = form.Article_Introduction.data

        User_Email = get_jwt_identity()
        user = UserModel.query.filter_by(email=User_Email).first()
        author_id = user.id

        article = ArticleModel(title=title, introduction=introduction, author_id=author_id)

        db.session.add(article)
        db.session.commit()
        data = {
            "code": 200,
            "message": "文章信息存储成功",
        }
        return jsonify(data)
    else:
        data = {
            "code": 400,
            "message": form.errors,
        }
        return jsonify(data)


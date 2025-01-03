from flask import Blueprint, request, redirect, jsonify, send_file
from wtforms.validators import email
import calendar, time, os

# 导入拓展
from exts import db

# 导入数据库表
from models import ArticleModel, UserModel

# 导入表单验证
from .forms import ArticleForm

# 导入token验证模块
from flask_jwt_extended import (create_access_token, get_jwt_identity, jwt_required, JWTManager)

# 导入api文档模块
from flasgger import swag_from

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
        # 获取文章id
        db.session.flush()
        db.session.refresh(article)
        # print(article.id)
        db.session.commit()

        data = {
            "code": 200,
            "message": "文章信息存储成功",
            "Article_Id": article.id,
        }
        return jsonify(data)
    else:
        data = {
            "code": 400,
            "message": form.errors,
        }
        return jsonify(data)


@bp.route("/article/detail", methods=["POST"])
@jwt_required()
@swag_from('../apidocs/article/article_detail.yaml')
def article_detail():
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    mode = user.user_mode
    # print(mode)
    if mode != 'admin':
        return jsonify({
            "code": 401,
            'message': "用户权限不够"
        })

    file = request.files['Article_Content']
    article_id = request.form.get('Article_Id')
    if file is None:  # 表示没有发送文件
        return jsonify({
            "code": 400,
            'message': "没有发送文件"
        })

    article = ArticleModel.query.filter_by(id=article_id).first()
    url = article.url
    if url:
        os.remove('./data/article/' + url)

    article_name = article_id + '_' + file.filename
    file.save('./data/article/' + article_name)

    ArticleModel.query.filter_by(id=article_id).update({'url': article_name})
    db.session.commit()

    return jsonify({
        "code": 200,
        'message': "文件上传完成"
    })


@bp.route("/article/delete", methods=["POST"])
@jwt_required()
@swag_from('../apidocs/article/article_delete.yaml')
def article_delete():
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    mode = user.user_mode
    # print(mode)
    if mode != 'admin':
        return jsonify({
            "code": 401,
            'message': "用户权限不够"
        })

    data = request.get_json()
    article_id = data['Article_Id']
    article = ArticleModel.query.filter_by(id=article_id).first()

    if article is None:
        return jsonify({
            "code": 400,
            'message': "找不到该文章"
        })

    url = article.url
    os.remove('./data/article/' + url)

    db.session.delete(article)
    db.session.commit()

    return jsonify({
        "code": 200,
        'message': "文章删除成功"
    })


@bp.route("/article/list")
@swag_from('../apidocs/article/article_list.yaml')
def article_list():
    a_list = ArticleModel.query.all()
    data = []
    for article in a_list:
        b_list = {'Article_Title': article.title,
                  'Article_Introduction': article.introduction,
                  'Article_Time': article.publish_time.strftime('%Y-%m-%d %H:%M:%S'),
                  'Article_Id': article.id,
                  }
        data.append(b_list)

    return jsonify(data)


# 文章内容发送（html）
@bp.route("/article")
@swag_from('../apidocs/article/article.yaml')
def article():
    article_id = request.args.get('Article_Id')
    if article_id is None:
        return jsonify({
            "code": 400,
            "message": '请求错误，请重试'
        })
    article = ArticleModel.query.filter_by(id=article_id).first()
    path = article.url
    article_path = './data/article/' + path
    # print(article_path)
    return send_file(article_path)

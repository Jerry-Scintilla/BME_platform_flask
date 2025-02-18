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
@swag_from('../apidocs/article/article_public.yaml')
def article_public():
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    mode = user.user_mode
    if mode != 'admin':
        return jsonify({
            "code": 400,
            'message': "用户权限不够"
        }), 400
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
        return jsonify(data), 400


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
        }), 401

    file = request.files['Article_Content']
    article_id = request.form.get('Article_Id')
    if file is None:  # 表示没有发送文件
        return jsonify({
            "code": 400,
            'message': "没有发送文件"
        }), 400

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

# 创建文章详情（以json格式接收html）
@bp.route("/article/detail_json", methods=["POST"])
@jwt_required()
@swag_from('../apidocs/article/article_detail_json.yaml')
def article_detail_json():
    user_email = get_jwt_identity()
    user = UserModel.query.filter_by(email=user_email).first()
    mode = user.user_mode
    # print(mode)
    if mode != 'admin':
        return jsonify({
            "code": 401,
            'message': "用户权限不够"
        }), 401
    try:
        data = request.get_json()
        html_content = data.get('Html')
        article_id = data.get('Article_Id')

        if not html_content:
            return jsonify({
                "code": 400,
                'message': '没有发送Html内容'
            }), 400
        article = ArticleModel.query.filter_by(id=article_id).first()
        url = article.url
        name = article.title
        if url:
            os.remove('./data/article/' + url)

        article_name = article_id + '_' + name
        file_path = os.path.join('./data/article', f"{article_name}.html")
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(html_content)

        ArticleModel.query.filter_by(id=article_id).update({'url': article_name + '.html'})
        db.session.commit()

        return jsonify({
            "code": 200,
            'message': '文件上传完成',
        }), 200

    except Exception as e:
        return jsonify({
            "code": 402,
            'message': str(e)
        }), 402



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
        }), 401

    data = request.get_json()
    article_id = data['Article_Id']
    article = ArticleModel.query.filter_by(id=article_id).first()

    if article is None:
        return jsonify({
            "code": 400,
            'message': "找不到该文章"
        }), 400

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
                  'Article_Author': article.author.username,
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
            "message": '传参格式错误'
        }), 400
    article = ArticleModel.query.filter_by(id=article_id).first()
    if article is None:
        return jsonify({
            "code": 401,
            "message": '文章不存在'
        }), 401
    path = article.url
    article_path = './data/article/' + path
    # print(article_path)
    # return send_file(article_path)
    with open(article_path, 'r', encoding='utf-8') as file:
        html_content = file.read()
    return jsonify({
        "code": 200,
        "message": "获取文章详情成功",
        "Article_Id": article_id,
        "Article_Title": article.title,
        "Article_Author": article.author.username,
        "Publish_Time": article.publish_time.strftime('%Y-%m-%d %H:%M:%S'),
        "html_content": html_content
    })
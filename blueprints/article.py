from flask import Blueprint, request, redirect, jsonify, send_file
from wtforms.validators import email
import calendar, time, os

# 导入拓展
from exts import db

# 导入数据库表
from models import ArticleModel, UserModel, ArticleComment

# 导入表单验证
from .forms import ArticleForm

# 导入token验证模块
from flask_jwt_extended import (create_access_token, get_jwt_identity, jwt_required, JWTManager)

# 导入api文档模块
from flasgger import swag_from

# 导入权限检查模块
from . import check_permission, audit_log

bp = Blueprint("article", __name__, url_prefix="")

# 创建文章简介
@bp.route("/article/public", methods=["POST"])
@jwt_required()
@check_permission('article_management')
@audit_log(operation="创建文章")
@swag_from('../apidocs/article/article_public.yaml')
def article_public():
    form = ArticleForm()
    if form.validate():
        title = form.Article_Title.data
        introduction = form.Article_Introduction.data
        Html = form.Html.data

        User_Email = get_jwt_identity()
        user = UserModel.query.filter_by(email=User_Email).first()
        author_id = user.id

        article = ArticleModel(title=title, introduction=introduction, author_id=author_id)
        db.session.add(article)
        db.session.flush()
        db.session.refresh(article)

        article_id = article.id

        try:
            html_content = Html
            if not html_content:
                return jsonify({
                    "code": 403,
                    'message': '没有发送Html内容'
                }), 400
            url = article.url
            name = title
            if url:
                os.remove('./data/article/' + url)

            article_name = str(article_id) + '_' + name
            file_path = os.path.join('./data/article', f"{article_name}.html")
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(html_content)

            ArticleModel.query.filter_by(id=article_id).update({'url': article_name + '.html'})
            db.session.commit()

            return jsonify({
                "code": 200,
                'message': '文章信息存储成功',
                "Article_Id": article.id,
                "Article_Title": title,
                "Article_Introduction": introduction,
            }), 200

        except Exception as e:
            return jsonify({
                "code": 402,
                'message': str(e)
            }), 402

    else:
        data = {
            "code": 400,
            "message": form.errors,
        }
        return jsonify(data), 400


@bp.route("/article/detail", methods=["POST"])
@jwt_required()
@check_permission('article_management')
@swag_from('../apidocs/article/article_detail.yaml')
def article_detail():
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
@check_permission('article_management')
@swag_from('../apidocs/article/article_detail_json.yaml')
def article_detail_json():
    try:
        data = request.get_json()
        html_content = data.get('Html')
        article_id = data.get('Article_Id')
        article_title = data.get('Article_Title')
        introduction = data.get('Article_Introduction')

        if not html_content:
            return jsonify({
                "code": 400,
                'message': '没有发送Html内容'
            }), 400
        article = ArticleModel.query.filter_by(id=article_id).first()
        url = article.url
        name = article_title
        if url:
            os.remove('./data/article/' + url)

        article_name = article_id + '_' + name
        file_path = os.path.join('./data/article', f"{article_name}.html")
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(html_content)

        ArticleModel.query.filter_by(id=article_id).update({'url': article_name + '.html', 'title': article_title, 'introduction': introduction})
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
@check_permission('article_management')
@audit_log(operation="删除文章")
@swag_from('../apidocs/article/article_delete.yaml')
def article_delete():
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

    # 删除文章统计量
    comments = ArticleComment.query.filter_by(article_id=article_id).all()
    for comment in comments:
        db.session.delete(comment)
        db.session.commit()
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
        "Article_Introduction": article.introduction,
        "html_content": html_content
    })


@bp.route("/article/edit", methods=["POST"])
@jwt_required()
@check_permission('article_management')
@audit_log(operation="编辑文章")
@swag_from('../apidocs/article/article_edit.yaml')
def article_edit():
    article_id = request.json.get('Article_Id')
    article_title = request.json.get('Article_Title')
    article_introduction = request.json.get('Article_Introduction')

    article = ArticleModel.query.filter_by(id=article_id).first()
    if article is None:
        return jsonify({
            "code": 400,
            "message": '文章不存在'
        }), 400
    article.title = article_title
    article.introduction = article_introduction
    db.session.commit()
    return jsonify({
        "code": 200,
        "message": "文章编辑成功"
    })


@bp.route("/article/statistic", methods=["GET", "POST"])
@jwt_required(optional=True)
@swag_from('../apidocs/article/article_statistic.yaml')
def article_statistic():
    if request.method == 'GET':
        # 获取文章ID
        article_id = request.args.get('Article_Id')
        user = request.args.get('user')
        if not article_id:
            return jsonify({
                "code": 400,
                "message": "缺少Article_Id参数"
            }), 400
        # 查询统计数据
        article_ = ArticleModel.query.filter_by(id=article_id).first()
        if article_ is None:
            return jsonify({
                "code": 400,
                "message": "文章不存在"
            }), 400
        comments = ArticleComment.query.filter_by(article_id=article_id).all()
        view_count = len(comments)
        like_count = sum(1 for comment in comments if comment.like_time is not None)

        # 初始化用户相关状态
        user_viewed = False
        user_liked = False

        if user:
            user_email = get_jwt_identity()
            user_ = UserModel.query.filter_by(email=user_email).first()
            if user_:
                # 查询用户对该文章的记录
                user_comment = ArticleComment.query.filter_by(
                    article_id=article_id,
                    user_id=user_.id
                ).first()

                if user_comment:
                    user_viewed = user_comment.view_time is not None
                    user_liked = user_comment.like_time is not None
        return jsonify({
            "code": 200,
            "message": "统计数据获取成功",
            "view_count": view_count,
            "like_count": like_count,
            "user_viewed": user_viewed,
            "user_liked": user_liked
        }), 200

    else:
        # POST请求处理
        user_email = get_jwt_identity()
        if not user_email:
            return jsonify({
                "code": 401,
                "message": "用户未认证"
            }), 401
            
        user = UserModel.query.filter_by(email=user_email).first()
        if not user:
            return jsonify({
                "code": 401,
                "message": "用户不存在"
            }), 401

        like = request.json.get('like')
        view = request.json.get('view')
        article_id = request.json.get('Article_Id')

        article_ = ArticleModel.query.filter_by(id=article_id).first()
        if article_ is None:
            return jsonify({
                "code": 400,
                "message": "文章不存在"
            }), 400

        if view:
            # 先检查是否已存在浏览记录
            existing_comment = ArticleComment.query.filter_by(
                article_id=article_id,
                user_id=user.id
            ).first()
            if not existing_comment:
                view_time = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
                article_comment = ArticleComment(view_time=view_time, article_id=article_id, user_id=user.id)
                db.session.add(article_comment)
                db.session.commit()

        article_comment = ArticleComment.query.filter_by(article_id=article_id, user_id=user.id).first()
        article_comment.like_time = None
        db.session.commit()
        if like:
            like_time = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
            statistic = ArticleComment.query.filter_by(article_id=article_id, user_id=user.id).first()
            if statistic is None:
                return jsonify({
                    "code": 400,
                    "message": '不能先点赞再浏览文章'
                }), 400
            statistic.like_time = like_time
            db.session.commit()
            
        return jsonify({
            "code": 200,
            "message": "文章统计成功"
        }), 200


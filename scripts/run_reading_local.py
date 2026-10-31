"""Run real platform blueprints against an isolated local SQLite database.

No remote database, email, scheduler or object-storage service is used.
Install requirements plus fakeredis, then run this file from the backend root.
"""
import argparse
import hashlib
import io
import os
import secrets
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault('JWT_SECRET', 'local-reading-only-' + secrets.token_hex(32))


def create_local_app(db_path=None, seed=True):
    from flask import Flask, jsonify
    from flask_cors import CORS
    from flask_jwt_extended import JWTManager
    from sqlalchemy.ext.compiler import compiles
    from sqlalchemy.dialects.mysql import LONGTEXT, MEDIUMTEXT
    import fakeredis
    import config
    from exts import db, mail, redis_client, limiter
    from storage import storage

    @compiles(LONGTEXT, 'sqlite')
    @compiles(MEDIUMTEXT, 'sqlite')
    def sqlite_longtext(_type, _compiler, **_kw):
        return 'TEXT'

    local_dir = ROOT / 'data' / 'reading-local'
    local_dir.mkdir(parents=True, exist_ok=True)
    (ROOT / 'log').mkdir(exist_ok=True)
    db_path = str(db_path or local_dir / 'community.sqlite')
    app = Flask('reading_local')
    app.config.from_object(config)
    app.config.update(SQLALCHEMY_DATABASE_URI='sqlite:///' + db_path.replace('\\', '/'),
        SQLALCHEMY_ENGINE_OPTIONS={'connect_args': {'timeout': 30}},
        JWT_SECRET_KEY=os.environ['JWT_SECRET'], JWT_TOKEN_LOCATION=['headers'],
        RATELIMIT_STORAGE_URI='memory://', RATELIMIT_ENABLED=False,
        REDIS_URL='redis://localhost:6379/15', STORAGE_BACKEND='local', DATA_ROOT=str(local_dir),
        DEV_TEST_ACCOUNTS=True, AUTH_REFRESH_COOKIE_ENABLED=False, WTF_CSRF_ENABLED=False,
        MAIL_SUPPRESS_SEND=True, TESTING=False)
    db.init_app(app)
    mail.init_app(app)
    limiter.init_app(app)
    redis_client.provider_class = fakeredis.FakeRedis
    redis_client.init_app(app)
    jwt = JWTManager(app)
    CORS(app, supports_credentials=True)
    storage.init_app(app)
    import blueprints
    for name in blueprints.__all__:
        app.register_blueprint(getattr(blueprints, name))
    from blueprints.points import bp as points_bp
    app.register_blueprint(points_bp)
    from services.request_guard import enforce_request_access
    app.before_request(enforce_request_access)

    @jwt.unauthorized_loader
    def missing_token(reason):
        return jsonify(code=401, message='请先登录', machine='ACCESS_TOKEN_MISSING'), 401

    @jwt.invalid_token_loader
    def invalid_token(reason):
        return jsonify(code=401, message='登录凭据无效', machine='INVALID_TOKEN'), 401

    @app.get('/local/health')
    def health():
        return jsonify(code=200, database='isolated-sqlite', runtime='real-platform-blueprints')

    with app.app_context():
        db.create_all()
        from scripts.migrate.migrate_83_community_discovery import apply as apply_discovery
        apply_discovery(db.engine)
        if seed:
            seed_reading(local_dir)
            seed_community_discovery()
    return app


def seed_reading(local_dir):
    import json
    from PIL import Image, ImageDraw, ImageFont
    from exts import db
    from models import (UserModel, PersonModel, CommunityTag, CommunityBook,
                        DiscussionThread as Thread, DiscussionReply as Reply)
    from services.identity.person import create_provisional
    from services.community_reading import normalize_book
    if CommunityTag.query.filter_by(name='秋日阅读周').first():
        return
    users = []
    for index, name in enumerate(('阅读周管理员', '林知遥', '陈嘉树', '周启航', '许清和')):
        user = UserModel(username=name, email=('admin' if index == 0 else f'stu{index}') + '@seed.dev',
                         role='super_admin' if index == 0 else 'user', student_id=20230000 + index)
        user.set_password(hashlib.md5(b'12345678').hexdigest())
        db.session.add(user)
        db.session.flush()
        person = create_provisional(user)
        person = person if isinstance(person, PersonModel) else db.session.get(PersonModel, user.person_id)
        person.verified_name = name
        person.verification_status = 'verified'
        if index == 0:
            user.admin_tag = 'teacher'
        users.append(user)
    reading = CommunityTag(name='秋日阅读周', description='翻开一本书，遇见另一种生活。分享你的阅读感受，也听听不同的声音。',
                           is_reading=True, is_active=True, suggested_words=500, created_by=users[0].id)
    empty = CommunityTag(name='冬日共读计划', description='下一次共读，从你的第一篇分享开始。', is_reading=True,
                         is_active=True, suggested_words=500, created_by=users[0].id)
    tag = CommunityTag(name='学习手记', description='记录值得留下的想法', is_active=True, created_by=users[0].id)
    db.session.add_all([reading, empty, tag])
    db.session.flush()
    try:
        title_font = ImageFont.truetype('C:/Windows/Fonts/msyh.ttc', 54)
        small_font = ImageFont.truetype('C:/Windows/Fonts/msyh.ttc', 24)
    except OSError:
        title_font = small_font = ImageFont.load_default()
    intro = [
        '读《乡土中国》，最让我停下来思考的是，许多习以为常的人际相处方式，都与我们生活的环境有关。熟人社会依靠长久的共同生活建立信任，而当我们进入大学，来到陌生的城市，过去的经验有时就需要重新理解。书中的概念并不只属于遥远的乡村，它们也帮助我观察宿舍、班级和家庭之间不同的关系。',
        '读完《活着》，我没有立即合上书。那些朴素的叙述让我重新注意到日常生活里容易被忽略的人：家人平常的问候，一顿一起吃的晚饭，以及朋友愿意听我说话的时间。我理解的坚韧并不是永远乐观，而是在难过的时候，仍然为生活留下一个小小的位置。',
        '《被讨厌的勇气》让我意识到，我常常把他人的评价提前放进自己的决定里。在选择课程、参加活动甚至提出一个问题之前，我会先猜测别人怎么看。读书并没有让这种顾虑突然消失，却给了我一个练习的方向：先分清哪些事情是自己的责任，再认真作出选择。',
    ]
    more = '我尝试把书里的问题带回自己的经历，而不是只记住结论。有一次小组讨论中，我们对同一件事产生了完全不同的理解。过去我可能急着证明自己正确，现在更愿意先问一句：你的经验是什么？这种改变很小，却让交流变得具体。阅读的意义也许就在这里，它让我们暂时走出熟悉的立场，看见别人所处的位置。对我来说，写心得不是复述简介，而是留下自己曾经认真思考过的痕迹。也期待在评论里看到不同的看法，一起把这个问题继续聊下去。'
    specs = [('乡土中国', '费孝通', '#E8DFC8', '#4B513E'), ('活着', '余华', '#D6DFE2', '#394B51'),
             ('被讨厌的勇气', '岸见一郎 / 古贺史健', '#E6D6CC', '#665044'), ('小王子', '圣埃克苏佩里', '#DBDFD0', '#465344')]
    topics = []
    for index, (title, author, background, ink) in enumerate(specs):
        book = CommunityBook(tag_id=reading.id, title=title, author=author, normalized_title=normalize_book(title))
        db.session.add(book)
        db.session.flush()
        if index == 3:
            continue
        image = Image.new('RGB', (960, 640), background)
        draw = ImageDraw.Draw(image)
        draw.rectangle((72, 58, 887, 581), outline=ink, width=2)
        draw.text((112, 107), 'READING WEEK / 2026', fill=ink, font=small_font)
        draw.text((112, 248), title, fill=ink, font=title_font)
        draw.text((115, 345), author, fill=ink, font=small_font)
        draw.line((112, 464, 410, 464), fill=ink, width=2)
        draw.text((112, 493), '把阅读变成一次相遇', fill=ink, font=small_font)
        path = local_dir / 'storage' / 'media' / 'discussions' / 'reading-local' / f'book-{index+1}.webp'
        path.parent.mkdir(parents=True, exist_ok=True)
        image.save(path, 'WEBP', quality=90)
        thread = Thread(title=['在熟悉的关系里，重新认识自己', '平凡的日子，也有自己的重量', '从在意评价，到认真作出选择'][index],
                        content=intro[index] + more * 2, author_id=users[index+1].id, scope_type='global',
                        category='share', book_id=book.id, tags=[reading], is_essence=index == 0,
                        images_json=json.dumps([f'/media/discussions/reading-local/book-{index+1}.webp']),
                        like_count=18-index*5, view_count=128-index*31,
                        created_at=datetime.now()-timedelta(hours=7-index))
        db.session.add(thread)
        db.session.flush()
        topics.append(thread)
    for index in range(25):
        top = Reply(thread_id=topics[0].id, author_id=users[index%4+1].id,
                    content=(intro[index%3] + more * 2) if index < 3 else f'第 {index+1} 条交流：我也想从身边的生活出发，重新理解书中讨论的人与关系。',
                    created_at=datetime.now()-timedelta(minutes=120-index))
        db.session.add(top)
        db.session.flush()
        if index == 0:
            parent = top
            for child_index in range(15):
                child = Reply(thread_id=topics[0].id, author_id=users[(child_index+1)%4+1].id,
                    content=f'这段观察很有共鸣。第 {child_index+1} 个补充：有时候先理解彼此的处境，就能让讨论多一些可能。',
                    parent_reply_id=parent.id, root_reply_id=top.id,
                    created_at=datetime.now()-timedelta(minutes=90-child_index))
                db.session.add(child)
                db.session.flush()
                parent = child
    topics[0].reply_count = 40
    for thread in topics[1:]:
        db.session.add(Reply(thread_id=thread.id, author_id=users[1].id, content=intro[0] + more))
        thread.reply_count = 1
    db.session.add(Thread(title='把每天的小发现，写进学习手记', content='最近开始用十分钟复盘一天。记录一个问题、一点进展，也记录还没有弄懂的地方。欢迎分享你的方法。',
                          author_id=users[4].id, scope_type='global', category='share', tags=[tag], like_count=6))
    db.session.commit()


def seed_community_discovery():
    """Add varied local examples without replacing existing posts or user edits."""
    from exts import db
    from models import CommunityTag, CommunityVisit, UserModel, DiscussionThread as Thread, DiscussionReply as Reply
    users = UserModel.query.filter(UserModel.email.in_([f'stu{i}@seed.dev' for i in range(1, 5)])).order_by(UserModel.id).all()
    if len(users) != 4:
        return
    examples = [
        ('项目实践', '第一次做硬件项目，怎样分工才不会互相等进度？', '我们几个人准备做一个小型环境监测装置，已经拆好了传感器、通信和外壳三个部分。想听听大家是怎么安排接口联调和进度同步的，有没有踩过的坑可以提前提醒？', 'ask', True),
        ('学习手记', '分享一个实验记录方法：把失败的过程也留下来', '以前只记最后成功的步骤，换个条件就很难复现。最近开始记录每次改动、当时的判断和实际结果。虽然多写几行，下次排查问题时却省下了不少时间。你们有没有顺手的记录方法？', 'share', True),
        ('校园日常', '周末想在学校附近走走，有什么适合散步的路线？', '最近天气适合出门，想找一条一两个小时能走完的路线。安静的小路、能看夕阳的地方都可以，欢迎分享你常去的角落。', 'chat', False),
        ('项目实践', '一起做个小工具：把课程资料整理得更好找', '想找同学一起做一个轻量资料索引，先解决文件命名和关键词检索。前端、后端或者只想提需求都欢迎，我们先从最常用的一个功能开始。', 'recruit', False),
        ('答疑互助', '串口数据偶尔丢一段，大家通常从哪里排查？', '开发板和电脑通信时，大多数时候正常，连续发送后偶尔缺几行。目前检查了波特率和接线，还准备看一下缓冲区与发送间隔。欢迎分享排查思路。', 'ask', False),
        ('校园日常', '你最近做成的一件小事是什么？', '不一定是很大的成果。一次按计划完成的实验、终于看懂的一道题，或者坚持早起的一周，都值得记录。来交换一点最近的小进展吧。', 'chat', False),
    ]
    for index, (tag_name, title, content, category, featured) in enumerate(examples):
        if Thread.query.filter_by(title=title, scope_type='global').first():
            continue
        tag = CommunityTag.query.filter_by(name=tag_name).first()
        if not tag:
            tag = CommunityTag(name=tag_name, is_active=True, created_by=users[0].id)
            db.session.add(tag)
        topic = Thread(title=title, content=content, author_id=users[index % 4].id, scope_type='global', category=category,
            tags=[tag], is_essence=featured, featured_at=datetime.now() - timedelta(minutes=index) if featured else None,
            like_count=12 + index * 2, created_at=datetime.now()-timedelta(hours=2 + index), reply_count=2,
            last_reply_at=datetime.now()-timedelta(minutes=10 + index))
        db.session.add(topic)
        db.session.flush()
        for step in range(2):
            db.session.add(Reply(thread_id=topic.id, author_id=users[(index + step + 1) % 4].id,
                content=['这个问题我也遇到过，可以先把最小可复现的过程整理出来，一起看看。', '谢谢分享，我也想试试这个方法，有后续进展再来交流。'][step],
                created_at=datetime.now()-timedelta(minutes=20 + index + step)))
        for user in users:
            if user.id != topic.author_id:
                db.session.add(CommunityVisit(thread_id=topic.id, user_id=user.id, last_seen_at=datetime.now()-timedelta(minutes=index)))
    for topic in Thread.query.filter(Thread.book_id.isnot(None)).all():
        for user in users:
            if user.id != topic.author_id and not db.session.get(CommunityVisit, (topic.id, user.id)):
                db.session.add(CommunityVisit(thread_id=topic.id, user_id=user.id, last_seen_at=datetime.now()-timedelta(minutes=30)))
    db.session.commit()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=5001)
    parser.add_argument('--database', type=Path)
    args = parser.parse_args()
    app = create_local_app(args.database)
    print(f'Local reading API: http://127.0.0.1:{args.port}; accounts: admin@seed.dev, stu1@seed.dev ... stu4@seed.dev')
    app.run(host='127.0.0.1', port=args.port, use_reloader=False)

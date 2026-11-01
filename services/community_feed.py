"""Portable SQL-paginated mixed feed, shared by MySQL and the local runner."""
import json
from datetime import datetime, timedelta
from sqlalchemy import select, func, case, literal, and_, or_, union_all
from sqlalchemy.orm import joinedload, selectinload
from exts import db
from models import (DiscussionThread as Thread, DiscussionReply as Reply, UserModel as User,
                    ArticleModel, ArticleV2Model, CommunityTag, ShowcaseProject)
from services import community_reading as reading


def cover_thumbnail(key):
    if not key or '.' not in key:
        return key
    # Match the existing media naming contract used by article and project cards.
    return key.split('.', 1)[0] + '_thumb.' + key.rsplit('.', 1)[1]


def build_page(args, user):
    page, size = reading.page_values(args)
    kind, sort = args.get('type', 'all'), args.get('sort', 'hot')
    if kind not in ('all', 'article', 'discussion') or sort not in ('hot', 'latest'):
        raise reading.CommunityError('内容类型或排序方式无效')
    tag_id = None
    if args.get('tag_id'):
        try:
            tag_id = int(args['tag_id'])
        except (ValueError, TypeError):
            raise reading.CommunityError('标签格式错误')
        if tag_id <= 0 or not db.session.get(CommunityTag, tag_id):
            raise reading.CommunityError('标签不存在', 404)
    from blueprints.discussion import THREAD_CATEGORIES
    category = args.get('category')
    if category and category not in THREAD_CATEGORIES:
        raise reading.CommunityError('分类无效')
    search = reading.text_value(args.get('q', ''), '搜索词', 0, 100)
    now = datetime.now()
    selects = []
    if kind != 'article':
        rank = func.coalesce(Thread.last_reply_at, Thread.created_at)
        pin = case((and_(Thread.is_pinned.is_(True), or_(Thread.pinned_until.is_(None), Thread.pinned_until > now)), 1), else_=0)
        query = select(literal('discussion').label('kind'), Thread.id.label('id'), rank.label('rank_dt'),
                       pin.label('pin'), Thread.reply_count.label('rc'), Thread.like_count.label('lc'),
                       Thread.view_count.label('vc'), Thread.is_essence.label('essence'),
                       User.role.label('role'), literal(14).label('half_life'),
                       (Thread.like_count + 2 * Thread.reply_count).label('interaction'),
                       Thread.id.label('like_tid')).join(User, User.id == Thread.author_id).where(
                           Thread.scope_type == 'global',
                           Thread.status != 'deleted' if user.is_admin() else Thread.status.in_(reading.PUBLIC_STATUSES))
        if tag_id:
            query = query.where(Thread.tags.any(CommunityTag.id == tag_id))
        if category:
            query = query.where(Thread.category == category)
        if search:
            from models import CommunityBook
            query = query.where(or_(
                and_(Thread.author_closed_at.is_(None), Thread.status != 'closed',
                     or_(Thread.title.contains(search, autoescape=True), Thread.content.contains(search, autoescape=True))),
                Thread.book.has(CommunityBook.title.contains(search, autoescape=True))))
        selects.append(query)
    if kind != 'discussion' and not tag_id and not category:
        for scope, model in (('article', ArticleModel), ('article_v2', ArticleV2Model)):
            agg = select(Thread.scope_id.label('scope_id'), func.sum(Thread.reply_count).label('rc'),
                         func.sum(Thread.like_count).label('lc'), func.sum(Thread.view_count).label('vc'),
                         func.max(Thread.last_reply_at).label('last'), func.max(Thread.id).label('tid')).where(
                             Thread.scope_type == scope, Thread.status == 'normal').group_by(Thread.scope_id).subquery()
            rc, lc, vc = (func.coalesce(agg.c[c], 0) for c in ('rc', 'lc', 'vc'))
            v2 = scope == 'article_v2'
            query = select(literal(scope).label('kind'), model.id.label('id'),
                           func.coalesce(agg.c.last, model.publish_time).label('rank_dt'), literal(0).label('pin'),
                           rc.label('rc'), (lc if v2 else literal(0)).label('lc'),
                           (vc if v2 else literal(0)).label('vc'),
                           (model.is_essence if v2 else literal(False)).label('essence'), User.role.label('role'),
                           literal(30).label('half_life'),
                           (lc + 2 * rc + .3 * vc if v2 else 2 * rc).label('interaction'),
                           (agg.c.tid if v2 else literal(None)).label('like_tid')).join(User, User.id == model.author_id).outerjoin(agg, agg.c.scope_id == model.id)
            if v2:
                query = query.where(model.status == 'published', model.is_official.is_(False))
            if search:
                query = query.where(or_(model.title.contains(search, autoescape=True), model.introduction.contains(search, autoescape=True)))
            selects.append(query)
    if not selects:
        return dict(code=200, data=[], total=0, pages=0, page=page, per_page=size)
    feed = union_all(*selects).subquery()
    if db.engine.dialect.name == 'sqlite':
        age = func.max(0.0, func.julianday(now) - func.julianday(feed.c.rank_dt))
    else:
        age = func.greatest(0.0, func.timestampdiff(db.text('SECOND'), feed.c.rank_dt, now) / 86400.0)
    score = (feed.c.interaction + 1 + case((feed.c.rank_dt > now - timedelta(hours=48), 3), else_=0)) * func.pow(.5, age / feed.c.half_life)
    score *= case((feed.c.role == 'super_admin', 1.5), else_=1.) * case((feed.c.essence.is_(True), 2.), else_=1.)
    order = [feed.c.pin.desc()]
    if sort == 'hot':
        order.extend([score.desc(), feed.c.rc.desc()])
    order.extend([feed.c.rank_dt.desc(), feed.c.rc.desc(), feed.c.kind, feed.c.id.desc()])
    total = db.session.scalar(select(func.count()).select_from(feed))
    ranks = db.session.execute(select(feed).order_by(*order).limit(size).offset((page - 1) * size)).mappings().all()
    entities = {}
    for key, model in (('discussion', Thread), ('article', ArticleModel), ('article_v2', ArticleV2Model)):
        ids = [r['id'] for r in ranks if r['kind'] == key]
        if ids:
            query = model.query.options(joinedload(model.author)).filter(model.id.in_(ids))
            if model is Thread:
                query = query.options(selectinload(Thread.tags))
            entities[key] = {e.id: e for e in query}
    threads = entities.get('discussion', {})
    project_ids = {t.project_id for t in threads.values() if t.project_id}
    projects = {p.id: p for p in ShowcaseProject.query.filter(ShowcaseProject.id.in_(project_ids), ShowcaseProject.status == 'visible')} if project_ids else {}
    from models import DiscussionReaction as Reaction
    likes = {r.target_id for r in Reaction.query.filter_by(user_id=user.id, target_type='thread', reaction_type='like')}
    items = []
    from blueprints.media import public_avatar_url
    from blueprints.showcase import STATUS_TEXT, SOURCE_TEXT
    for rank in ranks:
        entity = entities[rank['kind']][rank['id']]
        if rank['kind'] == 'discussion':
            item = reading.thread_data(entity, summary=True)
            item['replies'] = []
            project = projects.get(entity.project_id)
            if project:
                item.update(project_title=project.title, project_summary=project.summary,
                            project_cover_thumb=cover_thumbnail(project.cover), project_status=project.project_status,
                            project_status_text=STATUS_TEXT.get(project.project_status, project.project_status),
                            project_source=project.source, project_source_text=SOURCE_TEXT.get(project.source, project.source),
                            project_view_count=project.view_count)
                try:
                    item['project_tags'] = json.loads(project.tags or '[]')
                except (ValueError, TypeError):
                    item['project_tags'] = []
        else:
            v2 = rank['kind'] == 'article_v2'
            cover = entity.cover_image_key if v2 else None
            item = dict(type='article', id=entity.id, article_id=entity.id, article_version=2 if v2 else None,
                        title=entity.title, summary=(entity.introduction or '')[:200], cover=cover, cover_thumb=cover_thumbnail(cover),
                        author_id=entity.author_id, author_name=entity.author.username,
                        author_avatar=public_avatar_url(entity.author.avatar_url),
                        created_at=reading.stamp(entity.publish_time), reply_count=rank['rc'],
                        like_count=rank['lc'], view_count=rank['vc'], is_pinned=False, is_essence=bool(rank['essence']))
        item['liked'] = rank['like_tid'] in likes
        items.append(item)
    from blueprints.officers import badge_map
    badges = badge_map(list({item['author_id'] for item in items if item.get('author_id')}))
    for item in items:
        badge = badges.get(item['author_id'])
        if badge:
            item.update(author_badge=badge['text'], author_badge_tier=badge['tier'])
    # Window query limits previews in SQL even for topics with thousands of comments.
    if threads:
        ranked = select(Reply.id.label('id'), func.row_number().over(partition_by=Reply.thread_id,
                        order_by=(Reply.created_at.desc(), Reply.id.desc())).label('n')).where(
                            Reply.thread_id.in_(threads), Reply.parent_reply_id.is_(None), Reply.status == 'normal').subquery()
        previews = Reply.query.options(joinedload(Reply.author)).join(ranked, ranked.c.id == Reply.id).filter(ranked.c.n <= 2).order_by(Reply.created_at, Reply.id).all()
        preview_ids = [reply.id for reply in previews]
        liked_replies = {r.target_id for r in Reaction.query.filter_by(
            user_id=user.id, target_type='reply', reaction_type='like').filter(Reaction.target_id.in_(preview_ids))}
        by_thread = {}
        for reply in previews:
            by_thread.setdefault(reply.thread_id, []).append(reading.reply_data(reply, liked_replies))
        for item in items:
            if item['type'] == 'discussion':
                item['replies'] = by_thread.get(item['id'], [])
    return dict(code=200, data=items, total=total, page=page, per_page=size, pages=(total + size - 1) // size)

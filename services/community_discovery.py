"""Small curated section and cached ranking of unique recent participants."""
import json
import time
from datetime import datetime, timedelta
from sqlalchemy import select, func, literal, union_all
from exts import db, redis_client
from models import CommunityVisit as Visit, DiscussionThread as Thread, DiscussionReply as Reply, DiscussionReaction as Reaction
from services import community_reading as reading

REFRESH_SECONDS = 15 * 60


def record_read(thread, user):
    if thread.scope_type != 'global' or thread.author_id == user.id or thread.status != 'normal' or reading.closed(thread):
        return
    values = dict(thread_id=thread.id, user_id=user.id, last_seen_at=datetime.now())
    if db.engine.dialect.name == 'mysql':
        from sqlalchemy.dialects.mysql import insert
        statement = insert(Visit).values(**values)
        statement = statement.on_duplicate_key_update(last_seen_at=statement.inserted.last_seen_at)
    else:
        from sqlalchemy.dialects.sqlite import insert
        statement = insert(Visit).values(**values).on_conflict_do_update(
            index_elements=['thread_id', 'user_id'], set_={'last_seen_at': values['last_seen_at']})
    db.session.execute(statement)
    db.session.commit()


def rank_recent(tag_id, now):
    since = now - timedelta(hours=24)
    reads = select(Visit.thread_id.label('thread_id'), literal(1).label('weight'), Visit.last_seen_at.label('at')).join(
        Thread, Thread.id == Visit.thread_id).where(Visit.last_seen_at >= since, Visit.user_id != Thread.author_id)
    comments = select(Reply.thread_id.label('thread_id'), literal(3).label('weight'), func.max(Reply.created_at).label('at')).join(
        Thread, Thread.id == Reply.thread_id).where(Reply.created_at >= since, Reply.status == 'normal',
        Reply.author_id != Thread.author_id).group_by(Reply.thread_id, Reply.author_id)
    likes = select(Reaction.target_id.label('thread_id'), literal(2).label('weight'), Reaction.created_at.label('at')).join(
        Thread, Thread.id == Reaction.target_id).where(Reaction.target_type == 'thread', Reaction.reaction_type == 'like',
        Reaction.created_at >= since, Reaction.user_id != Thread.author_id)
    activity = union_all(reads, comments, likes).subquery()
    scores = select(activity.c.thread_id, func.sum(activity.c.weight).label('score'), func.max(activity.c.at).label('last')).group_by(activity.c.thread_id).subquery()
    query = reading.topic_query(tag_id, active_only=True).join(scores, scores.c.thread_id == Thread.id)
    rows = query.with_entities(Thread.id, scores.c.score).order_by(scores.c.score.desc(), scores.c.last.desc(), Thread.created_at.desc(), Thread.id.desc()).limit(32).all()
    return dict(ids=[row.id for row in rows], updated_at=reading.stamp(now), expires_at=time.time() + REFRESH_SECONDS)


def ranking(tag_id, now):
    key = f'community:discovery:v1:{tag_id or "all"}'
    cached = None
    try:
        raw = redis_client.get(key)
        cached = json.loads(raw) if raw else None
        if cached and cached['expires_at'] > time.time():
            return cached
        acquired = redis_client.set(key + ':lock', '1', nx=True, ex=10)
        if not acquired and cached:
            return cached
    except Exception:
        acquired = False
    try:
        result = rank_recent(tag_id, now)
        try:
            redis_client.setex(key, 3600, json.dumps(result))
        except Exception:
            pass
        return result
    finally:
        if acquired:
            try:
                redis_client.delete(key + ':lock')
            except Exception:
                pass


def discovery(args, user):
    tag_id = None
    if args.get('tag_id'):
        try:
            tag_id = int(args['tag_id'])
        except (ValueError, TypeError):
            raise reading.CommunityError('标签格式错误')
        from models import CommunityTag
        if tag_id <= 0 or not db.session.get(CommunityTag, tag_id):
            raise reading.CommunityError('标签不存在', 404)
    now = datetime.now()
    featured = reading.topic_query(tag_id, featured=True, active_only=True).order_by(
        func.coalesce(Thread.featured_at, Thread.created_at).desc(), Thread.id.desc()).limit(3).all()
    snapshot = ranking(tag_id, now)
    # Recheck visibility on every response; moderation must not wait for the cache.
    allowed = {t.id: t for t in reading.topic_query(tag_id, active_only=True).filter(Thread.id.in_(snapshot['ids'])).all()}
    featured_ids = {t.id for t in featured}
    hot = [allowed[tid] for tid in snapshot['ids'] if tid in allowed and tid not in featured_ids][:5]
    def item(t):
        return dict(id=t.id, title=t.title)
    return dict(code=200, data=dict(featured=[item(t) for t in featured], hot=[item(t) for t in hot],
        updated_at=snapshot['updated_at'], refresh_seconds=REFRESH_SECONDS, window_hours=24))

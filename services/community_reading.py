"""Community tags, reading topics and two-level reply presentation.

The existing discussion routes remain the public contract. This service keeps
validation, word counts, visibility and exports consistent across all clients.
"""
import json
import math
import time
import unicodedata
from datetime import datetime, timedelta

from flask import current_app
from sqlalchemy import and_, or_, exists, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload, selectinload, aliased

from exts import db, redis_client
from models import (CommunityTag, CommunityBook, DiscussionThreadTag,
                    DiscussionThread as Thread, DiscussionReply as Reply,
                    DiscussionReaction as Reaction, UserModel)

MAX_CONTENT = 10000
PUBLIC_STATUSES = ('normal', 'locked', 'closed')


class CommunityError(Exception):
    def __init__(self, message, status=400, **extra):
        super().__init__(message)
        self.message, self.status, self.extra = message, status, extra


def word_count(value):
    return sum(1 for c in (value or '') if not c.isspace() and not unicodedata.category(c).startswith(('P', 'C')))


def text_value(value, label, minimum=0, maximum=MAX_CONTENT):
    if not isinstance(value, str):
        raise CommunityError(f'{label}格式错误')
    value = value.strip()
    if len(value) < minimum or len(value) > maximum:
        raise CommunityError(f'{label}须为 {minimum} 至 {maximum} 个字符')
    return value


def positive_id(value, label):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CommunityError(f'{label}格式错误')
    return value


def page_values(args, default=20):
    try:
        page = int(args.get('page', 1))
        size = int(args.get('per_page', default))
    except (ValueError, TypeError):
        raise CommunityError('分页参数须为整数')
    if page < 1 or size < 1:
        raise CommunityError('分页参数须大于零')
    return page, min(size, 50)


def request_key(data):
    key = data.get('client_request_id')
    if key is not None:
        key = text_value(key, '请求标识', 8, 64)
    return key


def normalize_book(title):
    return ''.join(c for c in unicodedata.normalize('NFKC', title).casefold()
                   if not c.isspace() and c not in '《》〈〉「」『』')


def closed(thread):
    return thread.status == 'closed' or thread.author_closed_at is not None


def stamp(value):
    return value.strftime('%Y-%m-%d %H:%M:%S') if value else None


def tag_data(tag):
    return dict(id=tag.id, name=tag.name, description=tag.description,
                is_reading=bool(tag.is_reading), is_active=bool(tag.is_active),
                suggested_words=tag.suggested_words)


def book_data(book):
    return dict(id=book.id, tag_id=book.tag_id, title=book.title, author=book.author) if book else None


def thread_extra(thread):
    result = dict(tags=[tag_data(t) for t in thread.tags], book=book_data(thread.book),
                  book_id=thread.book_id, word_count=0 if closed(thread) else word_count(thread.content),
                  is_closed=closed(thread), author_closed_at=stamp(thread.author_closed_at))
    if closed(thread):
        result.update(title=f'《{thread.book.title}》' if thread.book else '已停止的讨论',
                      content='', summary='该帖子已停止讨论，历史评论仍可查看。', images=[])
    return result


def thread_data(thread, user=None, summary=False):
    from blueprints.discussion import CATEGORY_TEXT, _pin_active, _thread_extra
    from blueprints.media import public_avatar_url
    try:
        images = json.loads(thread.images_json) if thread.images_json else []
    except (ValueError, TypeError):
        images = []
    result = dict(id=thread.id, type='discussion', title=thread.title,
                  content=thread.content, summary=thread.content[:200], images=images,
                  category=thread.category, category_text=CATEGORY_TEXT.get(thread.category),
                  project_id=thread.project_id, scope_type=thread.scope_type, scope_id=thread.scope_id,
                  author_id=thread.author_id, author_name=thread.author.username if thread.author else '',
                  author_avatar=public_avatar_url(thread.author.avatar_url) if thread.author else '',
                  status=thread.status, is_pinned=_pin_active(thread), pinned_effective=_pin_active(thread),
                  is_essence=bool(thread.is_essence), reply_count=thread.reply_count,
                  like_count=thread.like_count, view_count=thread.view_count,
                  created_at=stamp(thread.created_at), updated_at=stamp(thread.updated_at),
                  last_reply_at=stamp(thread.last_reply_at), liked=False)
    result.update(_thread_extra(thread))
    if user:
        result['liked'] = Reaction.query.filter_by(user_id=user.id, target_type='thread',
                                                  target_id=thread.id, reaction_type='like').first() is not None
    if summary:
        result.pop('content', None)
    return result


def validate_images(images, required=False):
    if images is None:
        images = []
    if (not isinstance(images, list) or len(images) > 4 or
            any(not isinstance(u, str) or not u.startswith('/media/discussions/') or '..' in u for u in images)):
        raise CommunityError('图片须为最多 4 张已上传的社区图片')
    if required and not images:
        raise CommunityError('阅读分享首帖请至少上传一张书籍图片')
    if required:
        from storage import storage
        for path in images:
            try:
                storage.stat_object(path.lstrip('/'))
            except FileNotFoundError:
                raise CommunityError('书籍图片不存在，请重新上传')
            except Exception as exc:
                if getattr(exc, 'code', None) in ('NoSuchKey', 'NoSuchObject'):
                    raise CommunityError('书籍图片不存在，请重新上传')
                raise CommunityError('暂时无法核实书籍图片，请稍后重试，内容已保留', 503)
    return images


def selected_tags(ids, existing=None, lock=True):
    if not isinstance(ids, list) or len(ids) > 5:
        raise CommunityError('最多选择 5 个标签')
    ids = list(dict.fromkeys(positive_id(i, '标签') for i in ids))
    tags = CommunityTag.query.filter(CommunityTag.id.in_(ids)).order_by(CommunityTag.id).all() if ids else []
    if lock and len(tags) == len(ids):
        tags = [lock_activity(t.id) for t in tags]
    if len(tags) != len(ids) or any(not t.is_active and t.id not in {x.id for x in (existing or [])} for t in tags):
        raise CommunityError('标签不存在或已停用')
    if sum(t.is_reading for t in tags) > 1:
        raise CommunityError('一个帖子只可关联一个阅读活动')
    return tags


def lock_activity(tag_id):
    if db.engine.dialect.name == 'sqlite':
        # SQLite has no SELECT FOR UPDATE; take its writer lock for local parity.
        db.session.execute(update(CommunityTag).where(CommunityTag.id == tag_id).values(is_active=CommunityTag.is_active))
    tag = db.session.execute(select(CommunityTag).where(CommunityTag.id == tag_id).with_for_update()
                             .execution_options(populate_existing=True)).scalar_one_or_none()
    if tag is None:
        raise CommunityError('标签不存在或已删除', 404)
    return tag


def resolve_book(data, tags):
    reading = next((t for t in tags if t.is_reading), None)
    if reading is None:
        if data.get('book_id') or data.get('book_title'):
            raise CommunityError('请先选择阅读活动标签')
        return None
    # A tag row serializes same-activity creation on MySQL, including first books.
    lock_activity(reading.id)
    if data.get('book_id'):
        book = db.session.get(CommunityBook, positive_id(data['book_id'], '书籍'))
        if not book or book.tag_id != reading.id:
            raise CommunityError('书籍不属于所选阅读活动')
        return book
    title = text_value(data.get('book_title', ''), '书名', 1, 150)
    author = text_value(data.get('book_author', ''), '作者', 0, 100)
    normalized = normalize_book(title)
    if not normalized:
        raise CommunityError('请填写有效书名')
    book = CommunityBook.query.filter_by(tag_id=reading.id, normalized_title=normalized).with_for_update().first()
    if not book:
        book = CommunityBook(tag_id=reading.id, title=title, normalized_title=normalized, author=author)
        db.session.add(book)
        db.session.flush()
    return book


def duplicates(book, excluding=None):
    if not book:
        return []
    query = Thread.query.filter(Thread.book_id == book.id, Thread.status.in_(PUBLIC_STATUSES))
    if excluding:
        query = query.filter(Thread.id != excluding)
    # Closed history is discoverable, but must not prevent a fresh live topic.
    return query.filter(Thread.author_closed_at.is_(None), Thread.status != 'closed').order_by(Thread.created_at).limit(5).with_for_update().all()


def duplicate_check(data, book, excluding=None):
    if 'allow_duplicate' in data and not isinstance(data['allow_duplicate'], bool):
        raise CommunityError('重复发帖确认格式错误')
    rows = duplicates(book, excluding)
    if rows and data.get('allow_duplicate') is not True:
        raise CommunityError('这本书已有讨论，欢迎先去参与', 409, machine='DUPLICATE_BOOK',
                             existing_threads=[dict(id=t.id, title=t.title) for t in rows])


def validate_thread(data, existing=None):
    from blueprints.discussion import _validate_category_project
    title = text_value(data.get('title', existing.title if existing else ''), '标题', 4, 200)
    content = text_value(data.get('content', existing.content if existing else ''), '正文', 10)
    tags = selected_tags(data.get('tag_ids', [t.id for t in existing.tags] if existing else []), existing.tags if existing else None)
    book_fields = dict(data)
    if existing and 'book_id' not in data and 'book_title' not in data and existing.book_id:
        book_fields['book_id'] = existing.book_id
    book = resolve_book(book_fields, tags)
    images = validate_images(data.get('images', json.loads(existing.images_json or '[]') if existing else []), bool(book))
    category, project_id, err = _validate_category_project(data)
    if err:
        raise CommunityError('话题或关联项目无效')
    if existing:
        category = existing.category if category is False else category
        project_id = existing.project_id if project_id is False else project_id
    else:
        category = None if category is False else category
        project_id = None if project_id is False else project_id
    if book and (not existing or existing.book_id != book.id):
        duplicate_check(data, book, existing.id if existing else None)
    return dict(title=title, content=content, tags=tags, book=book,
                images_json=json.dumps(images, ensure_ascii=False) if images else None,
                category=category, project_id=project_id)


def enforce_rate(user_id, kind='reply'):
    windows = ((60, 10), (3600, 120)) if kind == 'reply' else ((300, 3), (3600, 10))
    now = int(time.time())
    try:
        pipe = redis_client.pipeline(transaction=True)
        for seconds, _ in windows:
            key = f'community:{kind}:{user_id}:{seconds}:{now // seconds}'
            pipe.incr(key)
            pipe.expire(key, seconds + 1)
        results = pipe.execute()
    except Exception as exc:
        current_app.logger.warning('Community rate store unavailable: %s', type(exc).__name__)
        raise CommunityError('评论服务暂不可用，已保留你的内容，请稍后重试', 503)
    for index, (seconds, limit) in enumerate(windows):
        if results[index * 2] > limit:
            retry = seconds - now % seconds
            raise CommunityError(f'发布较频繁，请 {retry} 秒后重试', 429, retry_after=retry)


def topic_request_matches(thread, data):
    if thread.content != str(data.get('content', '')).strip() or thread.title != str(data.get('title', '')).strip():
        return False
    if not isinstance(data.get('tag_ids', []), list):
        return False
    if sorted(str(i) for i in data.get('tag_ids', [])) != sorted(str(t.id) for t in thread.tags):
        return False
    if data.get('images', []) != json.loads(thread.images_json or '[]'):
        return False
    if (data.get('category') or None, data.get('project_id') or None) != (thread.category, thread.project_id):
        return False
    if data.get('book_id'):
        return thread.book_id == data['book_id']
    return normalize_book(data.get('book_title') or '') == (thread.book.normalized_title if thread.book else '')


def create_topic(data, user):
    if not isinstance(data, dict):
        raise CommunityError('请求参数错误')
    key = request_key(data)
    if key:
        old = Thread.query.filter_by(author_id=user.id, client_request_id=key).first()
        if old:
            if not topic_request_matches(old, data):
                raise CommunityError('请求标识已用于其他内容', 409)
            return old, True
    try:
        fields = validate_thread(data)
    except CommunityError as exc:
        if key and exc.extra.get('machine') == 'DUPLICATE_BOOK':
            db.session.rollback()
            old = Thread.query.filter_by(author_id=user.id, client_request_id=key).first()
            if old and topic_request_matches(old, data):
                return old, True
        raise
    enforce_rate(user.id, 'post')
    thread = Thread(scope_type='global', author_id=user.id, client_request_id=key, **fields)
    db.session.add(thread)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        old = Thread.query.filter_by(author_id=user.id, client_request_id=key).first() if key else None
        if old and topic_request_matches(old, data):
            return old, True
        raise CommunityError('内容已更新，请重新检查后发布', 409)
    try:
        from services.points import award_daily_post
        award_daily_post(thread)
    except Exception:
        db.session.rollback()
        current_app.logger.warning('Daily post award deferred for thread %s', thread.id)
    return thread, False


def update_topic(thread, data):
    if closed(thread) or thread.status in ('hidden', 'deleted'):
        raise CommunityError('该帖子已停止讨论或不可编辑', 403)
    # Creation locks activity before topic rows; keep the same order on edits.
    # Include both activities when moving a topic so opposite moves cannot invert locks.
    tags = selected_tags(data.get('tag_ids', [t.id for t in thread.tags]), thread.tags, lock=False)
    activity_ids = {t.id for t in [*thread.tags, *tags]}
    for tag_id in sorted(activity_ids):
        lock_activity(tag_id)
    db.session.refresh(thread, with_for_update=True)
    if closed(thread) or thread.status in ('hidden', 'deleted'):
        raise CommunityError('该帖子已停止讨论或不可编辑', 403)
    fields = validate_thread(data, thread)
    for key, value in fields.items():
        setattr(thread, key, value)
    db.session.commit()
    return thread


def stop_topic(thread):
    db.session.refresh(thread, with_for_update=True)
    if thread.status in ('hidden', 'deleted'):
        raise CommunityError('该帖子不可操作', 403)
    if not closed(thread):
        thread.status = 'closed'
        thread.author_closed_at = datetime.now()
        thread.is_pinned = False
        db.session.commit()


def reply_data(reply, liked_ids=None):
    from blueprints.media import public_avatar_url
    visible = reply.status == 'normal'
    parent = db.session.get(Reply, reply.parent_reply_id) if reply.parent_reply_id else None
    return dict(id=reply.id, thread_id=reply.thread_id, parent_reply_id=reply.parent_reply_id,
                root_reply_id=reply.root_reply_id, author_id=reply.author_id if visible else None,
                author_name=reply.author.username if visible and reply.author else '已删除的评论',
                author_avatar=public_avatar_url(reply.author.avatar_url) if visible and reply.author else '',
                content=reply.content if visible else '', word_count=word_count(reply.content) if visible else 0,
                status=reply.status, like_count=reply.like_count if visible else 0,
                liked=reply.id in (liked_ids or set()), created_at=stamp(reply.created_at),
                updated_at=stamp(reply.updated_at),
                reply_to_name=(parent.author.username if parent.status == 'normal' and parent.author
                               else '已删除的评论') if parent else None)


def create_comment(thread, user, data):
    from blueprints.discussion import can_post_thread
    # Lock before validating and before adding pending rows (autoflush must not race closure).
    db.session.refresh(thread, with_for_update=True)
    if thread.status != 'normal' or closed(thread):
        raise CommunityError('该帖子已停止讨论，无法新增回复', 403)
    if not can_post_thread(thread.scope_type, thread.scope_id, user):
        raise CommunityError('无权限回复', 403)
    content = text_value(data.get('content'), '回复', 2)
    parent_id = data.get('parent_reply_id') or None
    parent = None
    if parent_id:
        parent = db.session.get(Reply, positive_id(parent_id, '回复对象'))
        if not parent or parent.thread_id != thread.id or parent.status != 'normal':
            raise CommunityError('回复对象不存在或已删除')
    key = request_key(data)
    if key:
        old = Reply.query.filter_by(author_id=user.id, client_request_id=key).first()
        if old:
            if (old.content, old.thread_id, old.parent_reply_id) != (content, thread.id, parent_id):
                raise CommunityError('请求标识已用于其他评论', 409)
            return old, True
    enforce_rate(user.id)
    root_id = None
    if parent:
        root_id = parent.root_reply_id or parent.id
        # Legacy deeper conversations are traversed once and then stored flat.
        cursor, seen = parent, set()
        while cursor.parent_reply_id and not cursor.root_reply_id:
            if cursor.id in seen:
                raise CommunityError('回复关系异常')
            seen.add(cursor.id)
            cursor = db.session.get(Reply, cursor.parent_reply_id)
            if not cursor or cursor.thread_id != thread.id:
                raise CommunityError('回复关系异常')
            root_id = cursor.root_reply_id or cursor.id
    comment = Reply(thread_id=thread.id, author_id=user.id, content=content,
                    parent_reply_id=parent_id, root_reply_id=root_id, client_request_id=key)
    try:
        db.session.add(comment)
        # Atomic count and conditional write also guard SQLite's lack of row locks.
        changed = db.session.execute(update(Thread).where(Thread.id == thread.id, Thread.status == 'normal',
            Thread.author_closed_at.is_(None)).values(reply_count=Thread.reply_count + 1,
            last_reply_at=datetime.now()), execution_options={'synchronize_session': False})
        if changed.rowcount != 1:
            raise CommunityError('该帖子已停止讨论，无法新增回复', 403)
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        old = Reply.query.filter_by(author_id=user.id, client_request_id=key).first() if key else None
        if old and (old.content, old.thread_id, old.parent_reply_id) == (content, thread.id, parent_id):
            return old, True
        raise CommunityError('内容已变化，请重试', 409)
    try:
        from blueprints.notification import create_notification_once_unread
        targets = {thread.author_id, parent.author_id if parent else thread.author_id} - {user.id}
        for uid in targets:
            create_notification_once_unread(uid, title='你的内容有新回复',
                content=f'{user.username} 回复了你：{content[:80]}', category='community',
                source_type='discussion_reply', source_id=thread.id)
        db.session.commit()
    except Exception:
        db.session.rollback()
    return comment, False


def list_comments(thread, args, user, root=None):
    page, size = page_values(args, 10 if root else 20)
    liked = {r.target_id for r in Reaction.query.filter_by(user_id=user.id, target_type='reply', reaction_type='like')}
    if root:
        query = Reply.query.filter(Reply.thread_id == thread.id, Reply.root_reply_id == root.id,
                                   Reply.status == 'normal')
    else:
        child = aliased(Reply)
        has_child = exists(select(child.id).where(child.root_reply_id == Reply.id, child.status == 'normal'))
        query = Reply.query.filter(Reply.thread_id == thread.id, Reply.parent_reply_id.is_(None),
                                   or_(Reply.status == 'normal', and_(Reply.status == 'deleted', has_child)))
    pagination = query.options(joinedload(Reply.author)).order_by(Reply.created_at, Reply.id).paginate(
        page=page, per_page=size, error_out=False)
    rows = []
    for item in pagination.items:
        result = reply_data(item, liked)
        if not root:
            children = Reply.query.filter_by(thread_id=thread.id, root_reply_id=item.id, status='normal')
            result['children_total'] = children.count()
            result['children'] = [reply_data(c, liked) for c in children.order_by(Reply.created_at, Reply.id).limit(3)]
        rows.append(result)
    return dict(code=200, data=rows, total=pagination.total, page=page, per_page=size,
                pages=pagination.pages, reply_count=thread.reply_count)


def list_tags(include_inactive=False):
    counts = dict(db.session.query(DiscussionThreadTag.tag_id, func.count(Thread.id)).join(
        Thread, Thread.id == DiscussionThreadTag.thread_id).filter(Thread.status.in_(PUBLIC_STATUSES)).group_by(DiscussionThreadTag.tag_id).all())
    query = CommunityTag.query
    if not include_inactive:
        query = query.filter_by(is_active=True)
    return [dict(**tag_data(t), post_count=counts.get(t.id, 0)) for t in query.order_by(CommunityTag.id)]


def save_tag(data, user, tag=None):
    if tag is not None:
        tag = lock_activity(tag.id)
    name = text_value(data.get('name', tag.name if tag else ''), '标签名称', 1, 50)
    description = text_value(data.get('description', tag.description if tag else ''), '标签说明', 0, 500)
    for flag in ('is_reading', 'is_active'):
        if flag in data and not isinstance(data[flag], bool):
            raise CommunityError('标签选项格式错误')
    if tag and 'is_reading' in data and data['is_reading'] != tag.is_reading:
        if tag_has_posts(tag.id) or CommunityBook.query.filter_by(tag_id=tag.id).with_for_update().first():
            raise CommunityError('已有帖子或书单的标签不能修改类型；可停用后新建标签', 409)
    suggested = data.get('suggested_words', tag.suggested_words if tag else 500)
    if isinstance(suggested, bool) or not isinstance(suggested, int) or not 1 <= suggested <= MAX_CONTENT:
        raise CommunityError('建议字数须为 1 至 10000 的整数')
    if CommunityTag.query.filter(CommunityTag.name == name, CommunityTag.id != (tag.id if tag else -1)).first():
        raise CommunityError('该标签名称已存在', 409)
    tag = tag or CommunityTag(created_by=user.id)
    tag.name, tag.description, tag.suggested_words = name, description, suggested
    tag.is_reading = data.get('is_reading', tag.is_reading or False)
    tag.is_active = data.get('is_active', tag.is_active if tag.id else True)
    db.session.add(tag)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        raise CommunityError('该标签名称已存在', 409)
    return tag


def tag_has_posts(tag_id):
    # Include hidden/deleted history and book references even if the tag was detached.
    # Locking reads see commits made while waiting for the tag under MySQL REPEATABLE READ.
    if DiscussionThreadTag.query.filter_by(tag_id=tag_id).with_for_update().first():
        return True
    books = CommunityBook.query.filter_by(tag_id=tag_id).with_for_update().all()
    return bool(books) and Thread.query.filter(Thread.book_id.in_([b.id for b in books])).with_for_update().first() is not None


def delete_tag(tag):
    tag = lock_activity(tag.id)
    if tag_has_posts(tag.id):
        raise CommunityError('该标签已关联帖子或历史内容，不能删除；请在编辑中停用', 409)
    CommunityBook.query.filter_by(tag_id=tag.id).delete(synchronize_session='fetch')
    db.session.delete(tag)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        raise CommunityError('该标签已被使用，不能删除；请刷新后停用', 409)


def import_books(tag, rows):
    tag = lock_activity(tag.id)
    if not tag.is_reading:
        raise CommunityError('只有阅读活动可导入书单')
    if not isinstance(rows, list) or not 1 <= len(rows) <= 500:
        raise CommunityError('每次可导入 1 至 500 本书')
    prepared = []
    for row in rows:
        if not isinstance(row, dict):
            raise CommunityError('书单格式错误')
        title = text_value(row.get('title'), '书名', 1, 150)
        author = text_value(row.get('author', ''), '作者', 0, 100)
        norm = normalize_book(title)
        if not norm:
            raise CommunityError('书名无效')
        prepared.append((title, author, norm))
    created = 0
    for title, author, norm in prepared:
        if not CommunityBook.query.filter_by(tag_id=tag.id, normalized_title=norm).first():
            db.session.add(CommunityBook(tag_id=tag.id, title=title, author=author, normalized_title=norm))
            db.session.flush()
            created += 1
    db.session.commit()
    return created


def topic_query(tag_id=None, featured=False, active_only=False):
    query = Thread.query.filter(Thread.scope_type == 'global', Thread.status.in_(PUBLIC_STATUSES))
    if tag_id:
        query = query.filter(Thread.tags.any(CommunityTag.id == tag_id))
    if featured:
        query = query.filter(Thread.is_essence.is_(True))
    if active_only:
        query = query.filter(Thread.status == 'normal', Thread.author_closed_at.is_(None))
    return query


def topic_page(args, user, recommendations=False):
    page, size = page_values(args, 5 if recommendations else 20)
    try:
        tag_id = int(args['tag_id']) if args.get('tag_id') else None
    except (TypeError, ValueError):
        raise CommunityError('标签格式错误')
    if tag_id and not db.session.get(CommunityTag, tag_id):
        raise CommunityError('标签不存在', 404)
    query = topic_query(tag_id, args.get('featured') == 'true', recommendations)
    category = args.get('category')
    if category:
        from blueprints.discussion import THREAD_CATEGORIES
        if category not in THREAD_CATEGORIES:
            raise CommunityError('话题不存在')
        query = query.filter(Thread.category == category)
    sort = args.get('sort', 'hot')
    if sort not in ('hot', 'latest'):
        raise CommunityError('排序方式无效')
    if sort == 'hot':
        # SQL ordering with the same interaction, decay and essence factors as the feed.
        rank_dt = func.coalesce(Thread.last_reply_at, Thread.created_at)
        if db.engine.dialect.name == 'sqlite':
            age = func.max(0.0, func.julianday(datetime.now()) - func.julianday(rank_dt))
        else:
            age = func.greatest(0.0, func.timestampdiff(db.text('SECOND'), rank_dt, func.now()) / 86400.0)
        from sqlalchemy import case
        protection = case((rank_dt > datetime.now() - timedelta(hours=48), 3), else_=0)
        score = (Thread.like_count + Thread.reply_count * 2 + 1 + protection) * func.pow(0.5, age / 14.0)
        query = query.join(UserModel, UserModel.id == Thread.author_id)
        score *= case((UserModel.role == 'super_admin', 1.5), else_=1.0)
        score = score * case((Thread.is_essence.is_(True), 2.0), else_=1.0)
        pin = and_(Thread.is_pinned.is_(True), or_(Thread.pinned_until.is_(None), Thread.pinned_until > datetime.now()))
        query = query.order_by(pin.desc(), score.desc(), Thread.id.desc())
    else:
        pin = and_(Thread.is_pinned.is_(True), or_(Thread.pinned_until.is_(None), Thread.pinned_until > datetime.now()))
        query = query.order_by(pin.desc(), func.coalesce(Thread.last_reply_at, Thread.created_at).desc(), Thread.id.desc())
    result = query.options(joinedload(Thread.author), selectinload(Thread.tags)).paginate(page=page, per_page=size, error_out=False)
    rows = [thread_data(t, summary=True) for t in result.items]
    liked = {r.target_id for r in Reaction.query.filter_by(user_id=user.id, target_type='thread', reaction_type='like')}
    for row in rows:
        row['liked'] = row['id'] in liked
        row['replies'] = []
    return dict(code=200, data=rows, total=result.total, page=page, per_page=size, pages=result.pages)

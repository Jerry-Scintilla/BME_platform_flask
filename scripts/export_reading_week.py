"""Read-only tag export. Explicit --sqlite selects isolated local data.

Default excludes moderated/deleted content, includes closed-topic comment history,
and emits a marked blank row for the author's hidden original. No grading logic.
"""
import argparse
import csv
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlalchemy import create_engine, select, and_, case, literal, union_all, event
from models import (UserModel as User, PersonModel as Person, CommunityBook as Book,
                    CommunityTag as Tag, DiscussionThread as Thread, DiscussionReply as Reply,
                    DiscussionThreadTag as Link)
from services.community_reading import word_count

HEADERS = ['姓名', '字数', '原文', '姓名来源', '书名', '账号ID', '内容类型', '发表时间', '帖子链接', '内容ID', '内容状态']


def export_rows(connection, tag_id, base_url):
    tag = connection.execute(select(Tag.__table__).where(Tag.id == tag_id)).mappings().first()
    if not tag:
        raise ValueError('标签不存在')
    visible = Thread.status.in_(('normal', 'locked', 'closed'))
    stopped = (Thread.status == 'closed') | Thread.author_closed_at.is_not(None)
    shared = [Book.title.label('book'), User.id.label('uid'), User.username.label('nickname'),
              Person.verified_name.label('verified_name'), Person.verification_status.label('verification'),
              Person.record_status.label('record_status'), Thread.id.label('tid')]
    topics = select(*shared, Thread.id.label('cid'), literal('首帖').label('kind'), Thread.created_at.label('created'),
                    case((stopped, ''), else_=Thread.content).label('content'),
                    case((stopped, '楼主已停止讨论，原文隐藏'), else_='正常').label('state')).select_from(Thread).join(
                        Link, Link.thread_id == Thread.id).join(User, User.id == Thread.author_id).outerjoin(
                        Person, Person.id == User.person_id).outerjoin(Book, Book.id == Thread.book_id).where(Link.tag_id == tag_id, visible)
    comments = select(*shared, Reply.id.label('cid'),
                      case((Reply.parent_reply_id.is_(None), '一级评论'), else_='楼中楼').label('kind'),
                      Reply.created_at.label('created'), Reply.content.label('content'),
                      case((stopped, '停止讨论后的历史评论'), else_='正常').label('state')).select_from(Reply).join(
                          Thread, Thread.id == Reply.thread_id).join(Link, Link.thread_id == Thread.id).join(
                          User, User.id == Reply.author_id).outerjoin(Person, Person.id == User.person_id).outerjoin(
                          Book, Book.id == Thread.book_id).where(Link.tag_id == tag_id, visible, Reply.status == 'normal')
    query = union_all(topics, comments).subquery()
    for row in connection.execute(select(query).order_by(query.c.tid, query.c.created, query.c.kind, query.c.cid)).mappings():
        verified = row['verification'] == 'verified' and row['record_status'] == 'active' and bool(row['verified_name'])
        yield [row['verified_name'] if verified else row['nickname'], word_count(row['content']), row['content'],
               '已核验人员姓名' if verified else '昵称（真实姓名待确认）', row['book'] or '', row['uid'], row['kind'],
               str(row['created'] or ''), base_url.rstrip('/') + '/community/thread/' + str(row['tid']), row['cid'], row['state']]


def safe_csv(value):
    value = str(value) if value is not None else ''
    if value.lstrip().startswith(('=', '+', '-', '@')) or value.startswith(('\t', '\r', '\n')):
        return "'" + value
    return value


def write_export(path, rows):
    path = Path(path)
    if path.suffix.lower() == '.csv':
        with path.open('w', encoding='utf-8-sig', newline='') as file:
            writer = csv.writer(file); writer.writerow(HEADERS)
            count = 0
            for row in rows:
                writer.writerow([safe_csv(v) if isinstance(v, str) else v for v in row]); count += 1
        return count
    if path.suffix.lower() != '.xlsx':
        raise ValueError('输出文件须为 .csv 或 .xlsx')
    from openpyxl import Workbook
    from openpyxl.cell import WriteOnlyCell
    book = Workbook(write_only=True); sheet = book.create_sheet('阅读心得')
    sheet.freeze_panes = 'D2'
    for column, width in zip('ABCDEFGHIJK', (18, 10, 90, 28, 28, 12, 14, 24, 60, 12, 32)):
        sheet.column_dimensions[column].width = width
    def append(values):
        cells = []
        for value in values:
            cell = WriteOnlyCell(sheet, value=value)
            if isinstance(value, str):
                cell.data_type = 's'  # Keep the exact original, including leading '='.
            cells.append(cell)
        sheet.append(cells)
    append(HEADERS); count = 0
    for row in rows:
        append(row); count += 1
    book.save(path)
    return count


def readonly_engine(url):
    engine = create_engine(url)
    if engine.dialect.name == 'sqlite':
        @event.listens_for(engine, 'connect')
        def readonly(dbapi, _):
            dbapi.execute('PRAGMA query_only=ON')
    else:
        @event.listens_for(engine, 'begin')
        def readonly(connection):
            connection.exec_driver_sql('SET TRANSACTION READ ONLY')
    return engine


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tag', type=int, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--sqlite', type=Path, help='Explicit isolated local SQLite file; no configured MySQL connection')
    parser.add_argument('--base-url', default='http://127.0.0.1:8081/AMEII')
    args = parser.parse_args()
    if args.sqlite:
        if not args.sqlite.is_file():
            parser.error('本地数据库文件不存在')
        url = 'sqlite:///' + args.sqlite.resolve().as_posix()
    else:
        import config
        url = config.SQLALCHEMY_DATABASE_URI
    engine = readonly_engine(url)
    try:
        with engine.connect() as connection:
            count = write_export(args.output, export_rows(connection, args.tag, args.base_url))
        print(f'已只读导出 {count} 条内容：{args.output.resolve()}')
    finally:
        engine.dispose()


if __name__ == '__main__':
    main()

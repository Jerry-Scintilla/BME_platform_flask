"""Real blueprint smoke suite, isolated SQLite/fakeredis; no production services."""
import csv
import hashlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch
from datetime import datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.run_reading_local import create_local_app
from exts import db, redis_client
from models import (CommunityTag as Tag, CommunityBook as Book, DiscussionThread as Thread,
                    DiscussionReply as Reply, UserModel as User, ArticleModel, ArticleV2Model, PersonModel)
from services import community_reading as reading
from scripts.export_reading_week import export_rows, write_export, readonly_engine
from sqlalchemy import create_engine, text, event
from sqlalchemy.dialects import mysql


class ReadingSmoke(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='reading-smoke-')
        cls.path = Path(cls.tmp.name) / 'smoke.sqlite'
        cls.app = create_local_app(cls.path)
        cls.app.config['TESTING'] = True
        cls.client = cls.app.test_client()
        cls.headers = {}
        for name in ('admin', 'stu1', 'stu2', 'stu3', 'stu4'):
            response = cls.client.post('/auth/login', json={'User_Email':name+'@seed.dev', 'User_Password':hashlib.md5(b'12345678').hexdigest()})
            assert response.status_code == 200, response.json
            cls.headers[name] = {'Authorization':'Bearer '+response.json['token']}

    @classmethod
    def tearDownClass(cls):
        with cls.app.app_context():
            db.session.remove(); db.engine.dispose()
        cls.tmp.cleanup()

    def setUp(self):
        self.ctx = self.app.app_context(); self.ctx.push(); redis_client.flushdb()

    def tearDown(self):
        db.session.remove(); self.ctx.pop()

    def call(self, method, path, data=None, user='stu1', status=200):
        response = self.client.open(path, method=method, json=data, headers=self.headers[user])
        self.assertEqual(response.status_code, status, (path, response.json or response.data[:200]))
        return response.json

    def new_tag(self, reading_tag=True):
        return self.call('POST', '/community/tags', {'name':'活动'+uuid.uuid4().hex[:12], 'is_reading':reading_tag}, user='admin', status=201)['data']

    def topic(self, **extra):
        tag = self.new_tag()
        data = {'title':'一起读一本好书', 'content':'这些文字是一段真实可发布的短心得。', 'scope_type':'global',
                'tag_ids':[tag['id']], 'book_title':'乡土中国', 'images':['/media/discussions/reading-local/book-1.webp'],
                'client_request_id':uuid.uuid4().hex}
        data.update(extra)
        row = self.call('POST','/discussions/threads',data,status=201)['data']
        return row, data, tag

    def comment(self, tid, parent=None, user='stu2', content='读过之后，我开始重新思考人与人的关系。', key=None, status=201):
        return self.call('POST',f'/discussions/threads/{tid}/replies', {'content':content, 'parent_reply_id':parent, 'client_request_id':key or uuid.uuid4().hex},user=user,status=status).get('data', {})

    def test_noncommunity_comments_keep_complete_children_and_original_contract(self):
        admin = User.query.filter_by(email='admin@seed.dev').one()
        # These legacy clients have no "load more children" control and must receive all replies.
        for scope in ('article', 'article_v2', 'course', 'group', 'task', 'project'):
            with self.subTest(scope=scope), patch.object(reading, 'enforce_rate', side_effect=AssertionError('community-only limiter')):
                thread = Thread(scope_type=scope, scope_id=999999, author_id=admin.id,
                                title='原有模块讨论', content='原有讨论正文')
                db.session.add(thread); db.session.commit()
                tid = thread.id
                parent = self.comment(tid, user='admin')
                children = [self.comment(tid, parent=parent['id'], user='admin')['id'] for _ in range(5)]
                rows = self.call('GET', f'/discussions/threads/{tid}/replies', user='admin')['data']
                self.assertEqual([c['id'] for c in rows[0]['children']], children)
                detail = self.call('GET', f'/discussions/threads/{tid}', user='admin')['data']
                self.assertNotIn('book', detail)
                edited = self.call('PUT', f"/discussions/replies/{parent['id']}", {'content':'原有编辑行为'}, user='admin')['data']
                self.assertEqual(set(edited), {'id', 'content', 'updated_at'})

    def test_article_comment_notifies_article_author_not_thread_creator(self):
        author = User.query.filter_by(email='stu1@seed.dev').one()
        creator = User.query.filter_by(email='admin@seed.dev').one()
        article = ArticleModel(title='评论通知兼容性', introduction='说明', author_id=author.id)
        db.session.add(article); db.session.flush()
        thread = Thread(scope_type='article', scope_id=article.id, author_id=creator.id,
                        title='自动创建的评论区', content='文章评论')
        db.session.add(thread); db.session.commit()
        with patch('blueprints.notification.create_notification_once_unread') as notify:
            self.comment(thread.id, user='stu2')
        self.assertEqual([call.args[0] for call in notify.call_args_list], [author.id])
        db.session.delete(article); db.session.commit()

    def test_project_reply_actions_keep_existing_author_permissions(self):
        user = User.query.filter_by(email='stu1@seed.dev').one()
        thread = Thread(scope_type='project', scope_id=999999, author_id=user.id,
                        title='项目留言区', content='项目讨论', reply_count=1)
        db.session.add(thread); db.session.flush()
        reply = Reply(thread_id=thread.id, author_id=user.id, content='项目内原有留言')
        db.session.add(reply); db.session.commit()
        self.call('POST', '/discussions/reactions', {'target_type':'reply', 'target_id':reply.id, 'reaction_type':'like'})
        self.call('PUT', f'/discussions/replies/{reply.id}', {'content':'作者编辑项目留言'})
        self.call('DELETE', f'/discussions/replies/{reply.id}')
        self.assertEqual(db.session.get(Reply, reply.id).status, 'deleted')

    def test_feed_preserves_author_badge_liked_preview_and_cover_thumbnail(self):
        from models import ClubGroup, ClubMembership, DiscussionReaction
        row, _, tag = self.topic()
        author_id = row['author_id']
        group = ClubGroup(name='兼容性核对组'+uuid.uuid4().hex[:6])
        db.session.add(group); db.session.flush()
        membership = ClubMembership(user_id=author_id, group_id=group.id, slot='primary', joined_at=datetime.now())
        db.session.add(membership); db.session.commit()
        comment = self.comment(row['id'])
        db.session.add(DiscussionReaction(user_id=author_id, target_type='reply', target_id=comment['id'], reaction_type='like'))
        article = ArticleV2Model(title='封面兼容性', introduction='说明', content_md='正文', author_id=author_id,
                                 status='published', is_official=False, cover_image_key='media/articles/a.webp')
        db.session.add(article); db.session.commit()
        feed = self.call('GET', f"/community/feed?tag_id={tag['id']}")['data'][0]
        self.assertEqual(feed['author_badge'], group.name)
        self.assertEqual(feed['author_badge_tier'], 3)
        self.assertTrue(feed['replies'][0]['liked'])
        thread = db.session.get(Thread, row['id']); thread.status = 'hidden'; db.session.commit()
        self.assertEqual(self.call('GET', f"/community/feed?tag_id={tag['id']}")['total'], 0)
        self.assertEqual(self.call('GET', f"/community/feed?tag_id={tag['id']}", user='admin')['total'], 1)
        articles = self.call('GET', '/community/feed?type=article&sort=latest&per_page=50')['data']
        projected = next(a for a in articles if a['id'] == article.id and a['article_version'] == 2)
        self.assertEqual(projected['cover_thumb'], 'media/articles/a_thumb.webp')
        db.session.delete(membership); db.session.delete(group); db.session.delete(article); db.session.commit()

    def test_tag_directory_permissions_and_empty_activity(self):
        tag = self.new_tag()
        self.assertIn(tag['id'], [t['id'] for t in self.call('GET','/community/tags')['data']])
        self.assertEqual(self.call('GET',f"/community/feed?tag_id={tag['id']}")['total'],0)
        self.call('POST','/community/tags',{'name':'禁止创建'},status=403)
        self.call('PUT',f"/community/tags/{tag['id']}",{'name':'禁止修改'},status=403)
        self.call('PUT',f"/community/tags/{tag['id']}",{'is_active':False},user='admin')
        self.assertNotIn(tag['id'],[t['id'] for t in self.call('GET','/community/tags')['data']])
        changed = self.call('PUT',f"/community/tags/{tag['id']}",{'is_reading':False},user='admin')['data']
        self.assertFalse(changed['is_reading'])

    def test_tag_delete_permissions_unused_books_and_stale_selection(self):
        tag = self.new_tag(); url = f"/community/tags/{tag['id']}"
        self.call('DELETE', url, status=403)
        self.call('POST', url+'/books', {'books':[{'title':'未使用书籍'}]}, user='admin')
        self.call('PUT', url, {'is_reading':False}, user='admin', status=409)
        self.call('DELETE', url, user='admin')
        self.call('GET', url, status=404)
        self.call('DELETE', url, user='admin', status=404)
        self.assertEqual(Book.query.filter_by(tag_id=tag['id']).count(), 0)
        self.call('POST', '/discussions/threads', {'title':'标签已删除的旧草稿', 'content':'旧草稿提交时应明确提示标签已经不可用。',
                  'scope_type':'global', 'tag_ids':[tag['id']]}, status=400)

    def test_tag_delete_and_type_change_preserve_hidden_history(self):
        row, data, tag = self.topic()
        comment = self.comment(row['id'])
        url = f"/community/tags/{tag['id']}"
        self.call('DELETE', url, user='admin', status=409)
        self.call('PUT', url, {'is_reading':False}, user='admin', status=409)
        thread = db.session.get(Thread, row['id'])
        thread.status = 'deleted'
        thread.tags = []
        db.session.commit()
        self.call('DELETE', url, user='admin', status=409)
        self.assertIsNotNone(db.session.get(Reply, comment['id']))
        self.call('PUT', url, {'is_active':False}, user='admin')

    def test_normal_tag_default_and_empty_type_change(self):
        tag = self.call('POST', '/community/tags', {'name':'普通标签'+uuid.uuid4().hex[:8]}, user='admin', status=201)['data']
        self.assertFalse(tag['is_reading'])
        url = f"/community/tags/{tag['id']}"
        self.assertTrue(self.call('PUT', url, {'is_reading':True}, user='admin')['data']['is_reading'])
        self.assertFalse(self.call('PUT', url, {'is_reading':False}, user='admin')['data']['is_reading'])
        row = self.call('POST', '/discussions/threads', {'title':'项目实践交流记录', 'content':'普通标签可以自由交流，不需要选择书籍和上传图片。',
                        'scope_type':'global', 'tag_ids':[tag['id']]}, status=201)['data']
        self.call('PUT', url, {'is_reading':True}, user='admin', status=409)
        self.call('DELETE', url, user='admin', status=409)
        self.assertIsNotNone(db.session.get(Thread, row['id']))

    def test_book_import_normalization_atomic_validation(self):
        tag = self.new_tag(); url=f"/community/tags/{tag['id']}/books"
        self.call('POST',url,{'books':[{'title':'测试书'}]},status=403)
        result = self.call('POST',url,{'books':[{'title':'《Ａ B》','author':'甲'},{'title':'a b'}]},user='admin')
        self.assertEqual(result['created'],1)
        self.call('POST',url,{'books':[{'title':'另一本'},{'title':''}]},user='admin',status=400)
        self.assertEqual(len(self.call('GET',url)['data']),1)
        self.call('POST',url,{'books':[]},user='admin',status=400)
        normal=self.new_tag(False)
        self.call('GET',f"/community/tags/{normal['id']}/books",status=404)

    def test_reading_constraints_all_write_paths(self):
        row,data,tag=self.topic()
        for fields in ({'images':[]},{'book_id':None,'book_title':''},{'content':'短'}, {'content':'字'*10001},{'title':'字'*201}):
            self.call('PUT',f"/discussions/threads/{row['id']}",fields,status=400)
        for images in ([], ['https://bad.invalid/a'], ['/media/discussions/../../x']):
            data['images']=images; data['client_request_id']=uuid.uuid4().hex
            self.call('POST','/discussions/threads',data,status=400)
        self.call('PUT',f"/discussions/threads/{row['id']}",{'content':'没有权限修改这个分享'},user='stu2',status=403)

    def test_word_guidance_and_unicode_limit(self):
        row,_,_=self.topic(content='甲乙， ABC 123。\n短心得')
        self.assertEqual(row['word_count'],reading.word_count(row['content']))
        self.assertLess(row['word_count'],500)
        self.assertEqual(reading.word_count('甲，Ａ A\n1\t。'),4)
        content='𠮷'*10000
        r=self.comment(row['id'],content=content)
        self.assertEqual(r['word_count'],10000)
        self.comment(row['id'],content=content+'字',status=400)

    def test_emoji_content_survives_posts_replies_edits_and_export(self):
        content = '读完后想说：很受启发 😀 👍 ❤️ 👩🏽‍💻'
        row, _, tag = self.topic(content=content)
        self.assertEqual(self.call('GET', f"/discussions/threads/{row['id']}")['data']['content'], content)
        comment = self.comment(row['id'], content=content)
        child = self.comment(row['id'], parent=comment['id'], content='谢谢分享 🙌')
        edited = self.call('PUT', f"/discussions/replies/{child['id']}", {'content': '继续交流 🤝'}, user='stu2')['data']
        self.assertEqual(edited['content'], '继续交流 🤝')
        comments = self.call('GET', f"/discussions/threads/{row['id']}/replies")['data']
        self.assertEqual(comments[0]['content'], content)
        self.assertEqual(comments[0]['word_count'], reading.word_count(content))
        with db.engine.connect() as connection:
            rows = list(export_rows(connection, tag['id'], 'http://localhost'))
        self.assertEqual(sum(r[2] == content for r in rows), 2)

    def test_duplicate_interrupt_override_and_title_normalization(self):
        row,data,_=self.topic(book_title='《Ａ Ｂ》')
        data.update(client_request_id=uuid.uuid4().hex,book_title='a b')
        result=self.call('POST','/discussions/threads',data,status=409)
        self.assertEqual(result['machine'],'DUPLICATE_BOOK')
        self.assertEqual(result['existing_threads'][0]['id'],row['id'])
        data['allow_duplicate']=True
        other=self.call('POST','/discussions/threads',data,status=201)['data']
        self.assertEqual(other['book_id'],row['book_id'])

    def test_inactive_tag_keeps_history_and_existing_edits(self):
        row,data,tag=self.topic()
        self.call('PUT',f"/community/tags/{tag['id']}",{'is_active':False},user='admin')
        self.assertFalse(self.call('GET',f"/community/tags/{tag['id']}")['data']['is_active'])
        self.call('PUT',f"/discussions/threads/{row['id']}",{'content':'原有活动帖子可以继续编辑自己的心得。'})
        data['client_request_id']=uuid.uuid4().hex
        self.call('POST','/discussions/threads',data,status=400)

    def test_concurrent_same_book_and_topic_retry(self):
        from concurrent.futures import ThreadPoolExecutor
        tag=self.new_tag()
        body={'title':'同时发布的同一本书','content':'两位同学同时发布时，仍应提示已有主题。','scope_type':'global','tag_ids':[tag['id']],
              'book_title':'并发书名','images':['/media/discussions/reading-local/book-1.webp']}
        def publish(key):
            with self.app.test_client() as client:
                response=client.post('/discussions/threads',json=dict(body,client_request_id=key),headers=self.headers['stu1'])
                return response.status_code,response.json
        with ThreadPoolExecutor(max_workers=2) as workers:
            rows=list(workers.map(publish,[uuid.uuid4().hex,uuid.uuid4().hex]))
        self.assertEqual(sorted(code for code,_ in rows),[201,409],rows)
        newtag=self.new_tag(); body['tag_ids']=[newtag['id']]; key=uuid.uuid4().hex
        with ThreadPoolExecutor(max_workers=2) as workers:
            rows=list(workers.map(publish,[key,key]))
        self.assertEqual(sorted(code for code,_ in rows),[200,201],rows)
        self.assertEqual(len({r['data']['id'] for _,r in rows}),1)

    def test_concurrent_comment_retries_and_atomic_counts(self):
        from concurrent.futures import ThreadPoolExecutor
        row,_,_=self.topic(); key=uuid.uuid4().hex
        def publish(index):
            with self.app.test_client() as client:
                response=client.post(f"/discussions/threads/{row['id']}/replies",json={'content':'并发重试保留同一条评论','client_request_id':key},headers=self.headers['stu2'])
                return response.status_code, response.json
        with ThreadPoolExecutor(max_workers=4) as workers:
            responses=list(workers.map(publish,range(4)))
        self.assertTrue(all(code in (200,201) for code,_ in responses),responses)
        self.assertEqual(len({r['data']['id'] for _,r in responses}),1)
        db.session.expire_all()
        self.assertEqual(db.session.get(Thread,row['id']).reply_count,1)
        def distinct(index):
            with self.app.test_client() as client:
                response=client.post(f"/discussions/threads/{row['id']}/replies",json={'content':f'不同学生的并发评论 {index}','client_request_id':uuid.uuid4().hex},headers=self.headers['stu3'])
                return response.status_code
        with ThreadPoolExecutor(max_workers=4) as workers:
            self.assertEqual(list(workers.map(distinct,range(4))),[201]*4)
        db.session.expire_all()
        self.assertEqual(db.session.get(Thread,row['id']).reply_count,5)

    def test_topic_and_comment_idempotent_retries(self):
        row,data,_=self.topic()
        replay=self.call('POST','/discussions/threads',data)['data']
        self.assertEqual(row['id'],replay['id'])
        data['title']='这个标题发生了修改'
        self.call('POST','/discussions/threads',data,status=409)
        key=uuid.uuid4().hex
        first=self.comment(row['id'],key=key)
        replay=self.comment(row['id'],key=key,status=200)
        self.assertEqual(first['id'],replay['id'])
        self.comment(row['id'],key=key,content='不同的内容应该不能复用标识',status=409)
        data['title']=row['title']; data['images']=[]
        self.call('POST','/discussions/threads',data,status=409)
        self.assertEqual(db.session.get(Thread,row['id']).reply_count,1)

    def test_three_reply_depths_and_independent_complete_pagination(self):
        tid=1
        first=self.call('GET',f'/discussions/threads/{tid}/replies?per_page=20')
        second=self.call('GET',f'/discussions/threads/{tid}/replies?per_page=20&page=2')
        self.assertEqual(len(first['data'])+len(second['data']),first['total'])
        self.assertEqual(len({r['id'] for r in first['data']+second['data']}),first['total'])
        root=first['data'][0]
        child1=self.call('GET',f"/discussions/replies/{root['id']}/children?per_page=10")
        child2=self.call('GET',f"/discussions/replies/{root['id']}/children?per_page=10&page=2")
        self.assertEqual(len(child1['data'])+len(child2['data']),15)
        target=child2['data'][-1]
        reply=self.comment(tid,parent=target['id'])
        self.assertEqual(reply['root_reply_id'],root['id'])
        self.assertEqual(reply['parent_reply_id'],target['id'])
        self.assertTrue(reply['reply_to_name'])
        self.call('GET',f"/discussions/replies/{target['id']}/children",status=404)

    def test_rate_is_per_user_and_has_retry_after(self):
        for i in range(10): self.comment(1,content=f'第{i}条短评论')
        response=self.client.post('/discussions/threads/1/replies',json={'content':'第十一条'},headers=self.headers['stu2'])
        self.assertEqual(response.status_code,429); self.assertGreater(int(response.headers['Retry-After']),0)
        self.comment(1,user='stu3')

    def test_no_business_total_limit(self):
        row,_,_=self.topic()
        db.session.add_all([Reply(thread_id=row['id'],author_id=2,content='历史评论') for _ in range(151)])
        db.session.commit()
        self.comment(row['id'])
        self.assertEqual(self.call('GET',f"/discussions/threads/{row['id']}/replies")['total'],152)

    def test_stopping_hides_original_keeps_history_and_blocks_every_reply_path(self):
        row,data,tag=self.topic(); tid=row['id']
        root=self.comment(tid); child=self.comment(tid,root['id'])
        stopped=self.call('DELETE',f'/discussions/threads/{tid}')['data']
        for result in (stopped,self.call('GET',f'/discussions/threads/{tid}')['data']):
            self.assertEqual(result['content'],''); self.assertEqual(result['images'],[]); self.assertTrue(result['is_closed'])
        roots=self.call('GET',f'/discussions/threads/{tid}/replies')['data']
        self.assertEqual(roots[0]['id'],root['id']); self.assertEqual(roots[0]['children'][0]['id'],child['id'])
        for parent in (None,root['id'],child['id']):
            self.comment(tid,parent,user='stu1',status=403)
        self.call('PUT',f'/discussions/threads/{tid}',{'content':'楼主不允许重新编辑原文'},status=403)
        self.call('POST',f'/discussions/threads/{tid}/lock',user='admin',status=403)
        history=self.call('GET',f"/community/feed?tag_id={tag['id']}")['data'][0]
        self.assertTrue(history['is_closed']); self.assertNotIn(data['content'],json.dumps(history,ensure_ascii=False))
        self.assertEqual(self.call('GET',f"/community/recommendations?tag_id={tag['id']}")['total'],0)
        self.call('PUT',f"/discussions/replies/{root['id']}",{'content':'停止后本人依旧可以修改历史评论'},user='stu2')
        data['client_request_id']=uuid.uuid4().hex
        self.call('POST','/discussions/threads',data,status=201)

    def test_deleted_root_tombstone_and_repeat_delete_count(self):
        row,_,_=self.topic(); root=self.comment(row['id']); child=self.comment(row['id'],root['id'])
        self.call('DELETE',f"/discussions/replies/{root['id']}",user='stu2')
        self.call('DELETE',f"/discussions/replies/{root['id']}",user='stu2')
        result=self.call('GET',f"/discussions/threads/{row['id']}/replies")
        self.assertEqual(result['reply_count'],1); self.assertEqual(result['data'][0]['status'],'deleted')
        self.assertEqual(result['data'][0]['content'],''); self.assertEqual(result['data'][0]['children'][0]['id'],child['id'])

    def test_moderation_visibility_and_restore_closed_state(self):
        row,_,tag=self.topic(); tid=row['id']; root=self.comment(tid)
        self.call('DELETE',f'/discussions/threads/{tid}')
        self.call('POST',f'/discussions/threads/{tid}/hide',user='admin')
        self.call('GET',f'/discussions/threads/{tid}',user='stu3',status=403)
        self.call('POST','/discussions/reactions',{'target_type':'reply','target_id':root['id']},user='stu3',status=403)
        self.assertEqual(self.call('GET',f"/community/feed?tag_id={tag['id']}")['total'],0)
        self.call('POST',f'/discussions/threads/{tid}/hide',user='admin')
        self.assertEqual(self.call('GET',f'/discussions/threads/{tid}')['data']['status'],'closed')

    def test_tag_filters_latest_pagination_and_featured(self):
        row,_,tag=self.topic()
        rows=self.call('GET',f"/community/feed?tag_id={tag['id']}&sort=latest&per_page=1")
        self.assertEqual(rows['total'],1); self.assertEqual(rows['data'][0]['id'],row['id'])
        self.assertEqual(self.call('GET',f"/community/feed?tag_id={tag['id']}&page=2&per_page=1")['data'],[])
        self.assertEqual(self.call('GET',f"/community/feed?tag_id={tag['id']}&type=article")['total'],0)
        self.call('POST',f"/discussions/threads/{row['id']}/essence",user='admin')
        self.assertEqual(self.call('GET',f"/community/recommendations?tag_id={tag['id']}&featured=true")['total'],1)
        for params in ('page=0','per_page=0','page=x','tag_id=-1','tag_id=x','sort=bad','category=bad','type=bad'):
            self.call('GET','/community/feed?'+params,status=404 if params=='tag_id=-1' else 400)

    def test_original_mixed_articles_and_legacy_global_post(self):
        a=ArticleModel(title='旧版文章回归',introduction='旧版介绍',author_id=2)
        v=ArticleV2Model(title='新版文章回归',introduction='新版介绍',author_id=2,status='published')
        official=ArticleV2Model(title='官方推文',author_id=2,status='published',is_official=True)
        draft=ArticleV2Model(title='草稿不可见',author_id=2,status='draft')
        db.session.add_all([a,v,official,draft]); db.session.commit()
        result=self.call('GET','/community/feed?type=article')['data']
        self.assertEqual({r['title'] for r in result},{'旧版文章回归','新版文章回归'})
        self.assertEqual({r['title'] for r in self.call('GET','/community/spotlight')['data']},{'官方推文'})
        self.call('POST','/discussions/threads',{'scope_type':'global','title':'普通讨论不要求配图','content':'这是一段普通讨论，没有选择阅读标签。'},status=201)

    def test_upload_real_image_invalid_and_limit(self):
        from PIL import Image
        image=io.BytesIO(); Image.new('RGB',(120,160),'white').save(image,format='PNG'); image.seek(0)
        response=self.client.post('/discussions/upload_image',data={'image':(image,'cover.png')},headers=self.headers['stu1'])
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(self.client.get(response.json['url']).status_code,200)
        for value,name in ((b'not an image','bad.png'),(b'x'*(10*1024*1024+1),'huge.png')):
            response=self.client.post('/discussions/upload_image',data={'image':(io.BytesIO(value),name)},headers=self.headers['stu1'])
            self.assertEqual(response.status_code,400,response.json)

    def test_export_all_depths_names_counts_and_formula_safety_readonly(self):
        row,_,tag=self.topic(); root=self.comment(row['id'],content='=SUM(1,2) 原文要安全保留')
        self.comment(row['id'],root['id'],user='stu3',content='短心得也应当完整导出')
        person=db.session.get(PersonModel,db.session.get(User,4).person_id); person.verification_status='unverified'; db.session.commit()
        self.call('DELETE',f"/discussions/threads/{row['id']}")
        engine=readonly_engine('sqlite:///'+self.path.as_posix())
        with engine.connect() as connection:
            rows=list(export_rows(connection,tag['id'],'http://localhost/AMEII'))
            self.assertEqual(len(rows),3)
            self.assertEqual(rows[0][2],''); self.assertEqual(rows[1][2],'=SUM(1,2) 原文要安全保留')
            self.assertEqual(rows[1][1],reading.word_count(rows[1][2])); self.assertIn('昵称',rows[2][3])
            with self.assertRaises(Exception): connection.execute(text("UPDATE community_tag SET name='forbidden'"))
        csvfile=Path(self.tmp.name)/'reading.csv'; xlsx=Path(self.tmp.name)/'reading.xlsx'
        self.assertEqual(write_export(csvfile,rows),3); self.assertEqual(write_export(xlsx,rows),3)
        with csvfile.open(encoding='utf-8-sig') as file:
            data=list(csv.reader(file)); self.assertTrue(data[2][2].startswith("'="))
        from openpyxl import load_workbook
        book=load_workbook(xlsx); self.assertEqual(book.active['C3'].value,rows[1][2]); self.assertEqual(book.active['C3'].data_type,'s'); book.close()
        engine.dispose()

    def test_migration_reentrant_legacy_reply_backfill_and_mysql_ddl(self):
        path=Path(__file__).parent/'migrate/migrate_82_community_reading.py'
        spec=importlib.util.spec_from_file_location('reading_migration',path); module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        engine=create_engine('sqlite:///'+(Path(self.tmp.name)/'migration.sqlite').as_posix())
        with engine.begin() as c:
            c.execute(text('CREATE TABLE user (id INTEGER PRIMARY KEY)'))
            c.execute(text('CREATE TABLE discussion_thread (id INTEGER PRIMARY KEY, author_id INT)'))
            c.execute(text('CREATE TABLE discussion_reply (id INTEGER PRIMARY KEY, author_id INT, thread_id INT, parent_reply_id INT, status VARCHAR(20), created_at DATETIME)'))
            c.execute(text('INSERT INTO discussion_reply(id,thread_id,parent_reply_id) VALUES (1,1,NULL),(2,1,1),(3,1,2)'))
        self.assertEqual(module.apply(engine),2); self.assertEqual(module.apply(engine),0)
        with engine.connect() as c:
            self.assertEqual(c.execute(text('SELECT root_reply_id FROM discussion_reply ORDER BY id')).scalars().all(),[None,1,1])
        from sqlalchemy.schema import CreateTable
        for table in module.TABLES: self.assertIn('CREATE TABLE',str(CreateTable(table).compile(dialect=mysql.dialect())))
        engine.dispose()

    def test_discovery_recent_unique_participants_cache_and_visibility(self):
        from datetime import datetime, timedelta
        from models import CommunityVisit as Visit
        from services.community_discovery import rank_recent
        row, _, tag = self.topic(); tid=row['id']
        self.call('GET',f'/discussions/threads/{tid}')
        self.assertEqual(Visit.query.filter_by(thread_id=tid).count(),0)
        self.call('GET',f"/community/feed?tag_id={tag['id']}",user='stu2')
        self.assertEqual(Visit.query.filter_by(thread_id=tid).count(),0)
        for _ in range(2): self.call('GET',f'/discussions/threads/{tid}',user='stu2')
        self.assertEqual(Visit.query.filter_by(thread_id=tid).count(),1)
        visit=Visit.query.filter_by(thread_id=tid).one()
        visit.last_seen_at=datetime.now()-timedelta(hours=25); db.session.commit()
        self.assertNotIn(tid,rank_recent(tag['id'],datetime.now())['ids'])
        self.call('GET',f'/discussions/threads/{tid}',user='stu2')
        self.comment(tid); self.comment(tid)
        self.assertIn(tid,rank_recent(tag['id'],datetime.now())['ids'])
        url=f"/community/discovery?tag_id={tag['id']}"
        result=self.call('GET',url)['data']
        self.assertEqual(result['hot'],[{'id':tid,'title':row['title']}])
        self.assertEqual(result['refresh_seconds'],900)
        self.assertEqual(result['updated_at'],self.call('GET',url)['data']['updated_at'])
        self.call('POST',f'/discussions/threads/{tid}/essence',user='admin')
        result=self.call('GET',url)['data']
        self.assertEqual(len(result['featured']),1); self.assertEqual(result['hot'],[])
        self.call('POST',f'/discussions/threads/{tid}/hide',user='admin')
        result=self.call('GET',url)['data']
        self.assertEqual(result['featured']+result['hot'],[])
        self.call('GET','/community/discovery?tag_id=bad',status=400)
        allrows=self.call('GET','/community/discovery')['data']
        self.assertLessEqual(len(allrows['featured']),3); self.assertLessEqual(len(allrows['hot']),5)

    def test_search_book_lookup_and_closed_content_privacy(self):
        row,_,tag=self.topic(title='唯一标题检索甲乙',content='一段特殊正文检索丙丁，足够长度发布。')
        base=f"/community/feed?tag_id={tag['id']}&q="
        for q in ('唯一标题检索甲乙','特殊正文检索丙丁','乡土中国'):
            self.assertEqual(self.call('GET',base+q)['total'],1)
        self.assertEqual(self.call('GET',base+'%')['total'],0)
        self.call('GET',base+'x'*101,status=400)
        url=f"/community/books/{row['book_id']}/threads"
        self.assertIn(row['id'],[r['id'] for r in self.call('GET',url)['data']])
        self.call('DELETE',f"/discussions/threads/{row['id']}")
        for q in ('唯一标题检索甲乙','特殊正文检索丙丁'):
            self.assertEqual(self.call('GET',base+q)['total'],0)
        self.assertNotIn(row['id'],[r['id'] for r in self.call('GET',url)['data']])

    def test_discovery_migration_reentrant_and_mysql_ddl(self):
        from scripts.migrate.migrate_83_community_discovery import apply
        from models import CommunityVisit
        from sqlalchemy.schema import CreateTable
        engine=create_engine('sqlite:///'+(Path(self.tmp.name)/'discovery-migration.sqlite').as_posix())
        with engine.begin() as c:
            c.execute(text('CREATE TABLE discussion_thread (id INTEGER PRIMARY KEY, created_at DATETIME, is_essence BOOLEAN)'))
            c.execute(text("INSERT INTO discussion_thread VALUES(1,'2026-10-31 10:00:00',1)"))
        apply(engine); apply(engine)
        with engine.connect() as c:
            self.assertIsNotNone(c.execute(text('SELECT featured_at FROM discussion_thread')).scalar())
        self.assertIn('PRIMARY KEY (thread_id, user_id)',str(CreateTable(CommunityVisit.__table__).compile(dialect=mysql.dialect())))
        engine.dispose()


if __name__ == '__main__':
    unittest.main(verbosity=2)

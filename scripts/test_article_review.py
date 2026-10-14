"""文章先审后发（2026-10-15）隔离检查：送审可见性、审批状态机、过审积分、v1 收口。"""
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta
from flask_jwt_extended import create_access_token
from test_points_sso import _build_app
from exts import db
from models import UserModel, ArticleV2Model, NotificationModel
from points_models import PointsRule, PointsRuleVersion, PointsReward
from blueprints.article_v2 import bp as article_v2_bp
from blueprints.article import bp as article_v1_bp


class ArticleReviewTest(unittest.TestCase):
    def setUp(self):
        self.app,_=_build_app()
        self.app.config['POINTS_REWARDS_ENABLED']=True
        self.app.register_blueprint(article_v2_bp)
        self.app.register_blueprint(article_v1_bp)
        self.ctx=self.app.app_context();self.ctx.push();db.create_all()
        self.manager=self.user('manager',admin=True)
        self.writer=self.user('writer')
        self.reader=self.user('reader')
        self.client=self.app.test_client()
        self.rate=patch('blueprints.article_v2._publish_rate_guard',return_value=None);self.rate.start()
        # 审批后的积分派发是独立通道，测试里不允许碰积分中心
        self.dispatch=patch('blueprints.article_v2.points_dispatch',return_value=False);self.dispatch.start()
        self.network=patch('points_center_client.requests.request',side_effect=AssertionError('真实积分中心请求被测试禁止'));self.network.start()

    def tearDown(self):
        self.rate.stop();self.dispatch.stop();self.network.stop()
        db.session.remove();db.drop_all();self.ctx.pop()

    def user(self,name,admin=False):
        u=UserModel(username=name,email=name+'@mail2.sysu.edu.cn',password='unused',
            role='super_admin' if admin else 'user',status='active',lifecycle='active')
        db.session.add(u);db.session.flush();return u

    def headers(self,u):
        return {'Authorization':'Bearer '+create_access_token(identity=u.email)}

    def article_rule(self,amount=3):
        r=PointsRule(camp_id=None,kind='article',version=1);db.session.add(r);db.session.flush()
        db.session.add(PointsRuleVersion(rule_id=r.id,version=1,amount=amount,period_limit=1,
            enabled=True,effective_at=datetime.now()-timedelta(days=1),created_by=self.manager.id))
        db.session.commit();return r

    def publish(self,u,title='测试文章'):
        return self.client.post('/v2/article/public',headers=self.headers(u),
            json=dict(title=title,introduction='简介',content_md='正文内容足够长'))

    def review(self,u,art,action,reason=None):
        payload=dict(action=action)
        if reason is not None: payload['reason']=reason
        return self.client.post(f'/v2/article/admin/{art.id}/review',
            headers=self.headers(u),json=payload)

    def test_regular_publish_goes_pending_and_hidden(self):
        response=self.publish(self.writer)
        self.assertEqual(response.status_code,200);self.assertIn('审核',response.json['message'])
        art=ArticleV2Model.query.one()
        self.assertEqual(art.status,'pending_review')
        self.assertIsNotNone(art.submitted_at);self.assertIsNone(art.publish_time)
        # 公开列表不含待审文章；陌生人/未登录视为不存在，作者与管理员可见
        self.assertEqual(self.client.get('/v2/article/list').json['data'],[])
        self.assertEqual(self.client.get(f'/v2/article/{art.id}').status_code,404)
        self.assertEqual(self.client.get(f'/v2/article/{art.id}',headers=self.headers(self.reader)).status_code,404)
        self.assertEqual(self.client.get(f'/v2/article/{art.id}',headers=self.headers(self.writer)).status_code,200)
        self.assertEqual(self.client.get(f'/v2/article/{art.id}',headers=self.headers(self.manager)).status_code,200)
        # 我的文章（作者）可见待审
        data=self.client.get('/v2/article/my',headers=self.headers(self.writer)).json['data']
        self.assertEqual([a['status'] for a in data],['pending_review'])

    def test_manager_publish_bypasses_review(self):
        self.publish(self.manager)
        art=ArticleV2Model.query.one()
        self.assertEqual(art.status,'published')
        self.assertIsNotNone(art.publish_time);self.assertIsNone(art.submitted_at)

    def test_review_approve_publishes_and_enqueues_once_per_day(self):
        self.article_rule(3)
        self.publish(self.writer,'第一篇');first=ArticleV2Model.query.filter_by(title='第一篇').one()
        # 非管理员无审批权；管理员审批通过后公开并入队
        self.assertEqual(self.review(self.writer,first,'approve').status_code,403)
        self.assertEqual(self.review(self.manager,first,'approve').status_code,200)
        self.assertEqual(first.status,'published');self.assertIsNotNone(first.publish_time)
        # 重复审批被状态机拒绝
        self.assertEqual(self.review(self.manager,first,'approve').status_code,409)
        # 同日第二篇过审：文章照常公开，积分按提交日只入队一次
        self.publish(self.writer,'第二篇');second=ArticleV2Model.query.filter_by(title='第二篇').one()
        self.assertEqual(self.review(self.manager,second,'approve').status_code,200)
        self.assertEqual(second.status,'published')
        rewards=PointsReward.query.all()
        self.assertEqual(len(rewards),1)
        self.assertEqual(rewards[0].amount,3);self.assertEqual(rewards[0].status,'pending')
        self.assertEqual(rewards[0].event_key,f'article:{self.writer.id}:{first.submitted_at.date()}')
        self.assertEqual(rewards[0].camp_id,None)

    def test_review_reject_returns_draft_with_reason_and_notifies(self):
        self.publish(self.writer);art=ArticleV2Model.query.one()
        # 驳回必须带原因
        self.assertEqual(self.review(self.manager,art,'reject').status_code,400)
        self.assertEqual(self.review(self.manager,art,'reject',reason='内容与训练营无关').status_code,200)
        self.assertEqual(art.status,'draft');self.assertEqual(art.reject_reason,'内容与训练营无关')
        self.assertEqual(NotificationModel.query.filter_by(user_id=self.writer.id).count(),1)
        # 驳回后作者可改稿重投：再次进入待审，刷新提交时间
        response=self.client.post(f'/v2/article/{art.id}/publish',headers=self.headers(self.writer),json={})
        self.assertEqual(response.status_code,200);self.assertEqual(art.status,'pending_review')
        self.assertGreaterEqual(art.submitted_at,art.reviewed_at)

    def test_approve_without_rule_or_flag_off_enqueues_nothing(self):
        # 未配置平台 article 规则：审批只公开不入队
        self.publish(self.writer);art=ArticleV2Model.query.one()
        self.assertEqual(self.review(self.manager,art,'approve').status_code,200)
        self.assertEqual(PointsReward.query.count(),0)
        # 积分开关关闭：同样不入队
        self.app.config['POINTS_REWARDS_ENABLED']=False
        self.publish(self.writer,'第二篇');art2=ArticleV2Model.query.filter_by(title='第二篇').one()
        self.article_rule(3)
        self.assertEqual(self.review(self.manager,art2,'approve').status_code,200)
        self.assertEqual(art2.status,'published');self.assertEqual(PointsReward.query.count(),0)

    def test_v1_publish_blocked_for_regular_users(self):
        response=self.client.post('/article/public',headers=self.headers(self.writer),
            data={'Article_Title':'旧文','Article_Introduction':'旧简介','Html':'<p>旧正文</p>'})
        self.assertEqual(response.status_code,403)
        self.assertEqual(ArticleV2Model.query.count(),0)


if __name__=='__main__':unittest.main()

"""关键积分流程隔离检查：权限、版本、防重复、撤回及不确定结果重试。"""
import unittest
from unittest.mock import patch
from datetime import datetime, date, timedelta, time
from flask_jwt_extended import create_access_token
from test_points_sso import _build_app
from exts import db
from models import UserModel, CampSession, CampMember, CampPolicy, CampStaff, CourseModel, Chapter, CampChapterCertification, CampAttendancePlan, CheckRecord, DiscussionThread
from points_models import PointsRule, PointsRuleVersion, PointsReward, PointsEvent
from services import points
from services.points_identity import PointsIdentity
from points_center_client import PointsCenterError
from blueprints.points import bp


class RewardsTest(unittest.TestCase):
    def setUp(self):
        self.app,_=_build_app()
        self.app.config['POINTS_REWARDS_ENABLED']=True
        self.app.register_blueprint(bp)
        self.ctx=self.app.app_context();self.ctx.push();db.create_all()
        self.admin=self.user('admin',admin=True);self.teacher=self.user('teacher');self.student=self.user('student');self.outsider=self.user('outsider')
        self.camp=CampSession(name='测试营',category='learning',status='running',start_date=date.today()-timedelta(days=10),end_date=date.today()+timedelta(days=10),training_groups_ready=False)
        db.session.add(self.camp);db.session.flush()
        db.session.add(CampMember(camp_session_id=self.camp.id,user_id=self.student.id,role='student',status='active'))
        db.session.add(CampStaff(camp_session_id=self.camp.id,user_id=self.teacher.id,role='teacher',status='active',assigned_by=self.admin.id))
        db.session.commit()
        self.client=self.app.test_client()
        self.actor=self.admin
        self.patch=patch('blueprints.points._current_user',side_effect=lambda:self.actor);self.patch.start()
        self.headers={'Authorization':'Bearer '+create_access_token(identity=str(self.admin.id))}
        self.network=patch('points_center_client.requests.request',side_effect=AssertionError('真实积分中心请求被测试禁止'));self.network.start()

    def tearDown(self):
        self.patch.stop();self.network.stop();db.session.remove();db.drop_all();self.ctx.pop()

    def user(self,name,admin=False):
        u=UserModel(username=name,email=name+'@mail2.sysu.edu.cn',password='unused',role='super_admin' if admin else 'user',status='active',lifecycle='active')
        db.session.add(u);db.session.flush();return u

    def rule(self,kind='contribution',amount=30,limit=3):
        r=PointsRule(camp_id=self.camp.id,kind=kind,version=1);db.session.add(r);db.session.flush()
        db.session.add(PointsRuleVersion(rule_id=r.id,version=1,amount=amount,period_limit=limit,enabled=True,effective_at=datetime.now()-timedelta(days=2),created_by=self.admin.id));db.session.commit();return r

    def reward(self,rule=None,key='event:1',review=False):
        r=points.enqueue(rule or self.rule(),self.student.id,key,datetime.now(),'测试贡献依据',actor=self.admin.id,review=review)
        db.session.commit();return r

    def post(self,path,data):
        return self.client.post('/points'+path,headers=self.headers,json=data)

    def test_read_does_not_provision_and_identity_cannot_be_forged(self):
        self.actor=self.student
        with patch('points_center_client.get_user_points',return_value={'balance':0}) as read,patch('points_center_client.ensure_user') as ensure:
            response=self.client.get('/points/me?user_id=99',headers=self.headers)
            self.assertEqual(response.status_code,200);self.assertEqual(response.json['balance'],0)
            read.assert_called_once_with(str(self.student.id).zfill(7),self.student.email);ensure.assert_not_called()

    def test_unknown_balance_not_zero_and_unlinked_is_explicit(self):
        with patch('points_center_client.get_user_points',side_effect=PointsCenterError('not bound',category='HTTP',status_code=404)):
            r=self.client.get('/points/me',headers=self.headers)
            self.assertIsNone(r.json['balance']);self.assertEqual(r.json['status'],'unlinked')
        with patch('points_center_client.get_user_points',side_effect=PointsCenterError('timeout',category='TIMEOUT')):
            self.assertEqual(self.client.get('/points/me',headers=self.headers).status_code,504)

    def test_same_event_and_monthly_cap(self):
        rule=self.rule(limit=1);a=self.reward(rule)
        self.assertEqual(self.reward(rule).id,a.id)
        with self.assertRaises(points.PointsError):self.reward(rule,'event:2')
        self.assertEqual(PointsReward.query.count(),1)

    def test_rule_versions_apply_at_event_time(self):
        rule=self.rule();old=points.version_at(rule)
        db.session.add(PointsRuleVersion(rule_id=rule.id,version=2,amount=50,period_limit=3,enabled=True,effective_at=datetime.now()+timedelta(days=1),created_by=self.admin.id));rule.version=2;db.session.commit()
        reward=self.reward(rule);self.assertEqual(reward.amount,30);self.assertEqual(reward.rule_version,1)
        self.assertEqual(points.version_at(rule,datetime.now()+timedelta(days=2)).amount,50)

    def test_replay_keeps_idempotency_key_after_uncertain_response(self):
        r=self.reward();key=r.idempotency_key;self.assertEqual(r.status,'pending')
        account={'user_id':100,'email':self.student.email}
        with patch('points_center_client.ensure_user',return_value=account),patch('points_center_client.change_points',side_effect=PointsCenterError('uncertain',category='TIMEOUT')):
            self.assertFalse(points.dispatch(r.id))
        self.assertEqual(r.status,'failed');self.assertIsNone(r.balance_after)
        with patch('points_center_client.ensure_user',return_value=account),patch('points_center_client.change_points',return_value={'transaction_uuid':'tx-1','balance_after':130}) as change:
            self.assertTrue(points.dispatch(r.id));self.assertEqual(change.call_args.args[-1],key)
            self.assertFalse(points.dispatch(r.id));self.assertEqual(change.call_count,1)
        self.assertEqual(r.status,'completed');self.assertEqual(r.attempts,2)

    def test_reversal_waits_for_original_and_only_once(self):
        r=self.reward();r.attempts=1;r.status='failed';r.center_user_id=100;r.center_email=self.student.email;db.session.commit()
        undo=points.reverse(r,self.admin.id,'重复奖励');db.session.commit()
        self.assertEqual(points.reverse(r,self.admin.id,'再次点击').id,undo.id)
        self.assertFalse(points.dispatch(undo.id))
        r.status='completed';r.transaction_uuid='first';r.balance_after=100;db.session.commit()
        with patch('points_center_client.ensure_user',return_value={'user_id':100}),patch('points_center_client.change_points',return_value={'transaction_uuid':'reverse','balance_after':70}) as change:
            self.assertTrue(points.dispatch(undo.id));self.assertEqual(change.call_args.args[1],-30)
        self.assertEqual(r.status,'completed');self.assertEqual(undo.parent_id,r.id)

    def test_processing_lease_prevents_parallel_submit(self):
        r=self.reward();r.status='processing';r.lease_until=datetime.now()+timedelta(minutes=1);db.session.commit()
        with patch('points_center_client.change_points') as change:
            self.assertFalse(points.dispatch(r.id));change.assert_not_called()

    def test_identity_change_never_moves_retry_to_new_account(self):
        r=self.reward();r.center_user_id=100;r.center_email=self.student.email;db.session.commit()
        with patch('points_center_client.ensure_user',return_value={'user_id':999}),patch('points_center_client.change_points') as change:
            self.assertFalse(points.dispatch(r.id));change.assert_not_called()
        self.assertIn('身份已变化',r.last_error)

    def test_cancel_unattempted_never_debits(self):
        r=self.reward();cancel=points.reverse(r,self.admin.id,'撤销');db.session.commit()
        self.assertEqual(cancel.status,'cancelled');self.assertEqual(PointsReward.query.count(),1)
        self.assertFalse(points.dispatch(r.id))

    def test_permission_self_review_and_duplicate_approval(self):
        r=self.reward(review=True)
        self.actor=self.outsider
        self.assertEqual(self.post(f'/manage/rewards/{r.id}/approve',{}).status_code,403)
        self.actor=self.admin
        self.assertEqual(self.post(f'/manage/rewards/{r.id}/approve',{}).status_code,403)
        self.actor=self.teacher
        with patch('services.points.dispatch',return_value=False):
            self.assertEqual(self.post(f'/manage/rewards/{r.id}/approve',{}).status_code,200)
            self.assertEqual(self.post(f'/manage/rewards/{r.id}/approve',{}).status_code,409)
        self.assertEqual(r.reviewed_by,self.teacher.id);self.assertEqual(r.status,'pending')

    def test_personal_records_are_owned_and_pending_not_ledger(self):
        r=self.reward();self.actor=self.student
        self.assertEqual(self.client.get('/points/records',headers=self.headers).json['total'],0)
        self.assertEqual(len(self.client.get(f'/points/pending?camp_id={self.camp.id}',headers=self.headers).json['items']),1)
        r.status='completed';r.completed_at=datetime.now();r.balance_after=30;db.session.commit()
        self.actor=self.outsider
        self.assertEqual(self.client.get('/points/records?user_id='+str(self.student.id),headers=self.headers).json['total'],0)

    def test_rules_reject_nonadmin_invalid_amount_and_stale_version(self):
        data=dict(camp_id=self.camp.id,kind='contribution',amount=30,period_limit=3,enabled=True,effective_at=datetime.now().isoformat(),version=0)
        self.actor=self.teacher;self.assertEqual(self.post('/manage/rules',data).status_code,403)
        self.actor=self.admin
        self.assertEqual(self.post('/manage/rules',{**data,'amount':True}).status_code,400)
        self.assertEqual(self.post('/manage/rules',data).status_code,200)
        self.assertEqual(self.post('/manage/rules',data).status_code,409)

    def test_daily_attendance_uses_existing_definition_and_excludes_today(self):
        rule=self.rule('attendance',5)
        yesterday=date.today()-timedelta(days=1)
        db.session.add(CampAttendancePlan(camp_session_id=self.camp.id,user_id=self.student.id,date=yesterday,expected_check_in=time(8),min_daily_hours=2))
        db.session.add(CheckRecord(user_id=self.student.id,date=yesterday,check_in=datetime.combine(yesterday,time(9)),check_out=datetime.combine(yesterday,time(12)),duration=3))
        db.session.commit();points.collect_automatic();points.collect_automatic()
        self.assertEqual(PointsReward.query.count(),1)
        self.assertEqual(PointsReward.query.one().amount,5)

    def test_chapter_certification_first_only_and_revoke(self):
        rule=self.rule('chapter',20)
        course=CourseModel(title='测试课',introduction='测试',creator_id=self.admin.id);db.session.add(course);db.session.flush()
        chapter=Chapter(name='测试章节',course_id=course.id,level=2);db.session.add(chapter);db.session.flush()
        cert=CampChapterCertification(camp_session_id=self.camp.id,student_user_id=self.student.id,mentor_user_id=self.admin.id,chapter_id=chapter.id,course_id=course.id,certified_at=datetime.now())
        db.session.add(cert);db.session.commit();points.collect_automatic();points.collect_automatic()
        self.assertEqual(PointsReward.query.count(),1)
        cert.score=95;db.session.commit();points.collect_automatic();self.assertEqual(PointsReward.query.count(),1)
        db.session.delete(cert);db.session.commit();points.collect_automatic();self.assertEqual(PointsReward.query.one().status,'cancelled')

    def test_completion_can_resubmit_after_rejection_but_not_after_award(self):
        rule=self.rule('completion',100)
        first=self.reward(rule,'completion:first',review=True)
        first.status='rejected';db.session.commit()
        second=self.reward(rule,'completion:second',review=True)
        self.assertNotEqual(first.id,second.id)
        with self.assertRaises(points.PointsError):self.reward(rule,'completion:third',review=True)

    def test_record_reversal_requires_reason_and_cannot_cross_camp(self):
        r=self.reward()
        self.assertEqual(self.post(f'/manage/rewards/{r.id}/reverse',{'reason':''}).status_code,400)
        self.actor=self.outsider
        self.assertEqual(self.post(f'/manage/rewards/{r.id}/reverse',{'reason':'撤销'}).status_code,403)
        self.assertEqual(self.client.get(f'/points/manage/rewards?camp_id={self.camp.id}',headers=self.headers).status_code,403)

    def test_client_rejects_mismatched_identity_and_unconfirmed_result(self):
        import points_center_client as client
        with patch.object(client,'_request',return_value={'platform_user_id':'other','email':self.student.email,'balance':10,'user_id':100}):
            with self.assertRaises(PointsCenterError):client.get_user_points('0000003',self.student.email)
        with patch.object(client,'_request',return_value={'transaction_uuid':'tx','idempotency_key':'wrong','amount':20,'balance_after':100}):
            with self.assertRaises(PointsCenterError):client.change_points('0000003',20,'章节奖励','stable-key')
        with patch.object(client,'_request') as request:
            with self.assertRaises(PointsCenterError):client.change_points('0000003',True,'章节奖励','stable-key')
            request.assert_not_called()

    def test_late_response_cannot_overwrite_a_newer_attempt(self):
        r=self.reward()
        def newer_attempt(*args):
            current=db.session.get(PointsReward,r.id)
            current.attempts+=1;current.status='failed';db.session.commit()
            return {'transaction_uuid':'late-response','balance_after':130}
        with patch('points_center_client.ensure_user',return_value={'user_id':100}),patch('points_center_client.change_points',side_effect=newer_attempt):
            self.assertFalse(points.dispatch(r.id))
        self.assertEqual(r.status,'failed');self.assertIsNone(r.transaction_uuid)

    def test_certification_hook_is_transactional_and_flag_off_is_noop(self):
        rule=self.rule('chapter',20)
        from types import SimpleNamespace
        cert=SimpleNamespace(camp_session_id=self.camp.id,student_user_id=self.student.id,
            mentor_user_id=self.admin.id,chapter_id=999,course_id=1,certified_at=datetime.now())
        points.capture_certification(cert)
        self.assertEqual(PointsReward.query.count(),1)
        db.session.rollback()
        self.assertEqual(PointsReward.query.count(),0)
        self.app.config['POINTS_REWARDS_ENABLED']=False
        points.capture_certification(cert)
        self.assertEqual(PointsReward.query.count(),0)

    def test_personal_catalog_contains_only_unconfigured_platform_paths(self):
        self.rule('chapter',20)
        response=self.client.get('/points/tasks',headers=self.headers)
        self.assertEqual(response.status_code,200)
        self.assertEqual({r['kind'] for r in response.json['items']},{'daily_checkin','community_post','article'})
        self.assertTrue(all(r['scope']=='platform' and not r['enabled'] and r['amount'] is None for r in response.json['items']))
        self.assertTrue(all('camp_id' not in r for r in response.json['items']))

    def platform_rule(self,kind='daily_checkin',amount=1,**extra):
        """API 路径创建平台规则（生效时间只能取当下/未来，验证保存与目录）。"""
        self.actor=self.admin
        response=self.post('/manage/rules',dict(kind=kind,amount=amount,enabled=True,
            effective_at=datetime.now().isoformat(),version=0,**extra))
        self.assertEqual(response.status_code,200)
        return response.json['item']

    def platform_rule_backdated(self,kind='daily_checkin',amount=1,**extra):
        """直建回溯生效的平台规则（扫描公式测试用；API 不允许回溯生效）。"""
        r=PointsRule(camp_id=None,kind=kind,version=1);db.session.add(r);db.session.flush()
        db.session.add(PointsRuleVersion(rule_id=r.id,version=1,amount=amount,period_limit=1,
            enabled=True,effective_at=datetime.now()-timedelta(days=3),created_by=self.admin.id,**extra))
        db.session.commit();return r

    def test_platform_rules_admin_only_and_daily_checkin_formula(self):
        # 非管理员不能配平台规则；管理员经 API 配置成功（生效即刻，目录可见）
        self.actor=self.teacher
        self.assertEqual(self.post('/manage/rules',dict(kind='daily_checkin',amount=1,enabled=True,
            effective_at=datetime.now().isoformat(),version=0)).status_code,403)
        self.platform_rule('daily_checkin',1,bonus_unit_hours=2,bonus_amount=1,bonus_cap=5)
        # 回溯生效的规则驱动扫描：前天两段已签退合计 4.5h（1+floor(4.5/2)=2 → 3 分），未签退段不计
        self.platform_rule_backdated('daily_checkin',1,bonus_unit_hours=2,bonus_amount=1,bonus_cap=5)
        day=date.today()-timedelta(days=2)
        db.session.add(CheckRecord(user_id=self.student.id,date=day,check_in=datetime.combine(day,time(8)),check_out=datetime.combine(day,time(11)),duration=3))
        db.session.add(CheckRecord(user_id=self.student.id,date=day,check_in=datetime.combine(day,time(13)),check_out=datetime.combine(day,time(14,30)),duration=1.5))
        db.session.add(CheckRecord(user_id=self.student.id,date=day,check_in=datetime.combine(day,time(20))))
        # 今天的不结算（次日口径）
        db.session.add(CheckRecord(user_id=self.student.id,date=date.today(),check_in=datetime.combine(date.today(),time(9)),check_out=datetime.combine(date.today(),time(12)),duration=3))
        # 平台规则不要求营员身份：非营员打卡同样结算（20 分钟 → 仅基础分）
        db.session.add(CheckRecord(user_id=self.outsider.id,date=day,check_in=datetime.combine(day,time(9)),check_out=datetime.combine(day,time(9,20)),duration=1/3))
        db.session.commit()
        # 平台规则与营期规则共存时扫描不崩（回归：营期循环必须过滤 NULL camp）
        self.rule('chapter',20)
        points.collect_automatic();points.collect_automatic()
        self.assertEqual({(r.user_id,r.amount) for r in PointsReward.query.all()},{(self.student.id,3),(self.outsider.id,1)})
        # 任务目录返回真数据卡，且不带营期字段
        self.actor=self.student
        items=self.client.get('/points/tasks',headers=self.headers).json['items']
        card=[r for r in items if r['kind']=='daily_checkin'][0]
        self.assertTrue(card['enabled']);self.assertEqual(card['amount'],1);self.assertFalse(card['done_today'])
        self.assertTrue(all('camp_id' not in r for r in items))

    def test_platform_daily_checkin_respects_settle_cutoff(self):
        self.platform_rule_backdated('daily_checkin',1)
        yesterday=date.today()-timedelta(days=1)
        db.session.add(CheckRecord(user_id=self.student.id,date=yesterday,check_in=datetime.combine(yesterday,time(9)),check_out=datetime.combine(yesterday,time(12)),duration=3))
        db.session.commit()
        rule=PointsRule.query.filter_by(camp_id=None,kind='daily_checkin').one()
        first=PointsRuleVersion.query.filter_by(rule_id=rule.id).order_by(PointsRuleVersion.effective_at).first()
        # 跨天签退保护：D+1 凌晨 04:00 前不结算 D 日（人可能还坐着或刚跨天签退）
        points._collect_daily_checkin(rule,first,datetime.combine(date.today(),time(3,0)))
        self.assertEqual(PointsReward.query.count(),0)
        points._collect_daily_checkin(rule,first,datetime.combine(date.today(),time(5,0)))
        self.assertEqual(PointsReward.query.count(),1)

    def test_community_post_scan_first_per_day_only(self):
        self.platform_rule_backdated('community_post',1)
        day=date.today()-timedelta(days=1)
        db.session.add(DiscussionThread(scope_type='global',title='t1',content='c1',author_id=self.student.id,status='normal',created_at=datetime.combine(day,time(10))))
        db.session.add(DiscussionThread(scope_type='global',title='t2',content='c2',author_id=self.student.id,status='normal',created_at=datetime.combine(day,time(15))))
        db.session.add(DiscussionThread(scope_type='global',title='today',content='c',author_id=self.student.id,status='normal',created_at=datetime.now()))
        db.session.commit()
        points.collect_automatic();points.collect_automatic()
        rewards=PointsReward.query.all()
        self.assertEqual(len(rewards),1)
        self.assertEqual(rewards[0].event_key,f'community_post:{self.student.id}:{day}')

    def test_identity_ineligible_blocks_and_manual_retry(self):
        self.platform_rule_backdated('community_post',1)
        visitor=UserModel(username='visitor',email='visitor@example.com',password='x',role='user',status='active',lifecycle='active')
        db.session.add(visitor);db.session.flush()
        day=date.today()-timedelta(days=1)
        db.session.add(DiscussionThread(scope_type='global',title='v',content='c',author_id=visitor.id,status='normal',created_at=datetime.combine(day,time(10))))
        db.session.commit()
        points.collect_automatic()
        r=PointsReward.query.one()
        self.assertEqual(r.status,'pending')
        # 身份不合格：落 blocked、不自动重试，也不被 worker 扫走
        self.assertFalse(points.dispatch(r.id))
        r=db.session.get(PointsReward,r.id)
        self.assertEqual(r.status,'blocked');self.assertIsNone(r.next_attempt_at)
        points.run_worker()
        self.assertEqual(db.session.get(PointsReward,r.id).status,'blocked')
        # 平台发放记录仅管理员可见；管理员手动 retry 后复位 failed 等待 worker
        self.actor=self.teacher
        self.assertEqual(self.client.get('/points/manage/platform/rewards',headers=self.headers).status_code,403)
        self.actor=self.admin
        self.assertEqual(self.client.get('/points/manage/platform/rewards',headers=self.headers).json['total'],1)
        with patch('services.points.dispatch',return_value=False):
            self.assertEqual(self.post(f'/manage/rewards/{r.id}/retry',{}).status_code,200)
        r=db.session.get(PointsReward,r.id)
        self.assertEqual(r.status,'failed');self.assertIsNotNone(r.next_attempt_at)

    def test_camp_progress_is_scoped_to_camp_and_current_user(self):
        first=self.reward(self.rule('chapter',20),'first:reward')
        original=self.camp
        self.camp=CampSession(name='另一期',category='learning',status='running',start_date=date.today(),end_date=date.today()+timedelta(days=10))
        db.session.add(self.camp);db.session.flush()
        db.session.add(CampMember(camp_session_id=self.camp.id,user_id=self.student.id,role='student',status='active'))
        db.session.commit()
        self.reward(self.rule('chapter',50),'second:reward')
        self.actor=self.student
        result=self.client.get(f'/points/camps/{original.id}/rewards',headers=self.headers)
        self.assertEqual([r['id'] for r in result.json['items']],[first.id])
        tasks=self.client.get(f'/points/camps/{original.id}/tasks',headers=self.headers)
        self.assertEqual([r['camp_id'] for r in tasks.json['items']],[original.id])
        self.assertEqual(self.client.get('/points/pending',headers=self.headers).status_code,400)
        self.actor=self.outsider
        self.assertEqual(self.client.get(f'/points/camps/{original.id}/rewards',headers=self.headers).status_code,403)
        self.assertEqual(self.client.get(f'/points/camps/{original.id}/tasks',headers=self.headers).status_code,403)
        self.actor=self.teacher
        self.assertEqual(self.client.get(f'/points/manage/meta?camp_id={self.camp.id}',headers=self.headers).status_code,403)
        self.assertEqual([r['id'] for r in self.client.get(f'/points/manage/meta?camp_id={original.id}',headers=self.headers).json['camps']],[original.id])

    def test_personal_summary_no_longer_contains_camp_administration(self):
        with patch('points_center_client.get_user_points',return_value={'balance':1280}):
            result=self.client.get('/points/me',headers=self.headers).json
        self.assertNotIn('can_manage',result)
        self.assertNotIn('pending_total',result)


if __name__=='__main__':unittest.main()

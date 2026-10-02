"""D5 收尾 P0-1：identity outbox 消费者的隔离回归（SQLite）。

覆盖：正常投递（双端差异化文案）/幂等（sent 不再投）/未知载荷退避转终态/
调度入口可用（不真正起 scheduler，只测 deliver_pending）。
用法（项目根）：.venv/bin/python scripts/test_identity_outbox.py
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from flask import Flask
from sqlalchemy.dialects.mysql import LONGTEXT, MEDIUMTEXT
from sqlalchemy.ext.compiler import compiles


@compiles(MEDIUMTEXT, 'sqlite')
@compiles(LONGTEXT, 'sqlite')
def _mysql_text_on_sqlite(_element, _compiler, **_kwargs):
    return 'TEXT'


from exts import db  # noqa: E402
import models  # noqa: E402,F401


class OutboxWorkerTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        db.init_app(self.app)
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        from services.identity import outbox_worker
        from services.identity import person as identity_person
        from models import UserModel
        self.worker = outbox_worker
        self.UserModel = UserModel
        self.identity_person = identity_person

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def _setup_case(self):
        a = self.UserModel(username='甲', email='a@x.dev')
        b = self.UserModel(username='乙', email='b@x.dev')
        for u in (a, b):
            u.set_password('x' * 32)
            db.session.add(u)
            db.session.flush()
            self.identity_person.create_provisional(u)
        case = models.AccountLinkCaseModel(id='case01', account_a=a.id, account_b=b.id,
                                           state='applied')
        db.session.add(case)
        db.session.flush()
        db.session.add(models.IdentityOutboxModel(
            event_id=1, channel='notification',
            payload={'case_id': 'case01', 'action': 'link_applied', 'to_user': a.id}))
        db.session.add(models.IdentityOutboxModel(
            event_id=1, channel='notification',
            payload={'case_id': 'case01', 'action': 'link_applied', 'to_user': b.id}))
        db.session.commit()
        return a, b

    def test_deliver_both_sides_and_idempotent(self):
        a, b = self._setup_case()
        sent, failed = self.worker.deliver_pending()
        db.session.commit()
        self.assertEqual((sent, failed), (2, 0))
        notes = models.NotificationModel.query.all()
        self.assertEqual(len(notes), 2)
        by_user = {n.user_id: n for n in notes}
        self.assertIn('并入你当前的人员档案', by_user[a.id].content)   # 存续方
        self.assertIn('转为「已合并」状态', by_user[b.id].content)     # 被归并方
        self.assertEqual(by_user[a.id].source_type, 'identity')
        # 幂等：已 sent 的行不再投递
        sent2, failed2 = self.worker.deliver_pending()
        db.session.commit()
        self.assertEqual((sent2, failed2), (0, 0))
        self.assertEqual(models.NotificationModel.query.count(), 2)

    def test_unknown_payload_backoff_to_failed(self):
        db.session.add(models.IdentityOutboxModel(
            event_id=1, channel='notification',
            payload={'action': 'ghost', 'to_user': 1}))
        db.session.commit()
        for _ in range(self.worker.MAX_ATTEMPTS):
            sent, failed = self.worker.deliver_pending()
            db.session.commit()
            self.assertEqual((sent, failed), (0, 1))
        row = models.IdentityOutboxModel.query.one()
        self.assertEqual(row.delivery_state, 'failed')   # 终态，不再重试
        self.assertIn('未知载荷', row.last_error)
        sent, failed = self.worker.deliver_pending()
        db.session.commit()
        self.assertEqual((sent, failed), (0, 0))          # failed 不再被扫


if __name__ == '__main__':
    unittest.main(verbosity=2)

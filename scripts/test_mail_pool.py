"""Isolated SMTP pool regression: real Lua via fakeredis, no network or emails.

Install scripts/requirements-mail-test.txt, then run python scripts/test_mail_pool.py.
"""

import json
import os
import smtplib
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from email import policy
from email.parser import BytesParser
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("JWT_SECRET", "isolated-mail-test-secret-at-least-32-characters")

import fakeredis
from flask import Flask
from flask_mail import BadHeaderError, Connection, Message
from services.mail_pool import MailPoolError, PoolMail


class MailPoolTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {f"POOL_TEST_{i}": f"test-only-secret-{i}" for i in range(5)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.server = fakeredis.FakeServer()
        self.redis = fakeredis.FakeRedis(server=self.server)
        self.app, self.mail = self.make_app()
        self.context = self.app.app_context()
        self.context.push()
        self.addCleanup(self.context.pop)
        self.smtp = Mock()
        self.smtp.sendmail.return_value = {}
        self.smtp_patch = patch("services.mail_pool.smtplib.SMTP_SSL", return_value=self.smtp)
        self.factory = self.smtp_patch.start()
        self.addCleanup(self.smtp_patch.stop)

    def make_app(self, **config):
        app = Flask(__name__)
        app.config.update(TESTING=True, MAIL_SUPPRESS_SEND=False, MAIL_POOL_ENABLED=True,
                          MAIL_POOL_ACCOUNTS=json.dumps([
                              {"username": f"pool{i}@163.com", "password_env": f"POOL_TEST_{i}"}
                              for i in range(5)]))
        app.config.update(config)
        mail = PoolMail(redis_client=fakeredis.FakeRedis(server=self.server))
        mail.init_app(app)
        return app, mail

    @property
    def pool(self):
        return self.app.extensions["mail_pool"]

    def message(self, **kwargs):
        return Message("测试验证码", recipients=["student@example.org"], body="test-only-code", **kwargs)

    def clear_gaps(self, pool=None):
        pool = pool or self.pool
        for account in pool.accounts:
            self.redis.delete(pool.keys(account)[0])

    def test_five_accounts_rotate_and_wrap(self):
        for _ in range(5):
            self.mail.send(self.message())
        self.clear_gaps()
        self.mail.send(self.message())
        self.assertEqual([c.args[0] for c in self.smtp.login.call_args_list],
                         [f"pool{i}@163.com" for i in [0, 1, 2, 3, 4, 0]])
        self.assertEqual(self.factory.call_args.kwargs["timeout"], 5)
        self.assertTrue(self.factory.call_args.kwargs["context"].check_hostname)
        self.smtp.set_debuglevel.assert_not_called()

    def test_reordered_workers_share_cursor_and_account_limits(self):
        app2, mail2 = self.make_app()
        self.mail.send(self.message())
        with app2.app_context():
            mail2.send(self.message())
        self.assertEqual([c.args[0] for c in self.smtp.login.call_args_list],
                         ["pool0@163.com", "pool1@163.com"])
        rows = json.loads(self.app.config["MAIL_POOL_ACCOUNTS"])[::-1]
        app3, _ = self.make_app(MAIL_POOL_ACCOUNTS=rows)
        pool3 = app3.extensions["mail_pool"]
        self.assertEqual(pool3.keys(pool3.accounts[-1]), self.pool.keys(self.pool.accounts[0]))

    def test_concurrent_reservations_are_atomic(self):
        pools = [self.make_app()[0].extensions["mail_pool"] for _ in range(10)]
        barrier = threading.Barrier(10)

        def reserve(i):
            barrier.wait()
            try:
                return pools[i].reserve(set(), 1, f"worker-{i}").username
            except MailPoolError as exc:
                return str(exc)

        with ThreadPoolExecutor(max_workers=10) as executor:
            results = list(executor.map(reserve, range(10)))
        successful = [result for result in results if "@" in result]
        self.assertEqual(len(successful), 5)
        self.assertEqual(len(set(successful)), 5)
        self.assertEqual(results.count("MAIL_POOL_BUSY_OR_LIMITED"), 5)

    def test_pool_busy_does_not_fallback_or_sleep(self):
        for _ in range(5):
            self.mail.send(self.message())
        with self.assertRaisesRegex(MailPoolError, "BUSY_OR_LIMITED"):
            self.mail.send(self.message())
        self.assertEqual(self.factory.call_count, 5)

    def test_minute_and_day_budgets_count_all_recipients(self):
        for budget in ("MAIL_POOL_PER_MINUTE", "MAIL_POOL_PER_DAY"):
            with self.subTest(budget=budget):
                self.redis.flushdb()
                app, mail = self.make_app(**{budget: 2})
                with app.app_context():
                    for _ in range(5):
                        mail.send(self.message(cc=["second@example.org"]))
                    pool = app.extensions["mail_pool"]
                    self.clear_gaps(pool)
                    with self.assertRaisesRegex(MailPoolError, "BUSY_OR_LIMITED"):
                        mail.send(self.message())
                    for account in pool.accounts:
                        keys = pool.keys(account)
                        self.assertEqual(int(self.redis.get(keys[1])), 2)
                        self.assertEqual(int(self.redis.get(keys[2])), 2)
                        self.assertGreater(self.redis.ttl(keys[1]), 0)
                        self.assertGreater(self.redis.ttl(keys[2]), 0)
                        self.assertLessEqual(self.redis.ttl(keys[2]), 86400)

    def test_sender_body_reply_to_and_attachment_preserved(self):
        msg = self.message(sender="old-owner@example.org", html="<b>测试</b>",
                           reply_to="support@example.org", bcc=["private@example.org"])
        msg.attach("guide.txt", "text/plain", b"attachment")
        with self.mail.record_messages() as outbox:
            self.mail.send(msg)
        args = self.smtp.sendmail.call_args.args
        parsed = BytesParser(policy=policy.default).parsebytes(args[2])
        self.assertEqual(args[0], "pool0@163.com")
        self.assertEqual(set(args[1]), {"student@example.org", "private@example.org"})
        self.assertIn("pool0@163.com", str(parsed["From"]))
        self.assertEqual(parsed["Reply-To"], "support@example.org")
        self.assertIsNone(parsed["Bcc"])
        self.assertEqual(next(parsed.iter_attachments()).get_payload(decode=True), b"attachment")
        self.assertEqual(msg.sender, "old-owner@example.org")
        self.assertEqual(outbox[0].msgId, msg.msgId)

    def test_auth_failure_switches_sender_with_cooldown(self):
        bad, good = Mock(), self.smtp
        bad.login.side_effect = smtplib.SMTPAuthenticationError(535, b"secret-server-response")
        self.factory.side_effect = [bad, good]
        with self.assertLogs(self.app.logger, level="WARNING") as logs:
            self.mail.send(self.message())
        self.assertNotIn("secret-server-response", " ".join(logs.output))
        self.assertNotIn("test-only-secret", " ".join(logs.output))
        bad.sendmail.assert_not_called()
        bad.close.assert_called_once()
        self.assertEqual(good.sendmail.call_args.args[0], "pool1@163.com")
        keys = self.pool.keys(self.pool.accounts[0])
        self.assertGreater(self.redis.ttl(keys[3]), 0)
        self.assertFalse(self.redis.exists(keys[4]))
        self.assertEqual(int(self.redis.get(keys[2])), 1)

    def test_connection_attempts_are_bounded(self):
        self.factory.side_effect = TimeoutError("secret-server-response")
        with self.assertRaisesRegex(MailPoolError, "CONNECTION_FAILED"):
            self.mail.send(self.message())
        self.assertEqual(self.factory.call_count, 3)

    def test_delivery_disconnect_never_resends(self):
        for error in (TimeoutError("private"), smtplib.SMTPServerDisconnected("private")):
            with self.subTest(error=type(error).__name__):
                self.redis.flushdb()
                self.factory.reset_mock()
                self.smtp.sendmail.side_effect = error
                with self.assertRaisesRegex(MailPoolError, "DELIVERY_UNCERTAIN"):
                    self.mail.send(self.message())
                self.assertEqual(self.factory.call_count, 1)

    def test_explicit_rejection_never_cycles_through_senders(self):
        for error, code in (
            (smtplib.SMTPRecipientsRefused({"student@example.org": (550, b"private")}), "RECIPIENT_REJECTED"),
            (smtplib.SMTPDataError(451, b"private"), "MESSAGE_REJECTED"),
            (smtplib.SMTPSenderRefused(550, b"private", "private@example.org"), "MESSAGE_REJECTED"),
        ):
            with self.subTest(code=code):
                self.redis.flushdb()
                self.factory.reset_mock()
                self.smtp.sendmail.side_effect = error
                with self.assertRaisesRegex(MailPoolError, code):
                    self.mail.send(self.message())
                self.assertEqual(self.factory.call_count, 1)

    def test_partial_delivery_is_reported_without_retry(self):
        self.smtp.sendmail.return_value = {"second@example.org": (550, b"private")}
        with self.assertRaisesRegex(MailPoolError, "PARTIAL_DELIVERY"):
            self.mail.send(self.message(cc=["second@example.org"]))
        self.assertEqual(self.factory.call_count, 1)

    def test_cleanup_failure_does_not_change_accepted_result(self):
        self.smtp.close.side_effect = OSError("private")
        real_eval = self.pool.redis.eval

        def fail_release(script, n, *args):
            if n == 1:
                raise ConnectionError("private")
            return real_eval(script, n, *args)

        with patch.object(self.pool.redis, "eval", side_effect=fail_release):
            with self.mail.record_messages() as outbox:
                self.mail.send(self.message())
        self.assertEqual(len(outbox), 1)
        self.assertEqual(self.factory.call_count, 1)

    def test_redis_outage_fails_closed(self):
        self.server.connected = False
        with self.assertRaisesRegex(MailPoolError, "COORDINATION_UNAVAILABLE"):
            self.mail.send(self.message())
        self.factory.assert_not_called()

    def test_release_does_not_delete_new_owner(self):
        account = self.pool.reserve(set(), 1, "old")
        key = self.pool.keys(account)[4]
        self.redis.set(key, "new", ex=30)
        self.pool.release(account, "old")
        self.assertEqual(self.redis.get(key), b"new")

    def test_expired_cooldown_rejoins_pool(self):
        first = self.pool.accounts[0]
        self.pool.cool(first)
        self.mail.send(self.message())
        self.assertEqual(self.smtp.login.call_args.args[0], "pool1@163.com")
        self.redis.delete(self.pool.keys(first)[3])
        for _ in range(4):
            self.mail.send(self.message())
        self.assertEqual(self.smtp.login.call_args.args[0], "pool0@163.com")

    def test_suppression_uses_no_smtp_or_redis(self):
        self.app.extensions["mail"].suppress = True
        self.server.connected = False
        with self.mail.record_messages() as outbox:
            self.mail.send_message("suppressed", recipients=["student@example.org"])
            with self.mail.connect() as conn:
                conn.send_message("suppressed2", recipients=["student@example.org"])
        self.assertEqual(len(outbox), 2)
        self.factory.assert_not_called()

    def test_disabled_pool_keeps_legacy_api(self):
        app, mail = self.make_app(MAIL_POOL_ENABLED=False, MAIL_POOL_ACCOUNTS="invalid ignored",
                                  MAIL_SUPPRESS_SEND=True, MAIL_DEFAULT_SENDER="legacy@example.org")
        with app.app_context(), mail.record_messages() as outbox:
            self.assertIsInstance(mail.connect(), Connection)
            mail.send(self.message())
            mail.send_message("legacy", recipients=["student@example.org"])
        self.assertEqual([m.sender for m in outbox], ["legacy@example.org"] * 2)

    def test_two_mailboxes_are_supported(self):
        rows = json.loads(self.app.config["MAIL_POOL_ACCOUNTS"])[:2]
        app, mail = self.make_app(MAIL_POOL_ACCOUNTS=rows)
        with app.app_context():
            mail.send(self.message())
            mail.send(self.message())
        self.assertEqual(self.smtp.login.call_count, 2)

    def test_invalid_config_rejected_without_values(self):
        for config in (
            {"MAIL_POOL_ACCOUNTS": "private broken json"},
            {"MAIL_POOL_ACCOUNTS": []},
            {"MAIL_POOL_ACCOUNTS": [{"username": "private@163.com", "password_env": "MISSING_TEST_AUTH"}]},
            {"MAIL_POOL_ACCOUNTS": [{"username": "private@163.com", "password": "private"}]},
            {"MAIL_POOL_ACCOUNTS": [{"username": "x@163.com", "password_env": "POOL_TEST_0"}] * 2},
            {"MAIL_POOL_TIMEOUT": "private"},
            {"MAIL_POOL_PER_DAY": 0},
            {"MAIL_POOL_SENDER_NAME": "private\r\nBcc: evil@example.org"},
            {"MAIL_POOL_ENABLED": "private typo"},
        ):
            with self.subTest(fields=list(config)):
                with self.assertRaises(ValueError) as caught:
                    self.make_app(**config)
                self.assertNotIn("private", str(caught.exception))
        self.assertNotIn("test-only-secret", repr(self.pool.accounts))

    def test_bad_message_does_not_consume_budget(self):
        with self.assertRaises(BadHeaderError):
            self.mail.send(Message("bad\nheader", recipients=["student@example.org"]))
        with self.assertRaises(ValueError):
            self.mail.send(Message("no recipients"))
        self.assertEqual(self.redis.dbsize(), 0)
        self.factory.assert_not_called()

    def test_captcha_route_returns_503_and_clears_challenge_on_pool_failure(self):
        # Exercise the real route; isolate challenge creation, Redis and audit DB.
        Path("log").mkdir(exist_ok=True)
        import exts
        import blueprints
        from blueprints import auth
        self.app.config["RATELIMIT_ENABLED"] = False
        exts.limiter.init_app(self.app)
        self.app.register_blueprint(auth.bp)
        recipient = "student@example.org"
        key = f"captcha:register:{recipient}"
        cd = f"captcha:cd:register:{recipient}"

        def issue(*_args):
            self.redis.set(key, "test-digest", ex=300)
            self.redis.set(cd, "1", ex=60)
            return "test-only-code"

        with patch.object(auth, "issue_captcha", side_effect=issue), \
             patch.object(auth, "mail", self.mail), \
             patch.object(exts, "redis_client", self.redis), \
             patch.object(blueprints, "UserModel") as users, \
             patch.object(blueprints, "_audit_write"):
            users.query.filter_by.return_value.first.return_value = None
            response = self.app.test_client().post("/auth/captcha/email", json={
                "User_Email": recipient, "purpose": "register"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(self.smtp.sendmail.call_count, 1)
            self.factory.side_effect = OSError("private")
            response = self.app.test_client().post("/auth/captcha/email", json={
                "User_Email": recipient, "purpose": "register"})
        self.assertEqual(response.status_code, 503)
        self.assertFalse(self.redis.exists(key))
        self.assertFalse(self.redis.exists(cd))
        self.assertNotIn("private", response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main(verbosity=2)

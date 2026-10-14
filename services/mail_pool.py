"""Optional Redis-coordinated SMTP pool behind the existing Flask-Mail API."""

import copy
import hashlib
import json
import os
import re
import smtplib
import ssl
import time
import uuid
from dataclasses import dataclass, field

from flask import current_app
from flask_mail import BadHeaderError, Mail, Message, email_dispatched, sanitize_addresses
from redis import Redis


class MailPoolError(RuntimeError):
    """Public error codes never contain SMTP responses, credentials or recipients."""


@dataclass(frozen=True)
class Account:
    username: str
    password: str = field(repr=False)

    @property
    def key(self):
        return hashlib.sha256(self.username.lower().encode()).hexdigest()[:24]


def _integer(config, name, default, minimum, maximum):
    try:
        value = int(config.get(name, default))
        if not minimum <= value <= maximum:
            raise ValueError
        return value
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an integer in [{minimum}, {maximum}]") from None


def _enabled(value):
    if value is True or str(value).lower() in ("1", "true", "on"):
        return True
    if value is False or value is None or str(value).lower() in ("0", "false", "off", ""):
        return False
    raise ValueError("MAIL_POOL_ENABLED must be true or false")


# One atomic reservation across workers: cursor, per-account gap, minute/day
# recipient budgets, failure cooldown and in-flight ownership. Counters include
# failed attempts; they deliberately are not refunded during provider outages.
_RESERVE = """
local n = tonumber(ARGV[1])
local start = tonumber(redis.call('GET', KEYS[1]) or '0') % n
local cost = tonumber(ARGV[2])
local global_owner = redis.call('GET', KEYS[2])
if tonumber(ARGV[8]) > 0 and global_owner and global_owner ~= ARGV[6] then
    return 0
end
for offset = 1, n do
    local i = (start + offset - 1) % n + 1
    local k = 3 + (i - 1) * 5
    if ARGV[8 + i] == '1'
       and redis.call('EXISTS', KEYS[k], KEYS[k+3], KEYS[k+4]) == 0
       and tonumber(redis.call('GET', KEYS[k+1]) or '0') + cost <= tonumber(ARGV[3])
       and tonumber(redis.call('GET', KEYS[k+2]) or '0') + cost <= tonumber(ARGV[4]) then
        redis.call('SET', KEYS[1], i, 'EX', 86400)
        redis.call('SET', KEYS[k], '1', 'EX', ARGV[5])
        if redis.call('INCRBY', KEYS[k+1], cost) == cost then
            redis.call('EXPIRE', KEYS[k+1], 60)
        end
        if redis.call('INCRBY', KEYS[k+2], cost) == cost then
            redis.call('EXPIRE', KEYS[k+2], 86400)
        end
        redis.call('SET', KEYS[k+4], ARGV[6], 'EX', ARGV[7])
        if tonumber(ARGV[8]) > 0 then
            redis.call('SET', KEYS[2], ARGV[6], 'EX', ARGV[7])
        end
        return i
    end
end
return 0
"""

_RELEASE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""

_FINISH_GLOBAL = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    redis.call('SET', KEYS[1], 'cooldown', 'EX', ARGV[2])
    return 1
end
return 0
"""


class Pool:
    def __init__(self, app, redis_client=None):
        config = app.config
        try:
            raw = config.get("MAIL_POOL_ACCOUNTS", "[]")
            rows = json.loads(raw) if isinstance(raw, str) else raw
            if not isinstance(rows, list) or not 1 <= len(rows) <= 20:
                raise ValueError
            accounts = []
            for row in rows:
                if not isinstance(row, dict) or set(row) != {"username", "password_env"}:
                    raise ValueError
                username, env = row["username"], row["password_env"]
                if not isinstance(username, str) or not re.fullmatch(r"[^\s<>@]+@[^\s<>@]+\.[^\s<>@]+", username):
                    raise ValueError
                if not isinstance(env, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", env):
                    raise ValueError
                password = os.environ.get(env)
                if not password or not password.strip():
                    raise ValueError
                accounts.append(Account(username, password))
            if len({a.key for a in accounts}) != len(accounts):
                raise ValueError
        except (TypeError, ValueError, KeyError):
            raise ValueError("MAIL_POOL_ACCOUNTS invalid: require unique mailboxes and populated password_env references") from None
        self.accounts = tuple(accounts)
        self.host = config.get("MAIL_POOL_SERVER", "smtp.163.com")
        self.name = config.get("MAIL_POOL_SENDER_NAME", "BME 训练营")
        namespace = config.get("MAIL_POOL_NAMESPACE", "bme:mail-pool:v1")
        if not isinstance(self.host, str) or not self.host or re.search(r"[\s/]", self.host):
            raise ValueError("MAIL_POOL_SERVER invalid")
        if not isinstance(self.name, str) or any(c in self.name for c in "\r\n"):
            raise ValueError("MAIL_POOL_SENDER_NAME invalid")
        if not isinstance(namespace, str) or not re.fullmatch(r"[A-Za-z0-9:_-]{1,80}", namespace):
            raise ValueError("MAIL_POOL_NAMESPACE invalid")
        self.port = _integer(config, "MAIL_POOL_PORT", 465, 1, 65535)
        self.timeout = _integer(config, "MAIL_POOL_TIMEOUT", 5, 1, 15)
        self.attempts = _integer(config, "MAIL_POOL_MAX_ATTEMPTS", 3, 1, 5)
        self.interval = _integer(config, "MAIL_POOL_MIN_INTERVAL", 10, 1, 3600)
        self.global_interval = _integer(config, "MAIL_POOL_GLOBAL_INTERVAL", 3, 0, 60)
        self.minute_limit = _integer(config, "MAIL_POOL_PER_MINUTE", 5, 1, 1000)
        self.day_limit = _integer(config, "MAIL_POOL_PER_DAY", 200, 1, 100000)
        self.cooldown = _integer(config, "MAIL_POOL_FAILURE_COOLDOWN", 300, 1, 86400)
        # Hash tag keeps the reservation keys in one Redis Cluster slot, too.
        self.prefix = "{" + namespace + "}:"
        if redis_client is None:
            try:
                redis_client = Redis.from_url(config["REDIS_URL"], socket_connect_timeout=2, socket_timeout=2)
            except Exception:
                raise ValueError("MAIL_POOL requires a valid REDIS_URL") from None
        self.redis = redis_client

    def keys(self, account):
        base = self.prefix + account.key + ":"
        return [base + suffix for suffix in ("gap", "minute", "day", "cooldown", "busy")]

    def reserve(self, excluded, count, token):
        keys = [self.prefix + "cursor", self.prefix + "global"]
        for account in self.accounts:
            keys.extend(self.keys(account))
        # Upper bound for sequential RCPT commands plus connection/DATA overhead.
        lease = (count + 15) * self.timeout + 30
        args = [len(self.accounts), count, self.minute_limit, self.day_limit,
                self.interval, token, lease, self.global_interval]
        args.extend(int(a.key not in excluded) for a in self.accounts)
        try:
            index = int(self.redis.eval(_RESERVE, len(keys), *keys, *args))
        except Exception:
            raise MailPoolError("MAIL_POOL_COORDINATION_UNAVAILABLE") from None
        if not index:
            raise MailPoolError("MAIL_POOL_BUSY_OR_LIMITED")
        return self.accounts[index - 1]

    def cool(self, account):
        try:
            self.redis.set(self.keys(account)[3], "1", ex=self.cooldown)
        except Exception:
            raise MailPoolError("MAIL_POOL_COORDINATION_UNAVAILABLE") from None

    def release(self, account, token):
        try:
            self.redis.eval(_RELEASE, 1, self.keys(account)[4], token)
        except Exception:
            # A mail accepted by SMTP must stay successful even if cleanup fails.
            # The reservation expires; never clear another worker's ownership.
            current_app.logger.warning("mail_pool reservation release failed")

    def send(self, message):
        if not message.send_to:
            raise ValueError("Message requires recipients")
        if message.has_bad_headers():
            raise BadHeaderError
        recipients = list(sanitize_addresses(message.send_to))
        # Keep synchronous sends bounded. Current callers send one recipient.
        if len(recipients) > 100:
            raise MailPoolError("MAIL_POOL_TOO_MANY_RECIPIENTS")
        token = uuid.uuid4().hex
        try:
            self._send(message, recipients, token)
        finally:
            if self.global_interval:
                try:
                    self.redis.eval(_FINISH_GLOBAL, 1, self.prefix + "global",
                                    token, self.global_interval)
                except Exception:
                    current_app.logger.warning("mail_pool global reservation release failed")

    def _send(self, message, recipients, token):
        excluded = set()
        for _ in range(min(self.attempts, len(self.accounts))):
            account = self.reserve(excluded, len(recipients), token)
            excluded.add(account.key)
            host = None
            try:
                sent = copy.copy(message)
                sent.sender = (self.name, account.username) if self.name else account.username
                sent.date = sent.date or time.time()
                payload = sent.as_bytes()
                try:
                    host = smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout,
                                           context=ssl.create_default_context())
                    # Never enable SMTP debug output: AUTH contains credentials.
                    host.login(account.username, account.password)
                except (OSError, smtplib.SMTPException):
                    self.cool(account)
                    current_app.logger.warning("mail_pool connection/auth failed account=%s", account.key)
                    continue
                try:
                    refused = host.sendmail(account.username, recipients, payload,
                                            message.mail_options, message.rcpt_options)
                except smtplib.SMTPRecipientsRefused:
                    raise MailPoolError("MAIL_POOL_RECIPIENT_REJECTED") from None
                except smtplib.SMTPResponseException:
                    self.cool(account)
                    raise MailPoolError("MAIL_POOL_MESSAGE_REJECTED") from None
                except (OSError, smtplib.SMTPException):
                    self.cool(account)
                    # The server may have accepted DATA before losing the reply.
                    # Do not attempt another sender when delivery is uncertain.
                    raise MailPoolError("MAIL_POOL_DELIVERY_UNCERTAIN") from None
                if refused:
                    raise MailPoolError("MAIL_POOL_PARTIAL_DELIVERY")
                email_dispatched.send(current_app._get_current_object(), message=sent)
                return
            finally:
                if host is not None:
                    # close() avoids a failing QUIT undoing a successful send.
                    try:
                        host.close()
                    except Exception:
                        pass
                self.release(account, token)
        raise MailPoolError("MAIL_POOL_CONNECTION_FAILED")


class PoolConnection:
    def __init__(self, app):
        self.app = app

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def send(self, message, envelope_from=None):
        # The pool always uses the selected authenticated mailbox as envelope From.
        # An explicit legacy envelope_from must not impersonate a different sender.
        if self.app.extensions["mail"].suppress:
            if not message.send_to:
                raise ValueError("Message requires recipients")
            if message.has_bad_headers():
                raise BadHeaderError
            sent = copy.copy(message)
            pool = self.app.extensions["mail_pool"]
            sent.sender = (pool.name, pool.accounts[0].username)
            sent.date = sent.date or time.time()
            email_dispatched.send(self.app, message=sent)
            return
        self.app.extensions["mail_pool"].send(message)

    def send_message(self, *args, **kwargs):
        self.send(Message(*args, **kwargs))


class PoolMail(Mail):
    """MAIL_POOL_ENABLED=false leaves Flask-Mail's single-account behavior intact."""

    def __init__(self, app=None, *, redis_client=None):
        self.pool_redis = redis_client
        super().__init__(app)

    def init_app(self, app):
        pool = Pool(app, self.pool_redis) if _enabled(app.config.get("MAIL_POOL_ENABLED")) else None
        state = super().init_app(app)
        app.extensions["mail_pool"] = pool
        if pool:
            state.default_sender = (pool.name, pool.accounts[0].username)
        return state

    def connect(self):
        app = self.app or current_app._get_current_object()
        if app.extensions.get("mail_pool"):
            return PoolConnection(app)
        return super().connect()

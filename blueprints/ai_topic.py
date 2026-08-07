"""AI 每日话题：每天 1 篇精挑话题 + 开放讨论问题，系统账号"BME 资讯君"发布到社区广场 feed。

链路：feedparser 采集昨日 RSS → LLM 按摘要选 1（去重）→ fetch 原文 + LLM 深加工
→ ORM 直写 ArticleV2Model → 通知中心推送 → APScheduler 定时。
质量形态：1 篇/日（素材不足出 0 篇），目标是激发真人讨论而非堆量。
调度范式照搬 blueprints/attendance_report.py（fcntl 锁单例 + app_context）。
"""
import os
import json
import time as _t
import atexit
import hashlib
import secrets
from datetime import datetime, date, timedelta

import feedparser
import requests
import trafilatura

from flask import Blueprint, request, jsonify, current_app
from flask_jwt_extended import jwt_required, get_jwt_identity

from exts import db
from models import UserModel, ArticleV2Model, AiTopicLedger
from litellm_chat import chat_completion
from .notification import batch_create_notifications

try:
    import fcntl  # Unix 专属；Windows 下为 None（开发环境单进程退化为无跨进程锁）
except ImportError:
    fcntl = None

bp = Blueprint("ai_topic", __name__, url_prefix="/ai_topic")


# ==================== 系统账号 ====================

def ensure_ai_topic_account(app):
    """幂等确保系统账号"BME 资讯君"存在（仿 attendance_report.ensure_recipient_permission）。"""
    with app.app_context():
        email = app.config.get("AI_TOPIC_AUTHOR_EMAIL", "ai-topic@bme.sysu.edu.cn")
        u = UserModel.query.filter_by(email=email).first()
        if u:
            return u
        u = UserModel(
            username="BME资讯君", email=email,
            user_mode="admin", role="super_admin",
            introduction="BME 平台 AI 资讯助手，每日整理 AI/教育科技话题。",
        )
        u.set_password(secrets.token_urlsafe(24))   # 随机串，系统号不登录
        db.session.add(u)
        db.session.commit()
        app.logger.info(f"[ai_topic] 已建系统账号 BME资讯君 id={u.id}")
        return u


def ensure_ai_topic_schema(app):
    """幂等建 AiTopicLedger 表。生产 pull 代码后首次启动自动建表（checkfirst 防重复）。"""
    with app.app_context():
        AiTopicLedger.__table__.create(db.engine, checkfirst=True)


def _get_author():
    return UserModel.query.filter_by(
        email=current_app.config.get("AI_TOPIC_AUTHOR_EMAIL", "ai-topic@bme.sysu.edu.cn")
    ).first()


# ==================== Phase 1 · 采集 ====================

def _yesterday_window():
    """昨日本地时间 [00:00, 24:00)。服务器 tz=Asia/Shanghai 时即北京时间昨日。"""
    today = date.today()
    return (datetime.combine(today - timedelta(days=1), datetime.min.time()),
            datetime.combine(today, datetime.min.time()))


def _entry_dt(entry):
    """feedparser entry → datetime(本地)；无时间字段返回 None。"""
    st = entry.get("published_parsed") or entry.get("updated_parsed")
    if not st:
        return None
    try:
        return datetime.fromtimestamp(_t.mktime(st))
    except Exception:
        return None


def _collect_candidates():
    """抓 AI_TOPIC_FEED_URLS，过滤昨日条目。返回 [{title,summary,link,source,published}]。"""
    start, end = _yesterday_window()
    # 容错窗：±12h，防 RSS 时区/发布延迟导致昨日内容漏抓
    lo, hi = start - timedelta(hours=12), end + timedelta(hours=12)
    candidates = []
    headers = {"User-Agent": "BME-AI-Topic/1.0"}
    for url in current_app.config.get("AI_TOPIC_FEED_URLS", []):
        try:
            feed = feedparser.parse(url, request_headers=headers) or {}
        except Exception as e:
            current_app.logger.warning(f"[ai_topic] 抓取失败 {url}: {e}")
            continue
        source = ((feed.feed.get("title") if feed.feed else None) or url)[:60]
        for e in feed.entries:
            published = _entry_dt(e)
            if published is None or not (lo <= published < hi):
                continue
            link = e.get("link")
            if not link:
                continue
            candidates.append({
                "title": (e.get("title") or "").strip()[:200],
                "summary": (e.get("summary") or e.get("description") or "").strip()[:400],
                "link": link,
                "source": source,
                "published": published.isoformat(),
            })
    return candidates


# ==================== Phase 2 · 选题（LLM 摘要选） ====================

_PICK_PROMPT = (
    "你是 BME 教育/AI 社区的「每日话题」编辑。从下列昨日资讯候选里挑 1 个最适合本社区的话题。\n"
    "**领域限定**：只选属于以下三个方向之一——\n"
    "1) AI 技术进展(模型/工具/开源/能力突破)\n"
    "2) AI 与教育(教学/学习/评价/学术的应用或争议)\n"
    "3) AI 金融市场(AI 相关的股市/投资/产业资本/商业化)\n"
    "排除与 AI/教育/金融无关的纯自然科学、自然趣闻、社会八卦。\n"
    "评判：与本社区受众(教育+AI 从业者/学习者)的相关度 > 讨论价值 > 时效。\n"
    "若没有任何候选落在上述三方向内，pick 设为 null(本次不出文)。\n"
    "只输出严格 JSON，无多余解释："
    "{\"pick\":{\"title\":\"...\",\"link\":\"...\",\"reason\":\"属于哪个方向+为何值得聊(<=60字)\"},"
    "\"alternatives\":[{\"title\":\"...\",\"link\":\"...\"}]}\n"
    "候选(JSON数组)：{candidates}"
)


def _call_llm_json(messages, temperature):
    """调 LLM 并解析 JSON，解析失败重试 1 次，仍失败抛出（调用方兜底跳过）。"""
    last = None
    for _ in range(2):
        content = chat_completion(messages, temperature=temperature, response_json=True)
        try:
            return json.loads(content)
        except (json.JSONDecodeError, ValueError) as e:
            last = e
    raise last


def _pick_topic(candidates):
    """LLM 从候选挑 1 + 备选。返回 (pick, alternatives)。"""
    if not candidates:
        return None, []
    slim = [{"title": c["title"], "summary": c["summary"][:120], "link": c["link"]} for c in candidates]
    data = _call_llm_json(
        [{"role": "user", "content": _PICK_PROMPT.replace(
            "{candidates}", json.dumps(slim, ensure_ascii=False))}],
        temperature=0.3,
    )
    return data.get("pick"), (data.get("alternatives") or [])


# ==================== Phase 3 · fetch 原文 + 成文（LLM 原文写） ====================

def _fetch_content(link):
    """requests + trafilatura 提正文（<=4000 字）；失败返回 ''。"""
    try:
        r = requests.get(link, timeout=15, headers={"User-Agent": "BME-AI-Topic/1.0"})
        r.raise_for_status()
        text = trafilatura.extract(r.text or "", include_comments=False, favor_recall=True)
        return (text or "")[:4000]
    except Exception as e:
        current_app.logger.warning(f"[ai_topic] fetch 原文失败 {link}: {e}")
        return ""


_COMPOSE_PROMPT = (
    "你是「BME 资讯君」，为教育/AI 社区写一篇「每日话题」。基于下列原文写一篇能引发讨论的话题文章。\n"
    "要求：\n"
    "1. 结构：背景简介(2-3句) → 要点速览(3-5条,每条一句话) → 深度点评(你的独立观点,能激发思考) → 今日讨论(1个开放问题)\n"
    "2. Markdown 格式；专业克制；禁止 emoji；只点评+引用,不搬运原文整段\n"
    "3. introduction 必须含「由 AI 整理」字样,80-120 字\n"
    "4. 只输出严格 JSON：{\"title\":\"...\",\"introduction\":\"...\",\"content_md\":\"...(Markdown)\",\"discussion_question\":\"...\"}\n"
    "原文标题：{title}\n原文链接：{link}\n原文来源：{source}\n原文正文：{content}"
)


def _compose(topic):
    """topic={title,link,source,summary} → 成文 dict {title,introduction,content_md,discussion_question}。"""
    content = _fetch_content(topic.get("link")) or topic.get("summary") or "（原文未能获取，基于标题与摘要写作）"
    return _call_llm_json(
        [{"role": "user", "content": (_COMPOSE_PROMPT
            .replace("{title}", topic.get("title", ""))
            .replace("{link}", topic.get("link", ""))
            .replace("{source}", topic.get("source", ""))
            .replace("{content}", content))}],
        temperature=0.7,
    )


# ==================== Phase 4 · 发布 + 记账本 ====================

def _url_hash(url):
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _already_picked(url):
    return AiTopicLedger.query.filter_by(url_hash=_url_hash(url)).first() is not None


def _today_picked():
    """今日是否已成功出文（published/draft）。幂等防重发。"""
    return AiTopicLedger.query.filter(
        AiTopicLedger.picked_date == date.today(),
        AiTopicLedger.status.in_(["published", "draft"]),
    ).first()


def _publish(article_data, topic, reason=""):
    author = _get_author()
    if not author:
        raise RuntimeError("系统账号 BME资讯君 未建（ensure_ai_topic_account 未执行？）")
    mode = current_app.config.get("AI_TOPIC_PUBLISH_MODE", "draft")
    question = article_data.get("discussion_question", "")
    content_md = (article_data.get("content_md") or "")
    if question:
        content_md += f"\n\n---\n\n> **今日讨论**：{question}"
    art = ArticleV2Model(
        title=(article_data.get("title") or topic.get("title") or "今日话题")[:100],
        introduction=(article_data.get("introduction") or "")[:300],
        content_md=content_md,
        status=ArticleV2Model.STATUS_PUBLISHED if mode == "published" else ArticleV2Model.STATUS_DRAFT,
        author_id=author.id,
        publish_time=datetime.now() if mode == "published" else None,
    )
    db.session.add(art)
    db.session.flush()
    db.session.add(AiTopicLedger(
        source_url=topic["link"],
        url_hash=_url_hash(topic["link"]),
        title=(topic.get("title") or "")[:200],
        picked_date=date.today(),
        article_v2_id=art.id,
        status=mode,
        reason=(reason or "")[:500],
    ))
    db.session.commit()
    return art


# ==================== Phase 5 · 通知 ====================

def _notify(article):
    uids = [row[0] for row in db.session.query(UserModel.id).all()]
    if not uids:
        return
    batch_create_notifications(
        uids,
        title="今日 AI 话题已更新",
        content=(article.introduction or article.title or "BME 资讯君更新了今日话题")[:200],
        category="system",
        source_type="admin",
        source_id=article.id,
    )


# ==================== job 主流程 ====================

def _run_daily_topic(app):
    """采集→选题→去重→成文→发布→通知。任何失败都不发烂文（由 _scheduled_job 兜底记日志）。"""
    if _today_picked():
        app.logger.info("[ai_topic] 今日已生成话题，跳过")
        return
    candidates = _collect_candidates()
    app.logger.info(f"[ai_topic] 昨日候选 {len(candidates)} 条")
    if not candidates:
        app.logger.info("[ai_topic] 昨日无候选，今日跳过")
        return

    pick, alts = _pick_topic(candidates)
    # 去重：主选已选过则走备选；找到第一个未选过的可用候选
    chosen, reason = None, ""
    for t in [pick] + (alts or []):
        if not t or not t.get("link") or _already_picked(t["link"]):
            continue
        full = next((c for c in candidates if c["link"] == t["link"]), None) or t
        chosen, reason = full, (t.get("reason") or "")
        break
    if not chosen:
        app.logger.info("[ai_topic] 候选均已选过或无可用，今日跳过")
        return

    article_data = _compose(chosen)
    art = _publish(article_data, chosen, reason=reason)
    # 仅 published 模式推送通知；draft（测试期 admin 审）不打扰全体用户
    if current_app.config.get("AI_TOPIC_PUBLISH_MODE", "draft") == "published":
        _notify(art)
    app.logger.info(
        f"[ai_topic] 已发布话题 article_v2_id={art.id} status={art.status} link={chosen['link']}"
    )


# ==================== Phase 6 · 调度（照搬 attendance_report） ====================

_scheduler = None
_lock_fd = None
_LOCK_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "log", ".ai_daily_topic.lock"
)


def _try_acquire_lock():
    global _lock_fd
    os.makedirs(os.path.dirname(_LOCK_PATH), exist_ok=True)
    fd = os.open(_LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o644)
    if fcntl is None:
        _lock_fd = fd
        atexit.register(_release_lock)
        return fd
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    _lock_fd = fd
    atexit.register(_release_lock)
    return fd


def _release_lock():
    global _lock_fd
    if _lock_fd is not None:
        try:
            if fcntl is not None:
                fcntl.flock(_lock_fd, fcntl.LOCK_UN)
            os.close(_lock_fd)
        finally:
            _lock_fd = None


def _scheduled_job(app):
    with app.app_context():
        try:
            _run_daily_topic(app)
        except Exception as e:
            app.logger.exception(f"[ai_topic] 任务失败: {e}")


def init_ai_topic_scheduler(app):
    """初始化每日话题定时任务。fcntl 锁保证多 worker 只启一个；AI_DAILY_TOPIC_ENABLED 总开关。"""
    global _scheduler
    if _scheduler is not None:
        return
    if not app.config.get("AI_DAILY_TOPIC_ENABLED", False):
        app.logger.info("[ai_topic] 已通过 AI_DAILY_TOPIC_ENABLED 关闭")
        return
    if _try_acquire_lock() is None:
        app.logger.info("[ai_topic] 另一进程已持有锁，本进程不启动")
        return

    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    tz = app.config.get("ATTENDANCE_REPORT_TIMEZONE", "Asia/Shanghai")
    hour = app.config.get("AI_TOPIC_CRON_HOUR", 8)
    minute = app.config.get("AI_TOPIC_CRON_MINUTE", 30)
    sched = BackgroundScheduler(timezone=tz)
    sched.add_job(
        _scheduled_job,
        trigger=CronTrigger(hour=hour, minute=minute, timezone=tz),
        args=[app],
        id="ai_daily_topic",
        coalesce=True,
        max_instances=1,
        misfire_grace_time=3600,
        replace_existing=True,
    )
    sched.start()
    _scheduler = sched
    app.logger.info(f"[ai_topic] 已启动，每天 {hour}:{minute:02d} ({tz}) 生成今日话题")


# ==================== 调试路由（手动触发，开发/验证用） ====================

@bp.route("/run", methods=["POST"])
@jwt_required()
def manual_run():
    """手动触发一次当日话题生成（仅 admin）。幂等：今日已出则跳过。"""
    user = UserModel.query.filter_by(email=get_jwt_identity()).first()
    if not user or user.user_mode != "admin":
        return jsonify({"code": 403, "message": "仅管理员可触发"}), 403
    try:
        _run_daily_topic(current_app)
        return jsonify({"code": 200, "message": "已触发（结果详见后端日志）"}), 200
    except Exception as e:
        return jsonify({"code": 500, "message": f"触发失败: {e}"}), 500

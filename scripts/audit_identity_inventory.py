"""D0 身份基线盘点（只读）：成员身份确认与重复账号安全迁移 项目的第一项交付。

对运行库做只读扫描，产出身份/账号/引用面基线报告，为 D1 安全地基与 D2 人员层
（Person 回填、空壳判定覆盖面）提供事实输入。全程只 SELECT / SCAN，不写任何表。

八节内容：
  1 账号分布（role x status）          5 审计敏感行计数（token 模式，只计数不打印值）
  2 super_admin 清单（邮箱默认脱敏）    6 引用 user.id 的表清单（FK + 疑似裸引用 + 行数）
  3 疑似系统号                          7 非 JWT 身份来源清单（代码扫描 + 固定事实）
  4 归一化碰撞/重名/学号重复（弱线索）  8 Redis 认证相关 key 前缀计数

用法（项目根）：
  .venv/bin/python scripts/audit_identity_inventory.py                 # 报告打印 + 落盘 ../docs/记录/
  .venv/bin/python scripts/audit_identity_inventory.py --full-emails  # 本地可信环境显示完整邮箱
  .venv/bin/python scripts/audit_identity_inventory.py --out 路径.md   # 指定报告输出路径
"""
import argparse
import os
import re
import sys
from collections import Counter
from datetime import datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import app  # noqa: E402,F401  (import 即装配 config)
from exts import db, redis_client  # noqa: E402
from sqlalchemy import text  # noqa: E402

import config  # noqa: E402

REPORT_TITLE = "身份基线盘点（D0）"

# 审计 operation_data 中疑似令牌的只读识别模式（只计数，绝不回显命中内容）。
# 注意：audit 落库存的是 Python dict repr（单引号），不是 JSON——两种引号都要匹配。
_TOKEN_PATTERNS = (
    re.compile(r'["\']token["\']\s*:'),
    re.compile(r'["\']refresh_token["\']\s*:'),
    re.compile(r'Bearer\s+[A-Za-z0-9._-]{20,}'),
    re.compile(r'eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.'),  # JWT 三段式形状
)

# 疑似系统号的 username 关键词（弱信号，人工确认）
_SERVICE_NAME_HINTS = ("资讯君", "机器人", "系统", "官方", "公告")

# 裸用户引用的启发式列名（D2 空壳扫描需逐表判定；FK 已单列不再重复）
_BARE_REF_COLUMN = re.compile(
    r'^(user_id|actor_id|operator_id|reviewer_id|approver_id|granted_by|'
    r'appointed_by|ended_by|issued_by|created_by|reviewed_by|team_mentor_id|'
    r'owner_user_id|bound_user_id|.*_user_id)$'
)


def _mask_email(email):
    if not email or "@" not in email:
        return email or "-"
    local, domain = email.split("@", 1)
    head = local[:1] if len(local) <= 3 else local[:2]
    return f"{head}***@{domain}"


def _q(sql, **params):
    return db.session.execute(text(sql), params).fetchall()


class Inventory:
    def __init__(self, full_emails=False):
        self.full = full_emails
        self.lines = []

    def say(self, line=""):
        self.lines.append(line)
        print(line)

    def h(self, title):
        self.say(f"\n## {title}\n")

    def email(self, e):
        return e if self.full else _mask_email(e)

    # ── 1 账号分布 ──────────────────────────────────────────────
    def section_accounts(self):
        self.h("1. 账号分布")
        rows = _q("SELECT role, status, COUNT(*) AS n FROM `user` GROUP BY role, status ORDER BY n DESC")
        total = 0
        for r in rows:
            self.say(f"- role={r.role} / status={r.status}: {r.n}")
            total += r.n
        self.say(f"- 合计: {total}")
        no_hash = _q("SELECT COUNT(*) AS n FROM `user` WHERE password IS NULL OR password NOT LIKE 'pbkdf2:%'")
        self.say(f"- 密码非 pbkdf2 哈希（明文 MD5 遗留，登录时自动升级）: {no_hash[0].n}")

    # ── 2 super_admin 清单 ─────────────────────────────────────
    def section_super_admins(self):
        self.h("2. super_admin 清单（含最后登录审计时间）")
        rows = _q(
            "SELECT u.id, u.email, u.username, u.admin_tag, u.join_time FROM `user` u "
            "WHERE u.role='super_admin' ORDER BY u.id"
        )
        self.say("| id | 邮箱 | username | admin_tag | 最后登录审计 |")
        self.say("|---|---|---|---|---|")
        for r in rows:
            last = _q(
                "SELECT MAX(timestamp) AS last_login FROM audit_log WHERE user_id=:uid "
                "AND operation IN ('用户登录','管理员登录')",
                uid=r.id,
            )[0].last_login
            last_s = last.strftime("%Y-%m-%d %H:%M") if last else "从未"
            self.say(f"| {r.id} | {self.email(r.email)} | {r.username or '-'} | {r.admin_tag or '-'} | {last_s} |")
        self.say("\n注：D1 上线后此节应追加 MFA 绑定状态列（auth_factor）。")

    # ── 3 疑似系统号 ────────────────────────────────────────────
    def section_service_accounts(self):
        self.h("3. 疑似系统号（人工确认后 D2 标注 account_kind=service）")
        ai_email = os.getenv("AI_TOPIC_AUTHOR_EMAIL", "ai-topic@bme.sysu.edu.cn")
        rows = _q("SELECT id, email, username, role FROM `user` WHERE email=:em", em=ai_email)
        for r in rows:
            self.say(f"- [AI 资讯作者号] id={r.id} {self.email(r.email)} username={r.username} role={r.role}（随机密码，不登录）")
        rows = _q(
            "SELECT u.id, u.email, u.username, u.role FROM `user` u WHERE u.role='super_admin' AND NOT EXISTS ("
            " SELECT 1 FROM audit_log a WHERE a.user_id=u.id AND a.operation IN ('用户登录','管理员登录'))"
        )
        for r in rows:
            self.say(f"- [super_admin 且无任何登录审计] id={r.id} {self.email(r.email)} username={r.username or '-'}")
        rows = _q("SELECT id, email, username FROM `user` WHERE " +
                  " OR ".join([f"username LIKE :kw{i}" for i in range(len(_SERVICE_NAME_HINTS))]),
                  **{f"kw{i}": f"%{kw}%" for i, kw in enumerate(_SERVICE_NAME_HINTS)})
        for r in rows:
            self.say(f"- [username 命中关键词 {_SERVICE_NAME_HINTS}] id={r.id} {self.email(r.email)} username={r.username}")
        dev_rows = _q("SELECT COUNT(*) AS n FROM `user` WHERE email LIKE '%@seed.dev'")
        self.say(f"- @seed.dev 开发测试账号: {dev_rows[0].n} 个（dev 库专用，生产应=0）")

    # ── 4 归一化碰撞 / 重名 / 学号重复 ──────────────────────────
    def section_duplicates(self):
        self.h("4. 归一化碰撞与重名（弱线索：同名不等于同人，S01）")
        rows = _q("SELECT LOWER(TRIM(email)) AS norm, COUNT(*) AS n FROM `user` "
                  "GROUP BY norm HAVING n > 1")
        if rows:
            self.say(f"- 邮箱 trim+lower 碰撞组: {len(rows)}（当前唯一键按字面比较，存在隐患）")
            for r in rows[:20]:
                self.say(f"  - {self.email(r.norm)} x{r.n}")
        else:
            self.say("- 邮箱 trim+lower 碰撞组: 0")
        rows = _q("SELECT username, COUNT(*) AS n, GROUP_CONCAT(id ORDER BY id) AS ids FROM `user` "
                  "WHERE username IS NOT NULL AND username<>'' GROUP BY username HAVING n > 1 "
                  "ORDER BY n DESC LIMIT 30")
        self.say(f"- username 重名组（前 30）: {len(rows)}")
        for r in rows:
            self.say(f"  - {r.username} x{r.n} ids={r.ids}")
        rows = _q("SELECT student_id, COUNT(*) AS n, GROUP_CONCAT(id ORDER BY id) AS ids FROM `user` "
                  "WHERE student_id IS NOT NULL GROUP BY student_id HAVING n > 1 ORDER BY n DESC LIMIT 30")
        self.say(f"- student_id 重复组（自填整数，前 30）: {len(rows)}")
        for r in rows:
            self.say(f"  - {r.student_id} x{r.n} ids={r.ids}")

    # ── 5 审计敏感行计数 ────────────────────────────────────────
    def section_audit_secrets(self):
        self.h("5. 审计敏感行只读计数（响应含令牌的历史存量）")
        rows = _q("SELECT id, operation, timestamp, operation_data FROM audit_log "
                  "WHERE operation_data LIKE '%token%' ORDER BY id DESC LIMIT 20000")
        hit_ops, newest, hit_n = Counter(), None, 0
        for r in rows:
            data = r.operation_data or ""
            if any(p.search(data) for p in _TOKEN_PATTERNS):
                hit_n += 1
                hit_ops[r.operation] += 1
                if newest is None:
                    newest = r.timestamp
        total = _q("SELECT COUNT(*) AS n FROM audit_log")[0].n
        self.say(f"- audit_log 总行数: {total}；operation_data 命中令牌模式: {hit_n}（采样最近 2 万行）")
        for op, n in hit_ops.most_common():
            self.say(f"  - {op}: {n}")
        if newest:
            self.say(f"- 最新一条命中时间: {newest.strftime('%Y-%m-%d %H:%M')}；"
                     "access(2h)/refresh(14d) 均已自然过期则无活性泄露，处置结论见报告尾节")

    # ── 6 引用 user.id 的表清单 ─────────────────────────────────
    def section_user_references(self):
        self.h("6. 引用 user.id 的表清单（D2 空壳扫描覆盖面）")
        fk_rows = _q(
            "SELECT TABLE_NAME AS tbl, COLUMN_NAME AS col FROM information_schema.KEY_COLUMN_USAGE "
            "WHERE TABLE_SCHEMA=:db AND REFERENCED_TABLE_NAME='user' ORDER BY tbl, col", db=config.DATABASE)
        fk_cols = {(r.tbl, r.col) for r in fk_rows}
        self.say(f"- 外键引用（{len({tc[0] for tc in fk_cols})} 表 {len(fk_rows)} 列）:")
        for tbl in sorted({tc[0] for tc in fk_cols}):
            cols = ", ".join(sorted(c for (tt, c) in fk_cols if tt == tbl))
            self.say(f"  - {tbl} ({cols})")
        col_rows = _q(
            "SELECT TABLE_NAME AS tbl, COLUMN_NAME AS col, DATA_TYPE AS dtype FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=:db AND DATA_TYPE IN ('int','bigint') AND COLUMN_NAME REGEXP '_id$|_by$'",
            db=config.DATABASE)
        bare = [(r.tbl, r.col, r.dtype) for r in col_rows
                if (r.tbl, r.col) not in fk_cols and r.tbl != 'user' and _BARE_REF_COLUMN.match(r.col)]
        self.say(f"- 疑似裸用户引用（无外键，需 D2 逐表判定，{len(bare)} 处）:")
        for tbl, col, dtype in sorted(bare):
            self.say(f"  - {tbl}.{col} ({dtype})")
        table_names = sorted({tc[0] for tc in fk_cols} | {b[0] for b in bare})
        self.say("- 行数:")
        for tbl in table_names:
            try:
                n = _q(f"SELECT COUNT(*) AS cnt FROM `{tbl}`")[0].cnt
                self.say(f"  - {tbl}: {n}")
            except Exception:
                self.say(f"  - {t}: 计数失败（权限或已删表）")

    # ── 7 非 JWT 身份来源 ───────────────────────────────────────
    def section_non_jwt_identities(self, repo_root):
        self.h("7. 非 JWT 身份来源清单（D0 目标：没有未知认证通道）")
        jwt_count, media_calls = self._scan_code(repo_root)
        self.say(f"- @jwt_required 装饰端点（blueprints/services 代码扫描）: {jwt_count} 处")
        self.say(f"- media_signed_url 短签签发调用: {media_calls} 处"
                 "（camp_delivery/camp_material/camp_meeting/resource_center/feedback_tickets）")
        n = _q("SELECT COUNT(*) AS n FROM llm_user_key WHERE is_active=1")[0].n
        self.say(f"- LiteLLM virtual key（llm_user_key 活跃）: {n} 条——D1 后须随 owner 生命周期撤回")
        self.say("- 课程资料 Down_Code 一次性码：blueprints/resource_center.py 注释口径（与短签双轨并存），"
                 "一次性消费、无账号绑定语义，不属会话通道")
        self.say("- APScheduler 后台任务身份：attendance_report / camp_scheduler / work_scheduler / ai_topic"
                 "（均系统身份写库，D5 需接业务资格检查）")
        self.say("- /auth/* 端点分类预填（现网 url_map 实测，D1 request_guard 将按此分类）：")
        self.say("  | 路径 | 方法 |")
        self.say("  |---|---|")
        public, refresh_like, other = [], [], []
        for rule in app.url_map.iter_rules():
            if not rule.rule.startswith('/auth/'):
                continue
            methods = sorted(m for m in rule.methods if m not in ('HEAD', 'OPTIONS'))
            entry = (rule.rule, ",".join(methods))
            if rule.rule in ('/auth/refresh', '/auth/logout'):
                refresh_like.append(entry)
            elif rule.rule in ('/auth/login', '/auth/admin_login', '/auth/register',
                               '/auth/captcha/email', '/auth/find_password', '/auth/dev_accounts'):
                public.append(entry)
            else:
                other.append(entry)
        for label, group in (("公开", public), ("续期/退出", refresh_like), ("受限（其余 /auth/*）", other)):
            for rule, methods in group:
                self.say(f"  | {rule} | {methods} ({label}) |")

    @staticmethod
    def _scan_code(repo_root):
        jwt_n, media_n = 0, 0
        for sub in ('blueprints', 'services'):
            base = os.path.join(repo_root, sub)
            for dirpath, _dirs, files in os.walk(base):
                for fn in files:
                    if not fn.endswith('.py'):
                        continue
                    try:
                        with open(os.path.join(dirpath, fn), encoding='utf-8') as f:
                            src = f.read()
                    except OSError:
                        continue
                    jwt_n += src.count('@jwt_required')
                    media_n += len(re.findall(r'media_signed_url\(', src))
        return jwt_n, media_n

    # ── 8 Redis 认证 key 计数 ───────────────────────────────────
    def section_redis(self):
        self.h("8. Redis 认证相关 key 前缀计数")
        try:
            for prefix in ('captcha:*', 'jwt:blocklist:*'):
                n = sum(1 for _ in redis_client.scan_iter(match=prefix, count=500))
                self.say(f"- {prefix}: {n}")
        except Exception as e:
            self.say(f"- Redis 扫描失败（不影响其余各节）: {type(e).__name__}")

    # ── 9 身份运行时观测（D5 收尾 P0-2）─────────────────────────
    def section_identity_runtime(self):
        self.h("9. 身份运行时观测（人员规则 shadow / outbox / 锚点 / 人员态）")

        def _try(label, fn):
            try:
                fn()
            except Exception as e:
                self.say(f"- {label} 扫描失败（不影响其余小节）: {type(e).__name__}: {e}")

        def shadow_stats():
            rows = _q(
                "SELECT event_id, evidence_refs, created_at FROM identity_event "
                "WHERE action='identity.enforcement.shadow' ORDER BY event_id DESC LIMIT 5000")
            from collections import Counter
            by_code, by_scope, newest, hit = Counter(), Counter(), None, 0
            for r in rows:
                hit += 1
                if newest is None:
                    newest = r.created_at
                note = (r.evidence_refs or '') if isinstance(r.evidence_refs, str) else str(r.evidence_refs or '')
                # note 形如 {'note': 'camp_join#12: nonprimary;duplicate_person'}
                for part in note.split(';'):
                    code = part.strip().split(':')[-1].strip().strip("'}\"")
                    if code and code[0].isalpha():
                        by_code[code] += 1
                if '#' in note:
                    by_scope[note.split('#')[0].split(':')[-1].strip().strip("'{\"")] += 1
            self.say(f"- enforcement.shadow 事件（采样最近 5000）: {hit} 条"
                     + (f"，最新 {newest:%Y-%m-%d %H:%M}" if newest else ""))
            for code, n in by_code.most_common():
                self.say(f"  - 违规类型 {code}: {n}")
            for scope, n in by_scope.most_common():
                self.say(f"  - 业务 {scope}: {n}")
            if not hit:
                self.say("  - 无违规记录（enforce 决策前需至少一个完整营期周期的观测）")

        def outbox_stats():
            rows = _q(
                "SELECT delivery_state, COUNT(*) AS n, MIN(created_at) AS oldest, "
                "MAX(attempts) AS max_attempts FROM identity_outbox "
                "GROUP BY delivery_state")
            if not rows:
                self.say("- outbox: 空（无待投递与历史）")
            for r in rows:
                age = f"，最早 {r.oldest:%Y-%m-%d %H:%M}" if r.oldest else ''
                self.say(f"- outbox {r.delivery_state}: {r.n} 条{age}"
                         f"（最大重试 {r.max_attempts}）")
            stuck = _q("SELECT COUNT(*) AS n FROM identity_outbox "
                       "WHERE delivery_state='pending' AND attempts>0")
            if stuck[0].n:
                self.say(f"  - 待关注: {stuck[0].n} 条重试中（消费者日志/依赖排查）")

        def anchor_stats():
            n_camp = _q("SELECT COUNT(*) AS n FROM camp_person_participation")[0].n
            n_unit = _q("SELECT COUNT(*) AS n FROM unit_person_participation")[0].n
            self.say(f"- 参与锚点: camp={n_camp} / unit={n_unit}")
            drift = _q(
                "SELECT COUNT(*) AS n FROM camp_member cm "
                "LEFT JOIN camp_person_participation cpp "
                "  ON cpp.camp_member_id = cm.id "
                "WHERE cm.status='active' AND (cpp.camp_member_id IS NULL OR cpp.state != 'active')")
            if drift[0].n:
                self.say(f"  - 待关注: {drift[0].n} 个 active 成员无对应 active 锚点"
                         f"（回填/接线遗漏或 shadow 冲突保留）")

        def person_stats():
            row = _q("SELECT COUNT(*) AS n FROM person")[0].n
            verified = _q("SELECT COUNT(*) AS n FROM person "
                          "WHERE verification_status='verified'")[0].n
            merged = _q("SELECT COUNT(*) AS n FROM person "
                        "WHERE record_status='merged'")[0].n
            keys = _q("SELECT COUNT(*) AS n FROM person_identity")[0].n
            self.say(f"- 人员: {row}（verified {verified} / merged {merged}），"
                     f"已登记身份 key {keys}")

        _try("shadow", shadow_stats)
        _try("outbox", outbox_stats)
        _try("锚点", anchor_stats)
        _try("人员态", person_stats)

    def conclusion(self):
        self.h("结论与处置建议")
        self.say("- 本报告为只读盘点，未做任何写操作。")
        self.say("- 审计敏感行：登录/注册审计的 operation_data 内嵌完整 JWT（dict repr 单引号形态）。"
                 "最近 14 天内的行对任何具备 audit_log 读权限的角色构成活性 refresh 泄露面。"
                 "处置：D1-B3 审计脱敏上线后新行不再含令牌；存量行随 14 天自然过期全部失活；"
                 "无泄露证据不做历史清洗与批量撤会话（规格第 15 章：不臆造事故结论），"
                 "如后续确认泄露则按 bump+撤会话流程处置。")
        self.say("- 重名/学号重复仅作 D2 duplicate candidates 的弱线索（S01：同名不证明同人，禁止自动关联）。")
        self.say("- 第 6 节清单 = D2 空壳副号判定必须覆盖的引用面；缺引用检查器的模块不得判空壳。")
        self.say("- 疑似系统号（资讯君/快照超管等）待 D2 显式标注 account_kind=service；@seed.dev 等开发号"
                 "在生产部署前须确认生产库为零。")


def main():
    parser = argparse.ArgumentParser(description='D0 身份基线盘点（只读）')
    parser.add_argument('--full-emails', action='store_true', help='显示完整邮箱（默认脱敏）')
    parser.add_argument('--out', default=None, help='报告输出路径（默认 ../docs/记录/ 下按日期命名）')
    args = parser.parse_args()

    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
    inv = Inventory(full_emails=args.full_emails)
    inv.say(f"# {REPORT_TITLE}（{datetime.now().strftime('%Y-%m-%d %H:%M')}）")
    inv.say("> 只读扫描：本脚本不改任何表；用于 D1/D2 实施前的事实基线。")
    with app.app_context():
        inv.section_accounts()
        inv.section_super_admins()
        inv.section_service_accounts()
        inv.section_duplicates()
        inv.section_audit_secrets()
        inv.section_user_references()
        inv.section_non_jwt_identities(repo_root)
        inv.section_redis()
        inv.section_identity_runtime()
    inv.conclusion()

    out = args.out or os.path.join(repo_root, '..', 'docs', '记录',
                                   f"{datetime.now().strftime('%Y-%m-%d')}-身份基线盘点-D0.md")
    out = os.path.abspath(out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, 'w', encoding='utf-8') as f:
        f.write("\n".join(inv.lines) + "\n")
    print(f"\n[i] 报告已写入: {out}")


if __name__ == '__main__':
    main()

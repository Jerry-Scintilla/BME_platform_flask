"""实名名单解析服务（身份显示与人员检索改造 A1，2026-10-04 计划 §5.4）。

管理员手头通常只有实名姓名——本服务把「一列姓名」解析为人员与业务账号，
任命 / 组归属 / 选导生配对等批量入口共用，页面不得自行按名字建唯一 Map。

核心口径（计划 §5.4.2，验收 §7）：
  - 匹配只认 record_status='active' 且 verification_status='verified' 的
    Person.verified_name；strip 后严格相等（DB 等值命中后再按 Python 码点级
    复核，防 MySQL ci/ai 排序规则把大小写/重音差异当相等），不做模糊 /
    拼音 / 简繁 / 中点差异猜测；
  - 先解析身份候选，再独立判定业务资格——资格过滤不冒充姓名唯一性
    （「同名一人已入营/封禁、另一人符合条件」不得误选剩余一人）；
  - 候选按 Person 去重；账号解析：主参与号优先，其次 active standard 账号；
    多账号是「一个候选下列出账号」，不误当多人重名；
  - 显式 ID 与姓名同传时核对一致性，不一致标冲突、不静默优先任一字段；
  - 范围外（scope 不通过）的同名人员不进候选、不提示存在——「在可查范围
    内未找到实名匹配」；
  - 纯读服务：不写库、不发通知、不创建档案。check_verified 等 shadow 记账
    由调用方触发时，调用方收尾 rollback（预览端点惯例）。
"""
from exts import db
from models import (
    PersonModel, PersonIdentityModel, PersonPrimaryAccountModel, UserModel,
)


def normalize_name(raw):
    """姓名归一化：仅去首尾空白（计划 §5.4.2「默认只处理首尾空白，保留原文」）。
    空串 / None / 纯空白返回 None。"""
    if raw is None:
        return None
    name = str(raw).strip()
    return name or None


def _to_int(raw):
    """宽松整数解析（前端 JSON 的 user_id 可能是字符串数字）；非法返回 None。"""
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value


# ── 行状态（前端按状态渲染：自动预填 / 人工消歧 / 待处理） ──
ST_INVALID = 'invalid'                       # 既无姓名也无显式 ID / 姓名为空
ST_EXPLICIT_ID = 'explicit_id'               # 显式 ID 有效（姓名未传或无法核对）
ST_EXPLICIT_ID_NOT_FOUND = 'explicit_id_not_found'
ST_EXPLICIT_ID_CONFLICT = 'explicit_id_conflict'   # 显式 ID 的核验姓名与录入姓名不一致
ST_NOT_FOUND = 'not_found'                   # 可查范围内无当前核验姓名精确命中
ST_AMBIGUOUS = 'ambiguous'                   # 同名多名人员，需人工消歧
ST_UNIQUE = 'unique_matched'                 # 恰一名人员，业务账号可确定


class _Ctx:
    """一次批量解析的预取上下文：三段批量查询消 N+1（计划 §5.3）。"""

    def __init__(self, names, user_ids, scope, account_filter):
        self.scope = scope
        self.account_filter = account_filter
        # 1) 姓名 → 范围内当前已核验人员（DB 等值 + Python 严格复核）
        self.persons_by_name = {}
        self.persons_by_id = {}
        if names:
            rows = (PersonModel.query.filter(
                PersonModel.record_status == 'active',
                PersonModel.verification_status == 'verified',
                PersonModel.verified_name.in_(list(names))).all())
            for p in rows:
                if p.verified_name not in names:   # ci 排序规则复核（严格相等）
                    continue
                if scope is not None and not scope(p):
                    continue
                self.persons_by_name.setdefault(p.verified_name, []).append(p)
                self.persons_by_id[p.id] = p
        # 3) 显式 ID → 账号（及账号自身的人员档案，用于姓名一致性核对）
        self.users_by_id = {}
        if user_ids:
            for u in UserModel.query.filter(UserModel.id.in_(user_ids)).all():
                self.users_by_id[u.id] = u
        self.person_by_user = {}
        explicit_pids = {u.person_id for u in self.users_by_id.values() if u.person_id}
        if explicit_pids:
            for p in PersonModel.query.filter(PersonModel.id.in_(explicit_pids)).all():
                self.person_by_user[p.id] = p

        # 2) 人员 → 账号 / 主参与号 / 学校摘要（姓名命中 ∪ 显式 ID 引用的人员，
        #    显式 ID 人员的摘要也要能完整展示账号供冲突核对）
        pids = list(self.persons_by_id) + [
            pid for pid in explicit_pids if pid not in self.persons_by_id]
        self.users_by_person = {}
        self.primary_by_person = {}
        self.schools_by_person = {}
        if pids:
            for u in UserModel.query.filter(UserModel.person_id.in_(pids)).all():
                self.users_by_person.setdefault(u.person_id, []).append(u)
            for row in (PersonPrimaryAccountModel.query.filter(
                    PersonPrimaryAccountModel.person_id.in_(pids)).all()):
                self.primary_by_person[row.person_id] = row.user_id
            for ident in (PersonIdentityModel.query.filter(
                    PersonIdentityModel.person_id.in_(pids),
                    PersonIdentityModel.proof_status == 'verified').all()):
                self.schools_by_person.setdefault(ident.person_id, set()).add(
                    ident.issuer)


def _account_dict(ctx, user, person_id):
    """账号候选行：资格标注由 account_filter 提供（如本营角色 / merged 拦截），
    不改变身份候选枚举。"""
    item = {
        'user_id': user.id,
        'username': user.username,
        'is_primary': ctx.primary_by_person.get(person_id) == user.id,
        'status': user.status,
        'lifecycle': user.lifecycle,
        'account_kind': user.account_kind or 'standard',
        'eligible': True,
        'ineligible_reason': None,
    }
    if user.lifecycle == 'merged':
        item['eligible'] = False
        item['ineligible_reason'] = '已合并账号不产生新业务记录'
    elif user.status == 'banned':
        item['eligible'] = False
        item['ineligible_reason'] = '账号已封禁'
    elif ctx.account_filter is not None:
        ok, reason = ctx.account_filter(user)
        if not ok:
            item['eligible'] = False
            item['ineligible_reason'] = reason or '不符合本次操作条件'
    return item


def _person_dict(ctx, person):
    """人员候选摘要：学校只给 issuer 名（计划 §5.2「必要学校摘要，不整包返回」）。"""
    users = sorted(ctx.users_by_person.get(person.id, []), key=lambda u: u.id)
    # 主号在前、其余 user_id 升序；主号即正式参与对象（新参与默认主号，计划 §5.3）
    primary_uid = ctx.primary_by_person.get(person.id)
    users.sort(key=lambda u: 0 if u.id == primary_uid else 1)
    return {
        'person_id': person.id,
        'person_public_id': person.public_id,
        'verified_name': person.verified_name,
        'verification_status': person.verification_status,
        'schools': sorted(ctx.schools_by_person.get(person.id, [])),
        'accounts': [_account_dict(ctx, u, person.id) for u in users],
    }


def _resolve_unique(ctx, person):
    """唯一人员 → 建议业务账号：主参与号优先，其次唯一 active standard 账号；
    多个非主号 active standard 时账号不唯一，留给人工确认（不静默挑首个）。"""
    users = ctx.users_by_person.get(person.id, [])
    primary_uid = ctx.primary_by_person.get(person.id)
    if primary_uid is not None and any(u.id == primary_uid for u in users):
        return primary_uid
    active_std = [u for u in users
                  if u.status == 'active' and (u.lifecycle or 'active') == 'active'
                  and (u.account_kind or 'standard') == 'standard']
    if len(active_std) == 1:
        return active_std[0].id
    return None


def resolve_batch(entries, *, scope=None, account_filter=None):
    """批量解析实名名单（纯读）。

    entries：[{real_name?, user_id?, row_key?}]——real_name 为录入实名姓名
    （核验姓名口径），user_id 为可选显式账号 ID；两者同传时核对一致性。
    scope：callable(person)->bool，人员级可查范围（如仅本营授权成员）；
    account_filter：callable(user)->(ok, reason)，业务账号资格标注。

    返回与入参同序的行列表（形状见模块 docstring 与调用方），行内 resolved_user_id
    只在「唯一人员且账号可确定」或「显式 ID 一致」时给出；重名 / 冲突 / 无账号
    均回候选由人工消歧，绝不静默取首个匹配。"""
    entries = entries if isinstance(entries, list) else []
    names, user_ids = set(), set()
    for e in entries:
        if not isinstance(e, dict):
            continue
        name = normalize_name(e.get('real_name'))
        if name:
            names.add(name)
        uid = _to_int(e.get('user_id'))
        if uid is not None:
            user_ids.add(uid)
    ctx = _Ctx(names, user_ids, scope, account_filter)

    out = []
    for e in entries:
        row_key = e.get('row_key') if isinstance(e, dict) else None
        if not isinstance(e, dict):
            out.append({'row_key': row_key, 'status': ST_INVALID,
                        'message': '条目格式错误'})
            continue
        name = normalize_name(e.get('real_name'))
        raw_uid = e.get('user_id')
        uid = _to_int(raw_uid) if raw_uid is not None else None

        if name is None and uid is None:
            out.append({'row_key': row_key, 'input_name': None, 'status': ST_INVALID,
                        'message': '缺少实名姓名或账号 ID'})
            continue

        # ── 显式 ID 轨道：ID 是解析结果，姓名仅一致性核对 ──
        if uid is not None:
            user = ctx.users_by_id.get(uid)
            if user is None:
                out.append({'row_key': row_key, 'input_name': name,
                            'status': ST_EXPLICIT_ID_NOT_FOUND,
                            'user_id': uid,
                            'message': f'账号 #{uid} 不存在'})
                continue
            person = ctx.person_by_user.get(user.person_id) if user.person_id else None
            if name is not None and person is not None \
                    and person.verification_status == 'verified':
                if person.verified_name == name:
                    msg = None
                else:
                    # 显式 ID 与录入姓名不一致：标冲突，不静默优先任一字段（计划 §5.4.2）
                    out.append({
                        'row_key': row_key, 'input_name': name, 'user_id': uid,
                        'status': ST_EXPLICIT_ID_CONFLICT,
                        'resolved_user_id': None,
                        'message': (f'账号 #{uid}（昵称「{user.username}」）的核验姓名'
                                    f'「{person.verified_name}」与录入姓名「{name}」不一致，'
                                    '请核对原表或确认改名后重选'),
                        'candidates': [_person_dict(ctx, person)],
                    })
                    continue
            elif name is not None:
                msg = f'账号 #{uid} 当前未完成核验，无法核对录入姓名「{name}」'
            else:
                msg = None
            out.append({'row_key': row_key, 'input_name': name, 'user_id': uid,
                        'status': ST_EXPLICIT_ID,
                        'resolved_user_id': uid,
                        'username': user.username,
                        'message': msg,
                        'resolved_account': _account_dict(ctx, user, user.person_id),
                        'candidates': ([_person_dict(ctx, person)]
                                       if person is not None else [])})
            continue

        # ── 姓名轨道：范围内当前已核验姓名精确匹配 ──
        persons = ctx.persons_by_name.get(name, [])
        if not persons:
            out.append({'row_key': row_key, 'input_name': name,
                        'status': ST_NOT_FOUND, 'resolved_user_id': None,
                        'message': f'在可查范围内未找到实名匹配「{name}」',
                        'candidates': []})
            continue
        if len(persons) > 1:
            out.append({'row_key': row_key, 'input_name': name,
                        'status': ST_AMBIGUOUS, 'resolved_user_id': None,
                        'message': f'「{name}」有 {len(persons)} 名已核验人员，请选择',
                        'candidates': [_person_dict(ctx, p) for p in persons]})
            continue
        person = persons[0]
        resolved = _resolve_unique(ctx, person)
        msg = None
        account = None
        if resolved is None:
            msg = ('该人员无可用账号' if not ctx.users_by_person.get(person.id)
                   else '该人员有多个参与账号，请确认目标账号')
        else:
            account = next((u for u in ctx.users_by_person.get(person.id, [])
                            if u.id == resolved), None)
        out.append({'row_key': row_key, 'input_name': name,
                    'status': ST_UNIQUE, 'resolved_user_id': resolved,
                    'message': msg,
                    'resolved_account': (_account_dict(ctx, account, person.id)
                                         if account is not None else None),
                    'candidates': [_person_dict(ctx, person)]})
    return out

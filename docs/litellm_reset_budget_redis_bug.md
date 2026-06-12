# LiteLLM Bug：`reset_budget` 后台任务未清除 Redis spend 缓存

## 问题描述

**现象**：LiteLLM Proxy 为 internal user 设置 `budget_duration`（如 `5m`、`30d`）后，`budget_reset_at` 到期后用户的 spend 在数据库中被正确归零，但预算检查器仍从 Redis 缓存中读取旧的超限值，导致用户被持续拦截，直到 Redis key 自然过期（TTL 到期）为止。

**影响版本**：已在 v1.82.6、v1.83.14-stable、v1.84.0 上复现；跨版本持续存在。

**相关上游 Issue**：
- [#27735 - Virtual key BudgetExceededError uses stale spend](https://github.com/BerriAI/litellm/issues/27735)（OPEN）
- [#27639 - reserve_budget_for_request() leaks Redis spend counters](https://github.com/BerriAI/litellm/issues/27639)（CLOSED 但问题未完全修复）
- [#27481 - Tag budgets never reset](https://github.com/BerriAI/litellm/issues/27481)（OPEN）
- [#25495 - Organization budget does not reset](https://github.com/BerriAI/litellm/issues/25495)（OPEN）

---

## 根因分析

### 数据流

```
请求进来
  └─► _PROXY_MaxBudgetLimiter.async_pre_call_hook()
        └─► 读取 user spend
              ├─► 优先读 Redis：GET litellm:<cache_key>  ← 旧值，超限
              └─► Redis miss 时才读 DB
```

### 后台重置任务

```python
# litellm/proxy/common_utils/reset_budget_job.py

async def reset_budget(self):
    await self.reset_budget_for_litellm_keys()        # 重置 keys
    await self.reset_budget_for_litellm_users()       # 重置 users  ← 问题在这里
    await self.reset_budget_for_litellm_teams()       # 重置 teams
    await self.reset_budget_for_litellm_budget_table()
```

`reset_budget_for_litellm_users()` 的当前逻辑：

```python
async def reset_budget_for_litellm_users(self):
    # 1. 查询所有 budget_reset_at < NOW() 的用户
    users_to_reset = await prisma_client.db.litellm_usertable.find_many(
        where={"budget_reset_at": {"lt": now}}
    )
    # 2. 批量更新 DB：spend = 0，budget_reset_at = NOW() + budget_duration
    await prisma_client.db.litellm_usertable.update_many(...)

    # ❌ 缺失：未清除对应的 Redis 缓存
```

### 修复点

在步骤 2 之后，需要同步删除每个用户在 Redis 中缓存的 spend / user 对象，使下一次请求重新从 DB 读取归零后的值。

---

## PR 修复目标

### Fork 仓库

```
https://github.com/BerriAI/litellm
```

### 目标分支

`main`（或最新 stable 分支）

### 修改文件

**主要修改**：`litellm/proxy/common_utils/reset_budget_job.py`

在 `reset_budget_for_litellm_users()` 中，DB 更新完成后，逐个清除对应的 Redis 缓存：

```python
async def reset_budget_for_litellm_users(self):
    if self.prisma_client is None:
        return

    now = datetime.utcnow()
    users_to_reset = await self.prisma_client.db.litellm_usertable.find_many(
        where={"budget_reset_at": {"lt": now}}
    )
    if not users_to_reset:
        return

    new_reset_at = _get_next_reset_at(...)  # 按原有逻辑计算

    for user in users_to_reset:
        await self.prisma_client.db.litellm_usertable.update(
            where={"user_id": user.user_id},
            data={"spend": 0, "budget_reset_at": new_reset_at},
        )
        # ✅ 新增：清除 Redis 中该用户的缓存对象
        if self.proxy_logging_obj and self.proxy_logging_obj.internal_usage_cache:
            cache = self.proxy_logging_obj.internal_usage_cache
            # 用户对象缓存键（与 auth_checks.py 中 set_cache 一致）
            user_cache_key = f"litellm_user_key_{user.user_id}"
            await cache.async_delete_cache(user_cache_key)
```

**同样需要修改**（同一 Bug，不同实体）：
- `reset_budget_for_litellm_keys()` — 清除 `litellm_token_<key_hash>` 缓存
- `reset_budget_for_litellm_teams()` — 清除 team 缓存
- `reset_budget_for_litellm_budget_table()` — 清除 end-user 缓存

### 注意事项

1. **缓存键格式**：需对照 `litellm/proxy/auth/auth_checks.py` 中 `_cache_key_for_user()` 等函数确认实际 key 格式，避免猜测。
2. **批量 vs 逐个**：若 Redis 支持 `SCAN + DEL pattern`，可用 `delete_cache_keys_with_pattern` 批量清除，性能更好。
3. **向后兼容**：`internal_usage_cache` 在未配置 Redis 时为内存缓存，`async_delete_cache` 对两种情况均应有效。
4. **测试**：添加集成测试，验证 `budget_reset_at` 到期后第一个请求能正常通过（需 Mock Redis + DB）。

### PR 描述模板

```
fix(reset_budget_job): invalidate Redis cache after resetting user/key/team spend

When reset_budget_job runs and resets spend to 0 in the database, the
corresponding Redis cache entries are not invalidated. As a result, the
budget limiter continues reading the stale (over-budget) value from Redis
and blocks requests until the Redis TTL expires naturally.

This PR adds cache invalidation calls for users, keys, and teams after
their DB spend is reset, ensuring the budget reset takes effect immediately
on the next request.

Fixes #27735
Related: #27639, #27481, #25495
```

---

## 临时规避方案（已在本项目实施）

在 `budget_reset_at` 到期后，主动调用 `POST /user/update` 将 `spend` 置 0，
触发 LiteLLM 内部缓存更新逻辑（见 PR #10993 已合入的路径）。

详见 `litellm_client.py` 中的 `reset_user_spend()` 函数。

"""个人日程领域服务（2026-09-24 · Phase 1；09-24 Phase 2 修订事务边界）。

职责划分：
- preferences.py  偏好 Profile 的 get_or_create 与校验更新（含排程序闸锁）
- calendar.py     任务/固定日程/执行块三类对象 CRUD、冲突检测、事实流水、逾期口径
- reminders.py    提醒物料化（随写事务删旧插新）与到期扫描投递
- intent.py       文字意图提取（LLM 候选 + 服务端白名单校验，Phase 2）
- planner.py      确定性规则排程（生成/应用/撤销方案，零 LLM，Phase 2）
- capture.py      「说一句」录入 worker（线程池 + 条件更新状态机，Phase 2）

事务边界（v2 修订）：本包服务函数一律不 commit——commit 权属于三类执行上下文：
① 蓝图请求事务（服务只 add/flush）；
② reminders.scan_due_reminders（调度器上下文，每条提醒一个独立小事务）；
③ capture.process_capture（worker 线程上下文，每次处理至多三个小事务：
   claim、最终业务大事务、失败标记）。
②③ 是同一模式的推广：上下文外无请求事务兜底，必须自持事务；均为「每工作
单元一小事务」，【禁止跨 LLM 调用持有写事务】。

锁序规范（防死锁，Phase 2 新增）：将运行规划器（生成/应用/撤销方案，含
capture worker 的最终事务、POST /tasks 与 POST/PATCH /events 的挂钩路径）的
事务，在写任何 schedule_task/event/block 行之前，必须先 FOR UPDATE 该用户
schedule_profile 行（preferences.lock_profile）。profile 是唯一入口锁，持锁
后只向下取 task/block 行锁，不会回头要别的 profile，故锁图无环。

时间口径：全模块 naive 本地时间（Asia/Shanghai），比较一律 datetime.now()
（与 models.py 日程域横幅注释、全站既有 DateTime 列一致；刻意偏离规划文档
§10 的 UTC 建议，跨时区需求出现时再统一迁移）。

错误约定（蓝图映射 HTTP）：
- ValueError   入参/状态迁移不合法 → 400
- NotFound     对象不存在或不属于当前用户 → 404（不向客户端区分两者）
- VersionConflict 乐观锁版本不符 → 409
"""


class NotFound(Exception):
    """目标对象不存在或不属于当前用户（蓝图统一转 404）。"""


class VersionConflict(Exception):
    """乐观锁版本不符（蓝图转 409）。"""

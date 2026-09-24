"""个人日程领域服务（2026-09-24 · AI 智能日程管理模块 Phase 1）。

职责划分：
- preferences.py  偏好 Profile 的 get_or_create 与校验更新
- calendar.py     任务/固定日程/执行块三类对象 CRUD、冲突检测、事实流水、逾期口径
- reminders.py    提醒物料化（随写事务删旧插新）与到期扫描投递（独立小事务）

事务边界（重要）：
- 本包服务一律【不 commit】——只 add/flush，随调用方（蓝图请求事务）提交，
  与 services/workbench_items.py 同口径；
- 唯一例外是 reminders.scan_due_reminders：它在请求上下文之外运行（调度器/
  冒烟脚本直调），对每条提醒持有独立小事务并自行 commit，保证「通知创建与
  提醒状态翻转同生共死」的恰好一次投递。除它之外不得在服务层 commit。

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

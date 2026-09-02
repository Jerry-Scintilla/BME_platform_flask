"""迁移 15：CampPolicy 落表（营期升级阶段 1 收尾，幂等）。

设计（docs/营期升级重构-设计方案.md §3.3/附录）：
- 营期策略独立成表（application/formation/match_rule/project_limit/course_policy），
  建营时按类型默认值(CAMP_CATEGORY_DEFAULTS)生成一行，营期行可覆盖，避免主表膨胀。
- project_limit / course_policy 在阶段 3（项目营）/ 阶段 5（课程委托）开始消费；
  本迁移只为存量营补策略行并挂钩，不改变任何现行行为。

动作：
1. 建 camp_policy 表（存在即跳过）
2. camp_session + policy_id（存在即跳过）
3. 存量营逐营按 category 默认值生成策略行并回填 policy_id（已有 policy_id 的营跳过）

注意：默认值与 models.CAMP_CATEGORY_DEFAULTS 保持一致——改默认值请两处同步
（迁移是一次性回填，代码常量才是新营的生成源）。

用法：python scripts/migrate/migrate_15_camp_policy.py
回滚：DELETE camp_policy 相关行 + ALTER TABLE camp_session DROP COLUMN policy_id
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

import config  # noqa: E402
from sqlalchemy import create_engine, text, inspect  # noqa: E402

# 与 models.CAMP_CATEGORY_DEFAULTS 同步（本脚本不 import app 侧代码，避免触发启动钩子）
POLICY_DEFAULTS = {
    'learning': {
        'application': 'join_request', 'formation': 'preference_export',
        'match_rule': 'single_mentor', 'project_limit': None, 'course_policy': 'admin_managed',
    },
    'project': {
        'application': 'join_request', 'formation': 'preference_export',
        'match_rule': 'multi_project', 'project_limit': 3, 'course_policy': 'unit_creator',
    },
}

engine = create_engine(config.SQLALCHEMY_DATABASE_URI)
insp = inspect(engine)

with engine.connect() as conn:
    # 1) camp_policy 表
    if not insp.has_table('camp_policy'):
        conn.execute(text("""
            CREATE TABLE camp_policy (
                id INT AUTO_INCREMENT PRIMARY KEY,
                application VARCHAR(30) NOT NULL DEFAULT 'join_request' COMMENT '报名方式：join_request=申请入池+审批',
                formation VARCHAR(30) NOT NULL DEFAULT 'preference_export' COMMENT '组队方式：单轮志愿+导出+线下协调+回填',
                match_rule VARCHAR(30) NOT NULL DEFAULT 'single_mentor' COMMENT '归属规则：single_mentor/multi_project',
                project_limit INT NULL COMMENT '项目营每人参与上限（负责人计入；NULL=不限）',
                course_policy VARCHAR(30) NOT NULL DEFAULT 'admin_managed' COMMENT '课程策略（阶段5生效）',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
            ) CHARSET=utf8mb4
        """))
        conn.commit()
        print("[+] camp_policy 已创建")
    else:
        print("[=] camp_policy 已存在")

    # 2) camp_session.policy_id
    scols = {c['name'] for c in insp.get_columns('camp_session')}
    if 'policy_id' not in scols:
        conn.execute(text(
            "ALTER TABLE camp_session ADD COLUMN policy_id INT NULL COMMENT '营期策略（camp_policy.id）'"))
        conn.execute(text(
            "ALTER TABLE camp_session ADD CONSTRAINT fk_camp_session_policy "
            "FOREIGN KEY (policy_id) REFERENCES camp_policy(id)"))
        conn.commit()
        print("[+] camp_session.policy_id 已添加")
    else:
        print("[=] camp_session.policy_id 已存在")

    # 3) 存量营回填：按 category 默认值生成策略行并挂钩
    camps = conn.execute(text(
        "SELECT id, category FROM camp_session WHERE policy_id IS NULL")).fetchall()
    for cid, category in camps:
        p = POLICY_DEFAULTS.get(category or 'learning', POLICY_DEFAULTS['learning'])
        res = conn.execute(text(
            "INSERT INTO camp_policy (application, formation, match_rule, project_limit, course_policy) "
            "VALUES (:a, :f, :m, :pl, :c)"),
            {"a": p['application'], "f": p['formation'], "m": p['match_rule'],
             "pl": p['project_limit'], "c": p['course_policy']})
        conn.execute(text("UPDATE camp_session SET policy_id = :pid WHERE id = :cid"),
                     {"pid": res.lastrowid, "cid": cid})
        conn.commit()
        print(f"[~] 营 {cid}（{category}）已生成策略行 policy_id={res.lastrowid}")
    if not camps:
        print("[=] 无待回填营期")

    total = conn.execute(text(
        "SELECT COUNT(*) FROM camp_session WHERE policy_id IS NULL")).scalar()
    print(f"[ok] 未挂策略营期：{total}（应为 0）")
    print("[done] migrate_15 完成")

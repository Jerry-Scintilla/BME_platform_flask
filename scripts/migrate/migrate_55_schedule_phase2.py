"""迁移 55：个人日程 Phase 2——意图录入/排程方案/变更账三表（2026-09-24，幂等）。

背景：AI 日程模块 Phase 2 首轮「文字意图 + 智能排程」。schedule_capture 记录
一次「说一句」录入（request_id 幂等、LLM 结果服务端规范化后落 items_json）；
schedule_plan 存排程方案（ops 快照 + applied/reverted 状态）；schedule_change
是方案变更账（revert 按 id 逆序补偿的依据，after_json.entity_version 做版本
检查）。语音 input_type='audio' 预留。

附带升级：schedule_profile.automation_mode 全员 manual → suggest（适度自动）。
依据：Phase 1 设置面板该控件为 disabled，无任何用户主动改过，全部行都是出厂
默认 manual，全量升级零数据损失。注意【不可自动回滚】——升级后无法区分谁又
改回了 manual，回滚方式=用户在设置页自行改回；models.py 默认值已同步 suggest。

用法：python scripts/migrate/migrate_55_schedule_phase2.py
回滚：
  DROP TABLE schedule_change;
  DROP TABLE schedule_plan;
  DROP TABLE schedule_capture;
  （automation_mode 不做自动回退，见上）
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

import config  # noqa: E402
from sqlalchemy import create_engine, text, inspect  # noqa: E402

TABLES = {
    'schedule_capture': """
        CREATE TABLE schedule_capture (
            id INT AUTO_INCREMENT PRIMARY KEY,
            user_id INT NOT NULL COMMENT '录入人',
            request_id VARCHAR(64) NOT NULL COMMENT '客户端幂等键(uuid4)，重试复用',
            input_type VARCHAR(10) NOT NULL DEFAULT 'text' COMMENT 'text/audio(语音后置)',
            text VARCHAR(2000) NOT NULL COMMENT '用户原话',
            status VARCHAR(20) NOT NULL DEFAULT 'pending' COMMENT 'pending/processing/done/failed/clarify_needed',
            error VARCHAR(200) NULL COMMENT '用户可读中文错误',
            error_code VARCHAR(30) NULL COMMENT 'llm_timeout/llm_invalid/rate_limited/stale_timeout/internal',
            items_json TEXT NULL COMMENT '服务端规范化结果(含version)，非LLM原文',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uq_schedule_capture_request (user_id, request_id),
            INDEX ix_schedule_capture_user_time (user_id, created_at),
            CONSTRAINT fk_schedule_capture_user FOREIGN KEY (user_id) REFERENCES user(id))
        CHARSET=utf8mb4
    """,
    'schedule_plan': """
        CREATE TABLE schedule_plan (
            id INT AUTO_INCREMENT PRIMARY KEY,
            user_id INT NOT NULL,
            trigger VARCHAR(20) NOT NULL COMMENT 'capture/task/event/manual',
            status VARCHAR(20) NOT NULL DEFAULT 'proposed' COMMENT 'proposed/applied/expired/reverted',
            capture_id INT NULL COMMENT 'trigger=capture 时回链录入',
            reason VARCHAR(500) NULL COMMENT '中文排程摘要',
            diff_json TEXT NULL COMMENT '应用前 ops 快照(含版本基线)',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            applied_at DATETIME NULL,
            reverted_at DATETIME NULL,
            INDEX ix_schedule_plan_user_created (user_id, created_at),
            INDEX ix_schedule_plan_capture (capture_id),
            CONSTRAINT fk_schedule_plan_user FOREIGN KEY (user_id) REFERENCES user(id),
            CONSTRAINT fk_schedule_plan_capture FOREIGN KEY (capture_id) REFERENCES schedule_capture(id))
        CHARSET=utf8mb4
    """,
    'schedule_change': """
        CREATE TABLE schedule_change (
            id INT AUTO_INCREMENT PRIMARY KEY,
            user_id INT NOT NULL,
            plan_id INT NOT NULL COMMENT '所属方案',
            entity VARCHAR(10) NOT NULL COMMENT 'task/event/block',
            entity_id INT NOT NULL,
            operation VARCHAR(10) NOT NULL COMMENT 'create/update/cancel',
            before_json TEXT NULL COMMENT 'create 时 NULL；update 含起止时刻',
            after_json TEXT NOT NULL COMMENT '必含 entity_version(revert 版本检查依据)',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            INDEX ix_schedule_change_plan (plan_id, id),
            INDEX ix_schedule_change_user_time (user_id, created_at),
            CONSTRAINT fk_schedule_change_user FOREIGN KEY (user_id) REFERENCES user(id),
            CONSTRAINT fk_schedule_change_plan FOREIGN KEY (plan_id) REFERENCES schedule_plan(id))
        CHARSET=utf8mb4
    """,
}

engine = create_engine(config.SQLALCHEMY_DATABASE_URI)
insp = inspect(engine)

with engine.connect() as conn:
    for table, ddl in TABLES.items():
        if insp.has_table(table):
            print(f"[=] 表 {table} 已存在")
            continue
        conn.execute(text(ddl))
        conn.commit()
        print(f"[+] 表 {table} 已创建")

    # 附带升级：全员 manual → suggest（依据与不可回滚说明见 docstring）
    result = conn.execute(text(
        "UPDATE schedule_profile SET automation_mode='suggest' WHERE automation_mode='manual'"))
    conn.commit()
    print(f"[*] automation_mode 升级 suggest：影响 {result.rowcount} 行")
    print("[done] migrate_55 完成")

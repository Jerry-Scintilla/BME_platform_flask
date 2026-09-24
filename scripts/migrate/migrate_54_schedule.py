"""迁移 54：个人日程域六表（2026-09-24，幂等）。

背景：AI 智能日程管理模块 Phase 1「个人日程基础」。面向全体用户的私人日程，
与教学 Task 解耦：schedule_task（私人任务）/ schedule_event（固定日程）/
schedule_block（任务执行块）/ schedule_profile（偏好）/ schedule_activity
（不可变事实流水）/ schedule_reminder（提醒账本，Phase 1 单站内通道时与
投递状态合一）。Phase 2 的 capture/plan/change 等表本期不建。

时间口径：全列 naive 本地时间（Asia/Shanghai），与全站既有 DateTime 列一致
（刻意偏离规划文档 §10 的 UTC 建议，跨时区需求出现时再统一迁移）。

用法：python scripts/migrate/migrate_54_schedule.py
回滚（按外键依赖倒序）：
  DROP TABLE schedule_reminder;
  DROP TABLE schedule_activity;
  DROP TABLE schedule_block;
  DROP TABLE schedule_event;
  DROP TABLE schedule_task;
  DROP TABLE schedule_profile;

Housekeeping 备注：reminder 终态行（delivered/expired/failed）只增不减，
个人量级可忽略；将来按 delivered_at 归档清理（Phase 1 不做定时任务）。
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

import config  # noqa: E402
from sqlalchemy import create_engine, text, inspect  # noqa: E402

TABLES = {
    'schedule_profile': """
        CREATE TABLE schedule_profile (
            id INT AUTO_INCREMENT PRIMARY KEY,
            user_id INT NOT NULL COMMENT '归属用户（每人一行）',
            timezone VARCHAR(64) NOT NULL DEFAULT 'Asia/Shanghai' COMMENT 'IANA 时区，Phase 1 仅存储',
            day_start_time VARCHAR(5) NOT NULL DEFAULT '09:00' COMMENT '日可安排窗口起 HH:MM',
            day_end_time VARCHAR(5) NOT NULL DEFAULT '22:00' COMMENT 'date-only 截止提醒基线 HH:MM',
            default_reminder_minutes INT NOT NULL DEFAULT 15 COMMENT '事项未指定时的提醒提前量(分钟)',
            automation_mode VARCHAR(20) NOT NULL DEFAULT 'manual' COMMENT 'manual/suggest/auto，Phase 1 仅存储',
            version INT NOT NULL DEFAULT 1 COMMENT '乐观锁',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uq_schedule_profile_user (user_id),
            CONSTRAINT fk_schedule_profile_user FOREIGN KEY (user_id) REFERENCES user(id))
        CHARSET=utf8mb4
    """,
    'schedule_task': """
        CREATE TABLE schedule_task (
            id INT AUTO_INCREMENT PRIMARY KEY,
            owner_id INT NOT NULL COMMENT '归属（服务端从登录态推导）',
            title VARCHAR(200) NOT NULL,
            description TEXT,
            status VARCHAR(20) NOT NULL DEFAULT 'open' COMMENT 'open/done/cancelled',
            due_at DATETIME NULL COMMENT '精确截止时刻（naive 本地）',
            due_date DATE NULL COMMENT 'date-only 截止',
            deadline_precision VARCHAR(20) NOT NULL DEFAULT 'none' COMMENT 'none/datetime/date，与上两列自洽',
            estimated_minutes INT NULL COMMENT '估时',
            remaining_minutes INT NULL COMMENT '进度反馈的剩余量',
            priority VARCHAR(10) NOT NULL DEFAULT 'medium' COMMENT 'low/medium/high',
            splittable TINYINT(1) NOT NULL DEFAULT 1 COMMENT '可拆分，Phase 2 排程消费',
            reminder_minutes INT NULL COMMENT '事项级提醒提前量，NULL 用 profile 默认',
            version INT NOT NULL DEFAULT 1 COMMENT '乐观锁',
            completed_at DATETIME NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            INDEX ix_schedule_task_owner_status_due (owner_id, status, due_at),
            INDEX ix_schedule_task_owner_updated (owner_id, updated_at),
            CONSTRAINT fk_schedule_task_user FOREIGN KEY (owner_id) REFERENCES user(id))
        CHARSET=utf8mb4
    """,
    'schedule_event': """
        CREATE TABLE schedule_event (
            id INT AUTO_INCREMENT PRIMARY KEY,
            owner_id INT NOT NULL COMMENT '归属',
            title VARCHAR(200) NOT NULL,
            description TEXT,
            location VARCHAR(200) NULL,
            start_at DATETIME NOT NULL,
            end_at DATETIME NOT NULL,
            all_day TINYINT(1) NOT NULL DEFAULT 0 COMMENT '全天：不参与精确区间重叠',
            busy TINYINT(1) NOT NULL DEFAULT 1 COMMENT '占用忙闲：0 不参与冲突检测',
            status VARCHAR(20) NOT NULL DEFAULT 'active' COMMENT 'active/cancelled(软删)',
            reminder_minutes INT NULL,
            version INT NOT NULL DEFAULT 1 COMMENT '乐观锁',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            INDEX ix_schedule_event_owner_start (owner_id, start_at),
            CONSTRAINT fk_schedule_event_user FOREIGN KEY (owner_id) REFERENCES user(id))
        CHARSET=utf8mb4
    """,
    'schedule_block': """
        CREATE TABLE schedule_block (
            id INT AUTO_INCREMENT PRIMARY KEY,
            owner_id INT NOT NULL COMMENT '冗余归属（免 join 查周历/冲突）',
            task_id INT NOT NULL COMMENT '所属任务',
            start_at DATETIME NOT NULL,
            end_at DATETIME NOT NULL,
            locked TINYINT(1) NOT NULL DEFAULT 1 COMMENT '用户锁定不被自动重排；Phase 2 自动块为 0',
            status VARCHAR(20) NOT NULL DEFAULT 'planned' COMMENT 'planned/done/cancelled',
            version INT NOT NULL DEFAULT 1 COMMENT '乐观锁',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            INDEX ix_schedule_block_owner_start (owner_id, start_at),
            INDEX ix_schedule_block_task (task_id),
            CONSTRAINT fk_schedule_block_user FOREIGN KEY (owner_id) REFERENCES user(id),
            CONSTRAINT fk_schedule_block_task FOREIGN KEY (task_id) REFERENCES schedule_task(id) ON DELETE CASCADE)
        CHARSET=utf8mb4
    """,
    'schedule_activity': """
        CREATE TABLE schedule_activity (
            id INT AUTO_INCREMENT PRIMARY KEY,
            user_id INT NOT NULL COMMENT '操作者',
            task_id INT NULL COMMENT '关联任务（可空，事件类动作为空）',
            block_id INT NULL COMMENT '关联执行块（可空）',
            action VARCHAR(30) NOT NULL COMMENT 'task_*/event_*/block_* 动作白名单',
            occurred_at DATETIME NOT NULL COMMENT '实际发生时刻（支持补记）',
            recorded_at DATETIME NOT NULL COMMENT '录入时刻',
            actual_minutes INT NULL COMMENT '用户报告的实际投入',
            note VARCHAR(500) NULL COMMENT '如未完成原因',
            source VARCHAR(20) NOT NULL DEFAULT 'manual' COMMENT 'manual/ai/plan',
            INDEX ix_schedule_activity_user_time (user_id, occurred_at),
            INDEX ix_schedule_activity_task (task_id),
            CONSTRAINT fk_schedule_activity_user FOREIGN KEY (user_id) REFERENCES user(id),
            CONSTRAINT fk_schedule_activity_task FOREIGN KEY (task_id) REFERENCES schedule_task(id) ON DELETE CASCADE)
        CHARSET=utf8mb4
    """,
    'schedule_reminder': """
        CREATE TABLE schedule_reminder (
            id INT AUTO_INCREMENT PRIMARY KEY,
            user_id INT NOT NULL COMMENT '收提醒的用户',
            target_type VARCHAR(20) NOT NULL COMMENT 'event/task/block',
            target_id INT NOT NULL,
            target_version INT NOT NULL COMMENT '物料化时对象 version，发送前复核',
            kind VARCHAR(20) NOT NULL COMMENT 'start/due',
            trigger_at DATETIME NOT NULL COMMENT '触发时刻（naive 本地）',
            status VARCHAR(20) NOT NULL DEFAULT 'pending' COMMENT 'pending/delivered/cancelled/expired/failed',
            title_snapshot VARCHAR(200) NULL COMMENT '物料化时标题快照，兜底展示',
            notification_id INT NULL COMMENT '投递产生的通知 id 回填',
            delivered_at DATETIME NULL,
            attempts INT NOT NULL DEFAULT 0,
            last_error VARCHAR(500) NULL,
            next_retry_at DATETIME NULL COMMENT 'failed 重试到期时刻',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uq_schedule_reminder_dedup (user_id, target_type, target_id, kind, target_version),
            INDEX ix_schedule_reminder_scan (status, trigger_at),
            CONSTRAINT fk_schedule_reminder_user FOREIGN KEY (user_id) REFERENCES user(id))
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
    print("[done] migrate_54 完成")

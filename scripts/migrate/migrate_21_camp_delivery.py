"""迁移 21：项目营模板交付与档案表组（营期升级阶段 4 / 设计方案 v1.3 §3.10-3.11，幂等）。

表组：ProjectTemplate / ProjectTemplateNode / CampMilestone / CampSubmissionVersion /
CampSubmissionAttachment / CampOutcome / CampArchive / CampArchiveRevision。

三层解耦（09-12 拍板）：模板管共性（节点施工图，三起点创建，结题 archived 可复制=资产回流）/
里程碑管交付（模板实例化，可增删调时，节点级 submit_mode 双模式提交）/ 课程管学习（阶段5，不在本批）。

用法：python scripts/migrate/migrate_21_camp_delivery.py
回滚：DROP 八表（表间 FK 按序）
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

import config  # noqa: E402
from sqlalchemy import create_engine, text, inspect  # noqa: E402

TABLES = {
    'project_template': """
        CREATE TABLE project_template (
            id INT AUTO_INCREMENT PRIMARY KEY,
            name VARCHAR(100) NOT NULL,
            description TEXT NULL,
            scope VARCHAR(20) NOT NULL DEFAULT 'unit' COMMENT 'platform=平台默认 / unit=项目模板',
            category VARCHAR(50) NULL COMMENT '适用项目类别（筛选用）',
            camp_session_id INT NULL COMMENT 'unit 模板归属营期',
            unit_id INT NULL COMMENT 'unit 模板归属项目（1:1）',
            created_by INT NOT NULL,
            cloned_from_id INT NULL COMMENT '复制溯源（自引用）',
            status VARCHAR(20) NOT NULL DEFAULT 'active' COMMENT 'active / archived（结题冻结置位，可被复制）',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            INDEX ix_tpl_camp (camp_session_id),
            UNIQUE KEY uq_tpl_unit (unit_id),
            CONSTRAINT fk_tpl_camp FOREIGN KEY (camp_session_id) REFERENCES camp_session(id),
            CONSTRAINT fk_tpl_unit FOREIGN KEY (unit_id) REFERENCES camp_unit(id),
            CONSTRAINT fk_tpl_creator FOREIGN KEY (created_by) REFERENCES user(id)
        ) CHARSET=utf8mb4
    """,
    'project_template_node': """
        CREATE TABLE project_template_node (
            id INT AUTO_INCREMENT PRIMARY KEY,
            template_id INT NOT NULL,
            sort_order INT NOT NULL DEFAULT 1,
            title VARCHAR(100) NOT NULL,
            description TEXT NULL COMMENT '节点说明',
            deliverable_req TEXT NULL COMMENT '交付要求',
            material_note TEXT NULL COMMENT '材料模板说明',
            recommended_course_ids TEXT NULL COMMENT 'JSON 课程 id 数组（软链不复制）',
            submit_mode VARCHAR(10) NOT NULL DEFAULT 'team' COMMENT 'team 整队交 / member 个人交（节点级）',
            INDEX ix_tpn_tpl (template_id),
            CONSTRAINT fk_tpn_tpl FOREIGN KEY (template_id) REFERENCES project_template(id)
        ) CHARSET=utf8mb4
    """,
    'camp_milestone': """
        CREATE TABLE camp_milestone (
            id INT AUTO_INCREMENT PRIMARY KEY,
            camp_session_id INT NOT NULL,
            unit_id INT NOT NULL,
            node_id INT NULL COMMENT '来源模板节点（删节点不级联删里程碑）',
            title VARCHAR(100) NOT NULL,
            description TEXT NULL,
            requirement TEXT NULL COMMENT '交付要求（实例化随节点，可改）',
            due_date DATE NULL,
            order_no INT NOT NULL DEFAULT 1,
            submit_mode VARCHAR(10) NOT NULL DEFAULT 'team',
            status VARCHAR(20) NOT NULL DEFAULT 'open' COMMENT 'open/submitted/returned/approved',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            INDEX ix_ms_camp (camp_session_id),
            INDEX ix_ms_unit (unit_id),
            CONSTRAINT fk_ms_camp FOREIGN KEY (camp_session_id) REFERENCES camp_session(id),
            CONSTRAINT fk_ms_unit FOREIGN KEY (unit_id) REFERENCES camp_unit(id),
            CONSTRAINT fk_ms_node FOREIGN KEY (node_id) REFERENCES project_template_node(id)
        ) CHARSET=utf8mb4
    """,
    'camp_submission_version': """
        CREATE TABLE camp_submission_version (
            id INT AUTO_INCREMENT PRIMARY KEY,
            milestone_id INT NOT NULL,
            version INT NOT NULL,
            submitted_by INT NOT NULL,
            content TEXT NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'submitted' COMMENT 'submitted/returned/approved/superseded',
            reviewed_by INT NULL, reviewed_at DATETIME NULL, review_note VARCHAR(500) NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            INDEX ix_csv_milestone (milestone_id),
            INDEX ix_csv_submitter (submitted_by),
            UNIQUE KEY uq_csv_milestone_sub_ver (milestone_id, submitted_by, version),
            CONSTRAINT fk_csv_milestone FOREIGN KEY (milestone_id) REFERENCES camp_milestone(id),
            CONSTRAINT fk_csv_submitter FOREIGN KEY (submitted_by) REFERENCES user(id)
        ) CHARSET=utf8mb4
    """,
    'camp_submission_attachment': """
        CREATE TABLE camp_submission_attachment (
            id INT AUTO_INCREMENT PRIMARY KEY,
            submission_id INT NOT NULL,
            object_key VARCHAR(255) NOT NULL COMMENT 'MinIO 对象键',
            filename VARCHAR(200) NOT NULL,
            size INT NULL, content_type VARCHAR(100) NULL,
            is_asset TINYINT(1) NOT NULL DEFAULT 0 COMMENT '资产回流打标（冻结时对 approved 版本置位）',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            INDEX ix_csa_submission (submission_id),
            CONSTRAINT fk_csa_submission FOREIGN KEY (submission_id) REFERENCES camp_submission_version(id)
        ) CHARSET=utf8mb4
    """,
    'camp_outcome': """
        CREATE TABLE camp_outcome (
            id INT AUTO_INCREMENT PRIMARY KEY,
            unit_id INT NOT NULL,
            title VARCHAR(200) NOT NULL,
            description TEXT NULL,
            contributor_ids TEXT NULL COMMENT 'JSON 贡献者 user_id 数组',
            status VARCHAR(20) NOT NULL DEFAULT 'submitted' COMMENT 'submitted/verified/rejected（未核验不入档案）',
            submitted_by INT NOT NULL,
            verified_by INT NULL, verified_at DATETIME NULL, reject_reason VARCHAR(500) NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            INDEX ix_outcome_unit (unit_id),
            CONSTRAINT fk_outcome_unit FOREIGN KEY (unit_id) REFERENCES camp_unit(id),
            CONSTRAINT fk_outcome_submitter FOREIGN KEY (submitted_by) REFERENCES user(id)
        ) CHARSET=utf8mb4
    """,
    'camp_archive': """
        CREATE TABLE camp_archive (
            id INT AUTO_INCREMENT PRIMARY KEY,
            camp_session_id INT NOT NULL,
            snapshot LONGTEXT NULL COMMENT '关键事实 JSON（成员/项目/里程碑终态/已核验成果）',
            version INT NOT NULL DEFAULT 1,
            frozen_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            frozen_by INT NOT NULL,
            UNIQUE KEY uq_archive_camp (camp_session_id),
            CONSTRAINT fk_archive_camp FOREIGN KEY (camp_session_id) REFERENCES camp_session(id),
            CONSTRAINT fk_archive_freezer FOREIGN KEY (frozen_by) REFERENCES user(id)
        ) CHARSET=utf8mb4
    """,
    'camp_archive_revision': """
        CREATE TABLE camp_archive_revision (
            id INT AUTO_INCREMENT PRIMARY KEY,
            archive_id INT NOT NULL,
            version INT NOT NULL COMMENT '对应档案修正后的版本号',
            reason VARCHAR(500) NOT NULL,
            patch TEXT NULL COMMENT 'JSON：修正内容描述（快照原文不可变）',
            operator_id INT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            INDEX ix_rev_archive (archive_id),
            CONSTRAINT fk_rev_archive FOREIGN KEY (archive_id) REFERENCES camp_archive(id),
            CONSTRAINT fk_rev_operator FOREIGN KEY (operator_id) REFERENCES user(id)
        ) CHARSET=utf8mb4
    """,
}

engine = create_engine(config.SQLALCHEMY_DATABASE_URI)
insp = inspect(engine)

with engine.connect() as conn:
    for table, ddl in TABLES.items():
        if insp.has_table(table):
            print(f"[=] {table} 已存在")
            continue
        conn.execute(text(ddl))
        conn.commit()
        print(f"[+] {table} 已创建")
    print("[done] migrate_21 完成")

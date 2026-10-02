#!/usr/bin/env bash
# BME 迁移链执行器：预演与正式切换共用（在目标代码根目录运行，.env 决定目标库/存储）
# 顺序要点（2026-09-15 预演推导）：
#   - migrate_01~08 本机 7 月已应用过（预演核实：role 列/营期骨架/权限 17 条均在），跳过；
#     新 app.py 启动期 ai-topic 查询依赖新列，01~08（import app 型）在旧库上必挂
#   - migrate_09 例外：生产库没跑过（camp_session 缺 mentor_selection_* 6 列，
#     /camp/sessions 会 500）。它是 import app 型，须排在 13/17/19 之后
#   - migrate_10 --force：跳过 user 429（琴晓谭 role=super_admin/user_mode=user）等价性中止
#   - 23/33 先于 create_all 引导跑（自带严格 DDL+种子）；引导放在 12 之后、13 之前：
#     13/20 依赖新表（camp_mentor_eligibility/camp_unit 等），而完整 app 启动又依赖
#     13 的 user.level 列 → 用 db.metadata.create_all 破循环
set -uo pipefail
cd "$(dirname "$0")"
PY=.venv/bin/python
LOG=/tmp/migrate_run.log
: > "$LOG"

run() {
  echo "──── $* ────"
  if $PY "$@" >> "$LOG" 2>&1; then echo "  ✓"; else
    echo "✗✗✗ 失败：$*（最后 25 行错误，全文在 $LOG）"; tail -25 "$LOG"; exit 1
  fi
}

mkdir -p log
run scripts/migrate/migrate_10_identity.py --force
run scripts/migrate/migrate_11_camp_skeleton.py
run scripts/migrate/migrate_12_state_machine.py
# 23/33 自带严格 DDL（server 端 DEFAULT）+ 原生 SQL 种子，必须先于 create_all 自建，
# 否则 create_all 版无默认值的表会让 33 的 INSERT 触发 MySQL 1364
run scripts/migrate/migrate_23_club_officer.py
run scripts/migrate/migrate_33_club_org.py

echo "──── bootstrap create_all（不启动完整 app）────"
if $PY -c "import config; from sqlalchemy import create_engine; from exts import db; import models; db.metadata.create_all(create_engine(config.SQLALCHEMY_DATABASE_URI))" >> "$LOG" 2>&1; then
  echo "  ✓"
else echo "✗✗✗ create_all 失败"; tail -25 "$LOG"; exit 1; fi

for n in 13_level 14_mentor_review 15_camp_policy 16_join_tag 17_email_notify \
         18_mentor_favorite 19_user_status 09_mentor_selection \
         20_camp_project 21_camp_delivery \
         22_showcase 24_chapter_certification 25_attendance_mode \
         26_unit_activity 27_application_template_nodes 28_chapter_cert_score \
         29_node_evaluation 30_chapter_material 31_camp_learning_progress \
         32_static_media; do
  run scripts/migrate/migrate_${n}.py
done
run init_seats.py
# D1 身份安全地基（2026-10-01）：34-61 号迁移按历次发布 SOP 手工执行，此处从 62 起续链
run scripts/migrate/migrate_62_auth_foundation.py
# D2 人员层（2026-10-01）：person 六表 + user.person_id（幂等；复合外键依赖内部顺序）
run scripts/migrate/migrate_63_person_layer.py
# D3a 核验与审批（2026-10-01）：学校配置/申请/挑战三表 + sysu 种子（幂等；索引兜底补齐）
run scripts/migrate/migrate_64_identity_verification.py
# D3b 认领与归并（2026-10-01）：案例/证明/授权/占位/审批五表（零行为变化）
run scripts/migrate/migrate_65_identity_link.py
# D5 参与锚点（2026-10-02）：camp_member/camp_unit_member 三元组唯一键前置 + 锚点四表；
# 影子登记与 veto 提升随后跑 scripts/backfill_person_participation.py --apply
run scripts/migrate/migrate_66_person_participation.py
# D3c 外校名册 + 恢复申诉骨架（2026-10-02）：名册/邀请/恢复三表 + external:scuec 种子
run scripts/migrate/migrate_67_external_roster.py
# P2 收尾（2026-10-02）：辅助账号授权表 + 身份岗位权限位种子
run scripts/migrate/migrate_68_auxiliary_permissions.py

echo "──── 最终 app 启动验证 ────"
if $PY -c "from app import app; print('boot ok')" >> "$LOG" 2>&1; then echo "  ✓"; else
  echo "✗✗✗ app 启动失败"; tail -25 "$LOG"; exit 1; fi

echo "═══ 迁移链全部完成 ═══"

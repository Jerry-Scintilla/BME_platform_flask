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

echo "──── 最终 app 启动验证 ────"
if $PY -c "from app import app; print('boot ok')" >> "$LOG" 2>&1; then echo "  ✓"; else
  echo "✗✗✗ app 启动失败"; tail -25 "$LOG"; exit 1; fi

echo "═══ 迁移链全部完成 ═══"

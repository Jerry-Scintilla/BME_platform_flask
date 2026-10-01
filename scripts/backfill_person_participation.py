"""回填脚本：D5 参与锚点影子登记 + 账号级 veto 提升为人员级（规格 9.2/9.4）。

规则：
  - camp_member/unit_member 的 active 行 → camp/unit_person_participation
    （幂等：已有锚点跳过；同 (营/单元, person) 多账号 = 冲突——保留已有锚点
    不覆盖，进冲突报告由负责人裁定，不强行删记录）
  - work_access_grant status='vetoed'（账号级否决）→ person_workspace_restriction
    （幂等；换主账号不得绕过）
  - 目标账号缺人员档案（理论上 D2 回填后不发生）→ 异常报告

用法（项目根）：
  .venv/bin/python scripts/backfill_person_participation.py --dry-run
  .venv/bin/python scripts/backfill_person_participation.py --apply
"""
import argparse
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import app          # noqa: E402,F401


def main():
    parser = argparse.ArgumentParser(description='参与锚点影子登记与 veto 提升（幂等）')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if args.dry_run and args.apply:
        parser.error('--dry-run 与 --apply 互斥')

    from exts import db
    from models import (CampMember, CampPersonParticipationModel, CampUnitMember,
                        PersonModel, UnitPersonParticipationModel,
                        WorkAccessGrant, WorkWorkspace)
    from services.identity import enforcement, events
    from services.identity import person as identity_person

    batch_id = datetime.now().strftime('participation:%Y%m%d-%H%M%S')
    with app.app_context():
        # ── 营期参与锚点 ────────────────────────────────────────
        stats = {'registered': 0, 'skipped': 0, 'conflicts': [], 'no_person': []}
        for m in CampMember.query.filter_by(status='active').order_by(CampMember.id).all():
            user = m.user if hasattr(m, 'user') else None
            from models import UserModel
            user = user or db.session.get(UserModel, m.user_id)
            person = db.session.get(PersonModel, user.person_id) if user and user.person_id else None
            if person is None:
                stats['no_person'].append((m.camp_session_id, m.user_id))
                continue
            existing = db.session.get(CampPersonParticipationModel, (m.camp_session_id, person.id))
            if existing is not None:
                if existing.user_id != m.user_id:
                    stats['conflicts'].append(
                        (m.camp_session_id, person.id, existing.user_id, m.user_id))
                else:
                    stats['skipped'] += 1
                continue
            if not args.dry_run:
                db.session.add(CampPersonParticipationModel(
                    camp_session_id=m.camp_session_id, person_id=person.id,
                    user_id=m.user_id, camp_member_id=m.id))
            stats['registered'] += 1

        # ── 单元参与锚点 ────────────────────────────────────────
        ustats = {'registered': 0, 'skipped': 0, 'conflicts': [], 'no_person': []}
        for um in CampUnitMember.query.filter_by(status='active').order_by(CampUnitMember.id).all():
            from models import UserModel
            user = db.session.get(UserModel, um.user_id)
            person = db.session.get(PersonModel, user.person_id) if user and user.person_id else None
            if person is None:
                ustats['no_person'].append((um.unit_id, um.user_id))
                continue
            existing = db.session.get(UnitPersonParticipationModel, (um.unit_id, person.id))
            if existing is not None:
                if existing.user_id != um.user_id:
                    ustats['conflicts'].append((um.unit_id, person.id, existing.user_id, um.user_id))
                else:
                    ustats['skipped'] += 1
                continue
            if not args.dry_run:
                db.session.add(UnitPersonParticipationModel(
                    unit_id=um.unit_id, person_id=person.id,
                    user_id=um.user_id, unit_member_id=um.id))
            ustats['registered'] += 1

        # ── 账号级 veto → 人员级 ────────────────────────────────
        vetoes = WorkAccessGrant.query.filter_by(status='vetoed').all()
        veto_migrated = 0
        for g in vetoes:
            from models import UserModel
            user = db.session.get(UserModel, g.user_id)
            if not user or not user.person_id:
                continue
            if PersonModel.query.get is None:
                continue
            if not args.dry_run:
                enforcement.migrate_account_veto_to_person(
                    user, g.workspace_id,
                    reason=g.revoke_reason or '账号级否决迁移（回填）',
                    source_event='backfill')
            veto_migrated += 1

        print(f"[{'计划' if args.dry_run else '执行'}] 营期锚点：登记 {stats['registered']}"
              f" / 跳过 {stats['skipped']} / 冲突 {len(stats['conflicts'])}"
              f" / 缺档案 {len(stats['no_person'])}")
        print(f"[{'计划' if args.dry_run else '执行'}] 单元锚点：登记 {ustats['registered']}"
              f" / 跳过 {ustats['skipped']} / 冲突 {len(ustats['conflicts'])}"
              f" / 缺档案 {len(ustats['no_person'])}")
        print(f"[{'计划' if args.dry_run else '执行'}] veto 提升：{veto_migrated} 条")
        for sid, pid, u1, u2 in (stats['conflicts'] + ustats['conflicts'])[:20]:
            print(f"  [冲突] scope#{sid} person#{pid}：锚点 user#{u1} vs 成员 user#{u2}"
                  f"——保留原锚点，需负责人裁定")
        for sid, uid in (stats['no_person'] + ustats['no_person'])[:10]:
            print(f"  [异常] scope#{sid} user#{uid} 缺人员档案（不应发生，检查 D2 回填）")

        if args.dry_run:
            db.session.rollback()
            print('[i] 干跑未落库；确认后 --apply。')
            return
        events.record_event(
            'identity.participation.backfill', actor_user_id=0,
            evidence_refs={'note': batch_id},
            reason=f"camp={stats['registered']} unit={ustats['registered']} "
                   f"conflicts={len(stats['conflicts']) + len(ustats['conflicts'])} "
                   f"veto={veto_migrated}")
        db.session.commit()
        print(f'[+] 已落库并留痕（{batch_id}）。重跑幂等。')


if __name__ == '__main__':
    main()

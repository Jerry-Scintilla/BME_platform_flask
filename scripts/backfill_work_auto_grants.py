"""回填脚本：按现存组织行生成工作区自动授权（授权自动化方案 §8，D1=乙全量回填）。

对每个「启用中 + auto_grant 开」的组工作区，按当前归属/任职重算 (user, ws) 的
自动授权行（幂等：已有同形自动行跳过；尊重 veto；manual 行让位不动）。

用法（项目根）：
  .venv/bin/python scripts/backfill_work_auto_grants.py --dry-run            # 只看报告
  .venv/bin/python scripts/backfill_work_auto_grants.py --apply              # 全量落库
  .venv/bin/python scripts/backfill_work_auto_grants.py --apply --group 3    # 只回填一个组
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import app          # noqa: E402,F401


def main():
    parser = argparse.ArgumentParser(description='工作区自动授权回填（幂等）')
    parser.add_argument('--dry-run', action='store_true', help='只输出计划不落库')
    parser.add_argument('--apply', action='store_true', help='执行落库')
    parser.add_argument('--group', type=int, default=None, help='只处理该 club_group_id')
    args = parser.parse_args()
    if args.dry_run and args.apply:
        parser.error('--dry-run 与 --apply 互斥')

    from exts import db
    from models import ClubGroup, WorkWorkspace
    from services.work import provisioning

    with app.app_context():
        q = WorkWorkspace.query.filter_by(scope='group', status='active')
        if args.group:
            q = q.filter_by(club_group_id=args.group)
        wss = q.order_by(WorkWorkspace.id).all()
        if not wss:
            print('[i] 没有符合条件的组工作区（含 auto_grant 关闭的）')
            return
        gnames = {g.id: g.name for g in ClubGroup.query.all()}
        total = {}
        rows = []
        for ws in wss:
            if not ws.auto_grant:
                rows.append((ws, None, f'{gnames.get(ws.club_group_id, ws.club_group_id)}：自动授已关闭，跳过（--apply 也不会开）'))
                continue
            if args.dry_run:
                # 干跑：在保存点里跑完即回滚，只取计数
                nested = db.session.begin_nested()
                try:
                    counts = provisioning.backfill_workspace(ws, event='回填干跑')
                finally:
                    nested.rollback()
                rows.append((ws, counts, None))
            else:
                counts = provisioning.backfill_workspace(ws, event='回填')
                rows.append((ws, counts, None))
            if counts:
                for k, v in counts.items():
                    total[k] = total.get(k, 0) + v
        for ws, counts, note in rows:
            name = gnames.get(ws.club_group_id, str(ws.club_group_id))
            if note:
                print(f'[=] {note}')
            else:
                detail = ' '.join(f'{k}={v}' for k, v in sorted((counts or {}).items())) or '无变化'
                print(f'[{"+" if args.apply else "计划"}] {name}（ws#{ws.id}）：{detail}')
        print(f'\n[{"已落库" if args.apply else "干跑合计"}] ' + (
            ' '.join(f'{k}={v}' for k, v in sorted(total.items())) or '无任何变化'))
        if args.apply:
            db.session.commit()
            print('[i] 已提交。撤销用治理页否决，或重跑幂等无害。')
        else:
            db.session.rollback()
            print('[i] 干跑未落库；确认后 --apply。')


if __name__ == '__main__':
    main()

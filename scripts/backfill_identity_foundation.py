"""回填脚本：D2 人员层——为存量普通账号幂等建立 provisional Person 与主参与映射。

分类规则（D0 盘点报告人工确认后执行，规格 3.4 / 13.2）：
  - 普通账号：各建 provisional Person（全部 unverified，不从自填姓名/学号建立
    任何 verified 依据）+ person_primary_account 映射
  - 服务号：--service-account <id> 显式标注 account_kind=service，不建虚构自然人、
    不建 Person；未显式确认的疑似系统号（super_admin 无登录审计 / username 命中
    关键词）进人工队列，本轮不 Provisional 化（宁缺勿错）
  - @seed.dev 开发号：--mark-seed-dev-test 时批量标注 account_kind=test 并建
    Person（仅 dev 库；生产库应为 0，缺旗标时进人工队列兜底）
  - 未知/存疑种类：进人工队列文件 log/identity_manual_queue_<batch_id>.txt

幂等：已有 person_id 的 user 跳过（重跑零变更）；account_kind 已是非 standard
的跳过标注；半途状态（有 person 无 primary）修复补齐。

用法（项目根）：
  .venv/bin/python scripts/backfill_identity_foundation.py --dry-run   # 只看计划
  .venv/bin/python scripts/backfill_identity_foundation.py --apply     # 落库
  .venv/bin/python scripts/backfill_identity_foundation.py --apply \
      --service-account 10 --service-account 82 --mark-seed-dev-test  # 分类后全量
  .venv/bin/python scripts/backfill_identity_foundation.py --apply --batch-size 200 --after-id 100
前置：先重跑 scripts/audit_identity_inventory.py 复核基线（本脚本启动时提醒）。
回滚：Person 建错可按 identity_event 的 backfill 批次定位后整批删除（无业务引用，
D2 零 enforcement）；account_kind 标注改回 standard 即可。
"""
import argparse
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import app          # noqa: E402,F401

# 与 audit_identity_inventory.py 同源的疑似系统号线索（弱线索，人工确认才标注）
SERVICE_NAME_HINTS = ("资讯君", "机器人", "系统", "官方", "公告")
SEED_DEV_SUFFIX = '@seed.dev'


def main():
    parser = argparse.ArgumentParser(description='D2 人员层回填（幂等，规格 3.4/13.2）')
    parser.add_argument('--dry-run', action='store_true', help='只输出计划不落库')
    parser.add_argument('--apply', action='store_true', help='执行落库')
    parser.add_argument('--service-account', type=int, action='append', default=[],
                        help='确认的服务号 user id（可重复；标注 service，不建 Person）')
    parser.add_argument('--mark-seed-dev-test', action='store_true',
                        help='@seed.dev 开发号批量标注 test（仅 dev 库使用）')
    parser.add_argument('--batch-size', type=int, default=500, help='单轮处理上限（断点续跑）')
    parser.add_argument('--after-id', type=int, default=0, help='从该 id 之后继续（断点）')
    args = parser.parse_args()
    if args.dry_run and args.apply:
        parser.error('--dry-run 与 --apply 互斥')

    from exts import db
    from models import (IdentityEventModel, IdentityOperationModel, PersonModel,
                        PersonPrimaryAccountModel, UserModel)
    from services.identity import events, person as identity_person

    batch_id = datetime.now().strftime('backfill:%Y%m%d-%H%M%S')
    print(f"[i] 批次 {batch_id}；前置提醒：先重跑 scripts/audit_identity_inventory.py 复核基线")

    with app.app_context():
        # ── 全库一致性异常扫描（两种模式都先跑，异常不阻塞回填但必须报出）──
        orphan_pointer = [u.id for u in UserModel.query
                          .filter(UserModel.person_id.isnot(None)).all()
                          if db.session.get(PersonModel, u.person_id) is None]
        half_primary = [u.id for u in UserModel.query
                        .filter(UserModel.person_id.isnot(None)).all()
                        if db.session.get(PersonPrimaryAccountModel, u.person_id) is None]
        if orphan_pointer:
            print(f"[!] 异常：user.person_id 指向不存在的人员（需人工）: {orphan_pointer[:10]}")
        if half_primary:
            print(f"[i] 半途状态（有 person 无 primary，--apply 将修复）: {len(half_primary)} 个")

        q = (UserModel.query.filter(UserModel.id > args.after_id)
             .order_by(UserModel.id).limit(args.batch_size))
        users = q.all()
        service_ids = set(args.service_account)
        plan = {'provision': [], 'mark_service': [], 'mark_test': [],
                'skip_done': [], 'manual': []}
        for u in users:
            if u.id in service_ids:
                (plan['mark_service'] if u.account_kind != 'service' else plan['skip_done']
                 ).append(u)
                continue
            if u.email and u.email.endswith(SEED_DEV_SUFFIX):
                if args.mark_seed_dev_test:
                    (plan['mark_test'] if u.account_kind != 'test' else plan['skip_done']
                     ).append(u)
                    if not u.person_id:
                        plan['provision'].append(u)
                    continue
                plan['manual'].append((u, 'seed.dev 开发号但未传 --mark-seed-dev-test'))
                continue
            if _suspect_service(u):
                plan['manual'].append((u, '疑似系统号（弱线索命中）未显式分类'))
                continue
            if u.person_id:
                plan['skip_done'].append(u)
            else:
                plan['provision'].append(u)

        # ── 人工队列文件（两种模式都写，供 D0 报告人工确认后下轮 --service-account）──
        os.makedirs('log', exist_ok=True)
        queue_path = f"log/identity_manual_queue_{batch_id.replace(':', '_')}.txt"
        with open(queue_path, 'w', encoding='utf-8') as f:
            f.write(f"# 身份回填人工队列（{batch_id}，弱线索不自动写入）\n")
            for u, why in plan['manual']:
                f.write(f"id={u.id}\t{u.email}\tusername={u.username or '-'}\t"
                        f"role={u.role}\tkind={u.account_kind}\t原因: {why}\n")
        print(f"[{'计划' if args.dry_run else '执行'}] 共扫 {len(users)} 个（after-id={args.after_id}）："
              f"待建 {len(plan['provision'])} / 标 service {len(plan['mark_service'])} / "
              f"标 test {len(plan['mark_test'])} / 已完成跳过 {len(plan['skip_done'])} / "
              f"人工队列 {len(plan['manual'])}（全文 {queue_path}）")

        if args.dry_run:
            for u in plan['provision'][:5]:
                print(f"  [计划] user#{u.id} {u.email} -> provisional Person（unverified）")
            for u in plan['mark_service']:
                print(f"  [计划] user#{u.id} {u.email} -> account_kind=service（不建 Person）")
            for u in plan['mark_test'][:5]:
                print(f"  [计划] user#{u.id} {u.email} -> account_kind=test")
            print('[i] 干跑未落库；确认后 --apply。')
            return

        # ── apply：逐 user 事务（崩溃留干净断点），标注合并一事务 ──
        created = repaired = 0
        last_id = args.after_id
        for u in plan['provision']:
            had_person = bool(u.person_id)
            identity_person.create_provisional(u)
            db.session.commit()
            created += 0 if had_person else 1
            repaired += 1 if had_person else 0
            last_id = max(last_id, u.id)
        for u in plan['mark_service']:
            u.account_kind = 'service'
            last_id = max(last_id, u.id)
        for u in plan['mark_test']:
            u.account_kind = 'test'
            last_id = max(last_id, u.id)
        db.session.commit()

        summary = (f"created={created} repaired={repaired} "
                   f"marked_service={len(plan['mark_service'])} "
                   f"marked_test={len(plan['mark_test'])} "
                   f"skipped={len(plan['skip_done'])} manual={len(plan['manual'])}")
        # 批次留痕（幂等键=batch_id 天然不重放；账本记 run 级摘要，个人行即业务事实）
        op = IdentityOperationModel(
            operation_id=events.new_operation_id(), actor_user_id=0,
            operation_type='person.backfill', idempotency_key=batch_id,
            request_digest=events.request_digest(
                {'after_id': args.after_id, 'service_ids': sorted(service_ids),
                 'seed_dev_test': args.mark_seed_dev_test}),
            state='completed', result_ref=summary)
        db.session.add(op)
        events.record_event(
            'person.backfill', actor_user_id=0, operation_id=op.operation_id,
            target_ids=None, evidence_refs={'operation_id': op.operation_id},
            reason=f'{batch_id} {summary}')
        db.session.commit()
        print(f'[+] 已落库：{summary}')
        print(f"[i] 断点：如需续跑用 --after-id {last_id}；重跑本命令幂等（零变更）。")


def _suspect_service(user):
    """弱线索：username 命中关键词，或 super_admin 且无任何登录审计（同 D0 口径）。"""
    if user.username and any(kw in user.username for kw in SERVICE_NAME_HINTS):
        return True
    if user.role == 'super_admin':
        from models import AuditLog
        return not AuditLog.query.filter_by(user_id=user.id).filter(
            AuditLog.operation.in_(('用户登录', '管理员登录'))).first()
    return False


if __name__ == '__main__':
    main()

"""保存每阶段进度与检查证据；可同时导出本地HTML进度页。"""
import argparse
import html
import json
from datetime import datetime
from pathlib import Path

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('stage')
parser.add_argument('status', choices=['pending', 'in_progress', 'complete', 'local_complete', 'awaiting_server'])
parser.add_argument('--summary', required=True)
parser.add_argument('--checks', default='')
parser.add_argument('--remaining', default='')
parser.add_argument('--html')
args = parser.parse_args()
path = root / 'docs/project-core-progress.json'
data = json.loads(path.read_text())
stage = next(s for s in data['stages'] if s['id'] == args.stage)
stage.update(status=args.status, summary=args.summary, checks=args.checks,
             remaining=args.remaining, updated_at=datetime.now().isoformat(timespec='seconds'))
path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
if args.html:
    labels = {'pending':'待开始','in_progress':'进行中','complete':'完成','local_complete':'本地完成','awaiting_server':'待服务器验证'}
    rows = ''.join('<tr>' + ''.join('<td>'+html.escape(str(v))+'</td>' for v in [
        s['id']+' '+s['name'], labels[s['status']], s.get('summary',''), s.get('checks',''), s.get('remaining','')])+'</tr>' for s in data['stages'])
    target = Path(args.html)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>独立项目重构 · 实施进度</title><style>body{font:15px/1.8 system-ui;color:#25313a;margin:30px}main{max-width:1200px;margin:auto}table{border-collapse:collapse;width:100%;font-size:13px}td,th{border:1px solid #dce1e5;padding:12px;text-align:left;vertical-align:top}th{background:#f5f7f8}.table{overflow:auto}table{min-width:800px}</style><main><h1>独立项目重构 · 实施进度</h1><p>每阶段记录实际改动、检查和剩余工作；不做全量回归。服务器部署和真实数据切换单独记录。</p><div class="table"><table><thead><tr><th>阶段</th><th>状态</th><th>改动</th><th>检查证据</th><th>剩余工作</th></tr></thead><tbody>'+rows+'</tbody></table></div></main></html>')
print(args.stage + ': ' + args.status)

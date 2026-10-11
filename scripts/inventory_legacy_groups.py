"""离线列出前两代小组接口与仓库调用方；不导入 app/config，不连接数据库。

--sql 仅打印历史数据数量盘点 SELECT，执行前需另行确认目标库及业务窗口。
"""
import argparse
import ast
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
MODELS = {'GroupModel', 'CourseGroup', 'CourseGroupMember', 'CourseGroupJoinRequest',
          'Task', 'TaskAssignee', 'TaskSubmission', 'TaskSubmissionAttachment'}
MODULES = ('user', 'course_group', 'task', 'information', 'learningProgress', 'medal')


def inventory(frontend):
    routes = []
    for module in MODULES:
        path = ROOT / 'blueprints' / (module + '.py')
        tree = ast.parse(path.read_text())
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            names = {item.id for item in ast.walk(node) if isinstance(item, ast.Name)} & MODELS
            if not names and module not in ('course_group', 'task'):
                continue
            for dec in node.decorator_list:
                if not isinstance(dec, ast.Call) or not isinstance(dec.func, ast.Attribute) or dec.func.attr != 'route':
                    continue
                route = ast.literal_eval(dec.args[0]) if dec.args else ''
                methods = next((ast.literal_eval(k.value) for k in dec.keywords if k.arg == 'methods'), ['GET'])
                if methods == ['OPTIONS']:
                    continue
                routes.append(dict(endpoint=f'{module}.{node.name}', route=route, methods=methods,
                                   models=sorted(names), file=str(path.relative_to(ROOT)), line=node.lineno))
    consumers = []
    pattern = re.compile(r'(?<![\w/])(?:/user/group|/course-groups|/tasks(?:[\x27"/`]|$)|/learningProgress/group|/information/(?:task|homework|notice|leave))')
    for app in ('user','admin'):
        base = frontend / 'apps' / app / 'src'
        if not base.exists():
            continue
        for path in sorted(base.rglob('*')):
            if path.suffix not in ('.js','.ts','.vue'):
                continue
            for line, text in enumerate(path.read_text().splitlines(),1):
                if pattern.search(text):
                    consumers.append(dict(file=str(path.relative_to(frontend)),line=line,call=text.strip()))
    models = ast.parse((ROOT/'models.py').read_text())
    tables = {}
    for node in models.body:
        if isinstance(node,ast.ClassDef) and node.name in MODELS | {'InformationModel'}:
            for child in node.body:
                if isinstance(child,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='__tablename__' for t in child.targets):
                    tables[node.name] = ast.literal_eval(child.value)
    return dict(database_examined=False, routes=routes, consumers=consumers, tables=tables,
                limitations=['静态清单不代表生产调用流量或数据库存量；动态 URL 和仓库外客户端需另查。',
                             '社团和工作区不在本清单内；不根据数字 ID 自动关联 CampGroup。'])


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--frontend',type=Path,default=ROOT.parent/'BME_platform_frontend')
    parser.add_argument('--summary',action='store_true')
    parser.add_argument('--sql',action='store_true')
    args=parser.parse_args()
    result=inventory(args.frontend)
    if args.sql:
        print('-- 只读盘点模板；未执行。information 含多种旧业务，按 type 区分。')
        for model,table in result['tables'].items():
            print(f"SELECT '{model}' AS model, COUNT(*) AS rows_count FROM `{table}`;")
        print('SELECT type, COUNT(*) AS rows_count FROM information GROUP BY type;')
    elif args.summary:
        print(json.dumps(dict(database_examined=False,route_count=len(result['routes']),
            consumers=result['consumers'],tables=result['tables'],limitations=result['limitations']),ensure_ascii=False,indent=2))
    else:
        print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()

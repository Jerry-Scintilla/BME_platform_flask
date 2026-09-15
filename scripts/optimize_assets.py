"""一次性资产优化脚本（dev 机跑，产物入 scripts/migrate/assets/ 供 migrate_32 使用，不进生产流程）。

功能：
1. 勋章图：前端 apps/user/public/medals/*.png -> assets/medals/{name}.webp（512x512 contain）
2. 轮播底图：public/ 下 3 张 banner PNG -> assets/banners/{stem}.webp（1600x800 <=500KB）
3. favicon：public/New_Logo1.png -> assets/favicon-32.png（32x32，替换 3.2MB 原图当 favicon 的浪费）
4. --dry-run：只打印前端死资产删除清单（删除动作在前端仓手动/另行执行）

用法（后端仓根）：uv run python scripts/optimize_assets.py [--dry-run]
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import imaging  # noqa: E402
from PIL import Image  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_ROOT = os.path.join(HERE, 'migrate', 'assets')
# 前端仓相对位置（dev 机两仓同级；不同级时用 --frontend 指定）
DEFAULT_FRONTEND = os.path.join('..', '..', 'BME_platform_frontend', 'apps', 'user', 'public')

BANNER_SOURCES = ['2026秋季学期营.png', '大模型服务中心.png', '3D打印农场.png']

# 前端死资产清单（零引用 / 兜底期满后删）：dry-run 打印，删除在前端仓执行
DEAD_ASSETS = [
    'Logo_NewYear.png（311KB，public 死拷贝，assets/ 里有正主）',
    'Logo_plain.png（200KB，public 死拷贝）',
    'image.png（src/assets，零引用）',
    '2026暑期训练营.png（2.03MB，上一期遗留死资产）',
    'New_Logo1.png（3.2MB，favicon 改用压缩小图后删除）',
    'medals/（38MB，migrate_32 全量迁移且生产验证 Medal_Image 非空后删）',
    '2026秋季学期营.png / 大模型服务中心.png / 3D打印农场.png（banner 入库后删）',
]


def transcode_to(src, dst, fn):
    with open(src, 'rb') as f:
        data = fn(f)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(dst, 'wb') as f:
        f.write(data)
    return len(data)


def favicon_bytes(stream):
    img = imaging._load(stream)
    img = img.convert('RGBA').resize((32, 32), Image.LANCZOS)
    buf = __import__('io').BytesIO()
    img.save(buf, format='PNG')
    return buf.getvalue()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--frontend', default=DEFAULT_FRONTEND, help='apps/user/public 目录路径')
    parser.add_argument('--dry-run', action='store_true', help='只打印死资产清单，不做转码')
    args = parser.parse_args()

    if args.dry_run:
        print('== 前端死资产删除清单（在前端仓执行，兜底期满后删）==')
        for item in DEAD_ASSETS:
            print(f'  - {item}')
        return

    pub = os.path.abspath(args.frontend)
    if not os.path.isdir(pub):
        print(f'[FAIL] 前端 public 目录不存在：{pub}（用 --frontend 指定）')
        sys.exit(1)

    # 1) 勋章
    medals_dir = os.path.join(pub, 'medals')
    count = 0
    for name in sorted(os.listdir(medals_dir)):
        if not name.lower().endswith('.png'):
            continue
        dst = os.path.join(OUT_ROOT, 'medals', os.path.splitext(name)[0] + '.webp')
        size = transcode_to(os.path.join(medals_dir, name), dst, imaging.medal_bytes)
        count += 1
        print(f'[+] medals/{name} -> {os.path.basename(dst)} ({size // 1024}KB)')
    print(f"[done] 勋章 {count} 张")

    # 2) 轮播底图
    for name in BANNER_SOURCES:
        src = os.path.join(pub, name)
        if not os.path.isfile(src):
            print(f"[WARN] 缺 {name}，跳过（对应 banner 帧将无图，管理页可后补）")
            continue
        dst = os.path.join(OUT_ROOT, 'banners', os.path.splitext(name)[0] + '.webp')
        size = transcode_to(src, dst, imaging.banner_bytes)
        print(f'[+] {name} -> banners/{os.path.basename(dst)} ({size // 1024}KB，上限 500KB)')

    # 3) favicon
    logo = os.path.join(pub, 'New_Logo1.png')
    if os.path.isfile(logo):
        size = transcode_to(logo, os.path.join(OUT_ROOT, 'favicon-32.png'), favicon_bytes)
        print(f'[+] favicon-32.png ({size // 1024}KB)')
    else:
        print('[WARN] 未找到 New_Logo1.png，favicon 未生成')

    print(f'\n[done] 产物在 {OUT_ROOT}（随代码提交，migrate_32 从这里上传 storage）')


if __name__ == '__main__':
    main()

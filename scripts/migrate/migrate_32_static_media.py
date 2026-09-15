"""迁移 32：静态资源体系入 storage（2026-09-15，幂等）。

步骤（每步独立幂等，可重复运行）：
1. medal 表加 image 列（勋章图 '/media/medals/...'）
2. 建 banner 表（首页轮播 DB 化）
3. seed banner_management 权限并授权 super_admin（migrate_05 模式）
4. 头像搬入 storage：默认转 WebP 256x256，坏图回退原样字节拷贝；avatar_url 改存 '/media/...'
5. 导生名片照搬入 storage（保比例 WebP），CampMentorProfile.photo 改存 '/media/...'
6. 勋章图：scripts/migrate/assets/medals/{Medal_Name}.webp 上传并回填 image 列（稳定 key）
7. banner 空表时 seed 3 帧（图来自 scripts/migrate/assets/banners/；is_camp_frame=0 跟随 09-14 撤动态帧决定）
8. DROP TABLE IF EXISTS home_cover（homeCover 蓝图已删，data/homeCover 本就为空）
9. 老目录 {DATA_ROOT}/avatars、mentor_photos 重命名 *.migrated-202609（不删，验证后人工清理）

依赖：先在 dev 机跑 scripts/optimize_assets.py 产出 scripts/migrate/assets/（无该目录时第 6/7 步跳过并提示）。
用法（项目根）：python scripts/migrate/migrate_32_static_media.py
注意：读取 .env 的 STORAGE_BACKEND —— 迁到哪个后端，服务就配哪个后端（换后端需重跑本脚本灌入）。
回滚：恢复 DB 备份 + 恢复 data 目录备份；老目录重命名可用 mv 改回。
"""
import io
import os
import re
import sys
import uuid

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from app import app          # noqa: E402  (import 即装配 storage/config)
from exts import db          # noqa: E402
from storage import storage  # noqa: E402
import imaging               # noqa: E402
from sqlalchemy import create_engine, text, inspect  # noqa: E402

import config                # noqa: E402
from models import (         # noqa: E402
    UserModel, CampMentorProfile, MedalModel, BannerModel,
    PermissionModel, UserPermissionModel,
)

ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'assets')
MIGRATED_SUFFIX = '.migrated-202609'

engine = create_engine(config.SQLALCHEMY_DATABASE_URI)
insp = inspect(engine)


def _put(key, data, content_type="image/webp"):
    storage.put_object(key, io.BytesIO(data), len(data), content_type)


def _migrate_one_file(src_path, transcode, fallback_ext, key_dir):
    """单文件迁移：默认转码 WebP；坏图回退原样字节（可用性优先）。返回 object key 或 None。"""
    if not os.path.isfile(src_path):
        return None, 'missing'
    with open(src_path, 'rb') as f:
        try:
            data, ext = transcode(f), '.webp'
        except imaging.ImageError:
            f.seek(0)
            data, ext = f.read(), fallback_ext
    key = f"{key_dir}/{uuid.uuid4().hex}{ext}"
    _put(key, data, "image/webp" if ext == '.webp' else "application/octet-stream")
    return '/' + key, 'ok'


def step_1_columns():
    with engine.connect() as conn:
        cols = [c['name'] for c in insp.get_columns('medal')]
        if 'image' in cols:
            print("[=] medal.image 已存在")
        else:
            conn.execute(text("ALTER TABLE medal ADD COLUMN image VARCHAR(200) NULL"))
            conn.commit()
            print("[+] medal.image 已加列")


def step_2_banner_table():
    with engine.connect() as conn:
        if insp.has_table('banner'):
            print("[=] banner 表已存在")
            return
        conn.execute(text("""
            CREATE TABLE banner (
                id INT AUTO_INCREMENT PRIMARY KEY,
                sort_order INT NOT NULL,
                title VARCHAR(100) NOT NULL,
                description VARCHAR(200),
                image_key VARCHAR(300) NOT NULL,
                link_type VARCHAR(20) NOT NULL DEFAULT 'route',
                link_value VARCHAR(300),
                is_camp_frame TINYINT(1) NOT NULL DEFAULT 0,
                visible TINYINT(1) NOT NULL DEFAULT 1,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                UNIQUE KEY uq_banner_sort (sort_order)
            ) CHARSET=utf8mb4
        """))
        conn.commit()
        print("[+] banner 表已创建")


def step_3_permission():
    with app.app_context():
        perm = PermissionModel.query.filter_by(name='banner_management').first()
        if perm:
            print("[=] banner_management 权限已存在")
        else:
            perm = PermissionModel(name='banner_management', description='首页轮播管理')
            db.session.add(perm)
            db.session.commit()
            print("[+] banner_management 权限已创建")
        granted = 0
        for u in UserModel.query.filter_by(role='super_admin').all():
            if not UserPermissionModel.query.filter_by(user_id=u.id, permission_id=perm.id).first():
                db.session.add(UserPermissionModel(user_id=u.id, permission_id=perm.id))
                granted += 1
        db.session.commit()
        print(f"[+] 授权 super_admin {granted} 条新增")


def step_4_avatars():
    with app.app_context():
        rows = UserModel.query.filter(
            UserModel.avatar_url.isnot(None),
            UserModel.avatar_url != '',
            ~UserModel.avatar_url.like('/media/%'),
        ).all()
        if not rows:
            print("[=] 无待迁移头像")
            return
        ok = warn = 0
        for u in rows:
            src = os.path.join(config.DATA_ROOT, 'avatars', u.avatar_url)
            url, status = _migrate_one_file(
                src, imaging.avatar_bytes,
                os.path.splitext(u.avatar_url)[1] or '.png',
                f"media/avatars/{u.id}")
            if status == 'missing':
                print(f"[WARN] 头像文件缺失 user={u.id} value={u.avatar_url}（跳过，保持旧值）")
                warn += 1
                continue
            u.avatar_url = url
            ok += 1
        db.session.commit()
        print(f"[+] 头像迁移 {ok} 条（WARN {warn}）")


def step_5_mentor_photos():
    with app.app_context():
        rows = CampMentorProfile.query.filter(
            CampMentorProfile.photo.isnot(None),
            CampMentorProfile.photo != '',
            ~CampMentorProfile.photo.like('/media/%'),
        ).all()
        if not rows:
            print("[=] 无待迁移导生照片")
            return
        ok = warn = 0
        for p in rows:
            src = os.path.join(config.DATA_ROOT, 'mentor_photos', p.photo)
            url, status = _migrate_one_file(
                src, imaging.mentor_photo_bytes,
                os.path.splitext(p.photo)[1] or '.jpg',
                f"media/mentors/{p.camp_session_id}/{p.user_id}")
            if status == 'missing':
                print(f"[WARN] 导生照缺失 profile={p.id} value={p.photo}（跳过，保持旧值）")
                warn += 1
                continue
            p.photo = url
            ok += 1
        db.session.commit()
        print(f"[+] 导生照迁移 {ok} 条（WARN {warn}）")


def _safe_name(name):
    return re.sub(r"[^0-9A-Za-z_-]+", "", name or "")[:40] or "medal"


# 中文名勋章（seed 数据）到英文名图文件的别名；未命中别名的回填 Default 占位图（管理页可后换）
MEDAL_NAME_ALIASES = {
    '初学者': 'NewComer',
    '先驱者': 'PioneerX',
    '学霸': 'EliteYellow',
}


def step_6_medal_images():
    medals_dir = os.path.join(ASSETS, 'medals')
    if not os.path.isdir(medals_dir):
        print("[!] 缺 scripts/migrate/assets/medals/ —— 先在 dev 机跑 scripts/optimize_assets.py，本步跳过")
        return
    with app.app_context():
        # Default 兜底图（前端 getMedalImage 的空值兜底指向它）
        default_src = os.path.join(medals_dir, 'Default.webp')
        if os.path.isfile(default_src):
            with open(default_src, 'rb') as f:
                _put("media/medals/Default.webp", f.read())
            print("[+] media/medals/Default.webp 已确保")
        else:
            print("[WARN] assets/medals/Default.webp 缺失")
        # 按 Medal_Name（含别名）匹配回填；无图的给 Default 占位，杜绝裂图
        n = miss = 0
        for m in MedalModel.query.filter(MedalModel.image.is_(None)).all():
            stem = m.medal_name if os.path.isfile(
                os.path.join(medals_dir, f"{m.medal_name}.webp")) else MEDAL_NAME_ALIASES.get(m.medal_name)
            src = os.path.join(medals_dir, f"{stem}.webp") if stem else None
            if not src or not os.path.isfile(src):
                print(f"[WARN] 未匹配勋章图 Medal_Name={m.medal_name}，回填 Default 占位（管理页可后换）")
                m.image = '/media/medals/Default.webp'
                miss += 1
                continue
            key = f"media/medals/{m.id}_{_safe_name(m.medal_name)}.webp"
            with open(src, 'rb') as f:
                _put(key, f.read())
            m.image = '/' + key
            n += 1
        db.session.commit()
        print(f"[+] 勋章图回填 {n} 条（Default 占位 {miss}）")


def step_7_banner_seed():
    banners_dir = os.path.join(ASSETS, 'banners')
    with app.app_context():
        if BannerModel.query.first():
            print("[=] banner 已有数据，跳过 seed")
            return
    if not os.path.isdir(banners_dir):
        print("[!] 缺 scripts/migrate/assets/banners/ —— 先在 dev 机跑 scripts/optimize_assets.py，本步跳过")
        return
    seeds = [
        # (stem, title, description, link_type, link_value, is_camp_frame)
        ('2026秋季学期营', '营期中心', '查看营期与报名', 'route', '/camp', 0),
        ('大模型服务中心', '大模型服务中心', '大模型 API 接口平台', 'route', '/ai-service', 0),
        ('3D打印农场', '3D打印农场', '在线预约，一站式 3D 打印服务', 'external', '/3dfarm/', 0),
    ]
    with app.app_context():
        for idx, (stem, title, desc, lt, lv, camp_flag) in enumerate(seeds, start=1):
            src = os.path.join(banners_dir, f"{stem}.webp")
            if not os.path.isfile(src):
                print(f"[WARN] 缺 {stem}.webp，该帧跳过（管理页可后建）")
                continue
            key = f"media/banners/seed/{stem}.webp"
            with open(src, 'rb') as f:
                _put(key, f.read())
            db.session.add(BannerModel(sort_order=idx, title=title, description=desc,
                                       image_key='/' + key, link_type=lt, link_value=lv,
                                       is_camp_frame=bool(camp_flag), visible=True))
        db.session.commit()
        print("[+] banner 已 seed 3 帧（is_camp_frame=0，跟随 09-14 撤动态帧决定）")


def step_8_drop_home_cover():
    with engine.connect() as conn:
        conn.execute(text("DROP TABLE IF EXISTS home_cover"))
        conn.commit()
    print("[+] home_cover 表已 DROP（蓝图死代码已删，data/homeCover 本就为空）")


def step_9_rename_old_dirs():
    for name in ('avatars', 'mentor_photos'):
        src = os.path.join(config.DATA_ROOT, name)
        dst = src + MIGRATED_SUFFIX
        if os.path.isdir(src) and not os.path.isdir(dst):
            os.rename(src, dst)
            print(f"[+] {src} -> {os.path.basename(dst)}（旧静态兜底随之失效属预期，人工验证后可删）")
        elif os.path.isdir(dst):
            print(f"[=] {name} 已重命名过")
        else:
            print(f"[=] 无 {name} 目录")


if __name__ == '__main__':
    print(f"[i] STORAGE_BACKEND={storage.backend} | DATA_ROOT={config.DATA_ROOT}")
    step_1_columns()
    step_2_banner_table()
    step_3_permission()
    step_4_avatars()
    step_5_mentor_photos()
    step_6_medal_images()
    step_7_banner_seed()
    step_8_drop_home_cover()
    step_9_rename_old_dirs()
    print("[done] migrate_32 完成")

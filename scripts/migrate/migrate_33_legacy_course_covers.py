"""迁移 33：老课程封面入库（2026-09-15 cutover 当晚补，幂等）。

migrate_32 迁了头像/导生照/勋章/banner，漏了 course 表旧封面：老值是裸文件名
（如 '4.jpg'），实际文件在 {DATA_ROOT}/cover/，新前端按 /media/ 解析 → 破图。
本脚本把 {DATA_ROOT}/cover/<cover> 按上传管线同规格
（imaging.course_cover_pair → webp 母版+缩略成对）写入对象存储，
course.cover 改存 '/media/course-covers/{cid}/{uid32}.webp'。

幂等：cover 已是 '/media/...' 的课程跳过；旧文件缺失告警跳过（保留旧值）。
用法（项目根）：.venv/bin/python scripts/migrate/migrate_33_legacy_course_covers.py
"""
import io
import os
import sys
import uuid

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from app import app          # noqa: E402
from exts import db          # noqa: E402
from storage import storage  # noqa: E402
import imaging               # noqa: E402
import config                # noqa: E402
from models import CourseModel  # noqa: E402
from blueprints.media import media_url  # noqa: E402


def main():
    cover_dir = os.path.join(config.DATA_ROOT, 'cover')
    with app.app_context():
        courses = CourseModel.query.filter(
            CourseModel.cover.isnot(None), CourseModel.cover != ''
        ).all()
        migrated, skipped = [], []
        for c in courses:
            if c.cover.startswith('/media/'):
                skipped.append((c.id, c.cover, '已是新格式'))
                continue
            src = os.path.join(cover_dir, c.cover)
            if not os.path.isfile(src):
                skipped.append((c.id, c.cover, f'旧文件缺失: {src}'))
                continue
            with open(src, 'rb') as f:
                master, thumb = imaging.course_cover_pair(f)
            uid = uuid.uuid4().hex
            master_key = f"media/course-covers/{c.id}/{uid}.webp"
            thumb_key = f"media/course-covers/{c.id}/{uid}_thumb.webp"
            storage.put_object(master_key, io.BytesIO(master), len(master), "image/webp")
            storage.put_object(thumb_key, io.BytesIO(thumb), len(thumb), "image/webp")
            c.cover = media_url(master_key)
            migrated.append((c.id, c.title, c.cover))
        db.session.commit()

    print(f"✅ 迁移完成 {len(migrated)} 门：")
    for cid, title, cover in migrated:
        print(f"   id={cid} {title} → {cover}")
    if skipped:
        print(f"⏭ 跳过 {len(skipped)} 门：")
        for cid, cover, why in skipped:
            print(f"   id={cid} cover={cover}（{why}）")


if __name__ == '__main__':
    main()

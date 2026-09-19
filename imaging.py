"""图片转码工具（Pillow，全内存 BytesIO，不落业务磁盘）。

所有上传 / 迁移图片的统一入口：入参是可读二进制流（Werkzeug FileStorage.stream
或迁移脚本打开的文件句柄），出参 bytes（调用方配 len() 喂 storage.put_object）。
统一输出 WebP；源格式 Pillow 能解即可（jpg/png/webp/gif/bmp 等）。

规格（与前端展示位对齐，详见 docs/deploy/静态资源迁移指南-202609.md）：
- 头像   avatar_bytes        256x256 中心方形裁切 q82（展示位 <=64px，3x 余量）
- 导生照 mentor_photo_bytes  最长边 800 保比例 q82（名片位 ~300px）
- 勋章   medal_bytes         512x512 contain 到画布 q85（勋章墙大图位）
- 轮播   banner_bytes        1600x800 cover 裁切，q80 起 ≤500KB 逐档降（运营规范）
- 封面   course_cover_pair   3:4 中心裁切，母版 <=1200x1600 + 缩略 600x800 q82
- 广场封面 showcase_cover_pair    16:9 中心裁切，母版 <=1600x900 + 缩略 640x360 q82
- 广场图集 showcase_gallery_bytes 最长边 1600 保比例 q82（不裁切）
- 文章封面 article_cover_pair     同 16:9 规格（与广场封面共用实现）
"""
import io

from PIL import Image, UnidentifiedImageError

Image.MAX_IMAGE_PIXELS = 80_000_000  # 解码炸弹防线（默认 89M，收紧一点）

WEBP_QUALITY = 82
MEDAL_QUALITY = 85
BANNER_QUALITY = 80
BANNER_MIN_QUALITY = 60
BANNER_MAX_BYTES = 500 * 1024        # docs/首页banner-运营规范.md

AVATAR_SIZE = 256
MENTOR_PHOTO_MAX_SIDE = 800
MEDAL_SIZE = 512
BANNER_SIZE = (1600, 800)
COVER_MASTER_MAX = (1200, 1600)
COVER_THUMB = (600, 800)
SHOWCASE_COVER_MASTER_MAX = (1600, 900)
SHOWCASE_COVER_THUMB = (640, 360)
SHOWCASE_GALLERY_MAX_SIDE = 1600

# 带透明通道的源（勋章多为 PNG）压 WebP 时保留 alpha；其余统一压到 RGB（省体积）
def _has_alpha(img):
    return img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info)


class ImageError(ValueError):
    """源文件不是可解码图片（Pillow UnidentifiedImageError 的业务包装，调用方回 400）。"""


def _load(stream):
    """解码 + verify 防伪装炸弹，返回重新 open 的 Image（verify 后原图不可复用）。"""
    data = stream.read()
    if not data:
        raise ImageError("empty image data")
    try:
        with Image.open(io.BytesIO(data)) as probe:
            probe.verify()
        img = Image.open(io.BytesIO(data))
        img.load()
        return img
    except (UnidentifiedImageError, OSError, ValueError) as e:
        raise ImageError(f"not a valid image: {e}") from e


def _to_webp(img, quality=WEBP_QUALITY):
    buf = io.BytesIO()
    img.save(buf, format="WEBP", quality=quality, method=4)
    return buf.getvalue()


def _center_crop(img, ratio_w, ratio_h):
    """按目标宽高比中心裁切（cover 语义）。"""
    w, h = img.size
    target = ratio_w / ratio_h
    cur = w / h
    if cur > target:                       # 太宽，裁左右
        new_w = int(h * target)
        left = (w - new_w) // 2
        img = img.crop((left, 0, left + new_w, h))
    elif cur < target:                     # 太高，裁上下
        new_h = int(w / target)
        top = (h - new_h) // 2
        img = img.crop((0, top, w, top + new_h))
    return img


def _shrink_only(img, max_size):
    """缩到 <=max_size（只缩不放，LANCZOS）。"""
    img.thumbnail(max_size, Image.LANCZOS)
    return img


def avatar_bytes(stream) -> bytes:
    """头像：中心方形裁切 -> 256x256 WebP。"""
    img = _load(stream)
    img = _center_crop(img, 1, 1).resize((AVATAR_SIZE, AVATAR_SIZE), Image.LANCZOS)
    if not _has_alpha(img):
        img = img.convert("RGB")
    return _to_webp(img)


def mentor_photo_bytes(stream) -> bytes:
    """导生名片照：保比例缩到最长边 800 WebP（不裁切）。"""
    img = _load(stream)
    img = _shrink_only(img, (MENTOR_PHOTO_MAX_SIDE, MENTOR_PHOTO_MAX_SIDE))
    if not _has_alpha(img):
        img = img.convert("RGB")
    return _to_webp(img)


def medal_bytes(stream) -> bytes:
    """勋章：contain 到 512x512 画布（保比例、不拉伸、透明补边）WebP q85。"""
    img = _load(stream)
    img.thumbnail((MEDAL_SIZE, MEDAL_SIZE), Image.LANCZOS)
    if not _has_alpha(img):
        img = img.convert("RGB")
        return _to_webp(img, MEDAL_QUALITY)
    canvas = Image.new("RGBA", (MEDAL_SIZE, MEDAL_SIZE), (0, 0, 0, 0))
    canvas.paste(img, ((MEDAL_SIZE - img.width) // 2, (MEDAL_SIZE - img.height) // 2))
    return _to_webp(canvas, MEDAL_QUALITY)


def banner_bytes(stream) -> bytes:
    """轮播底图：cover 裁 2:1 -> 1600x800，q80 起逐档降到 <=500KB。"""
    img = _load(stream)
    img = _center_crop(img, 2, 1).resize(BANNER_SIZE, Image.LANCZOS)
    if _has_alpha(img):
        img = img.convert("RGBA").convert("RGB")   # 轮播无透明需求
    else:
        img = img.convert("RGB")
    for q in range(BANNER_QUALITY, BANNER_MIN_QUALITY - 1, -5):
        data = _to_webp(img, q)
        if len(data) <= BANNER_MAX_BYTES:
            return data
    return data                              # 最低档仍超就放行（图片内容优先）


def course_cover_pair(stream) -> tuple[bytes, bytes]:
    """课程封面：3:4 中心裁切 -> (母版 <=1200x1600, 缩略 600x800)，均 WebP q82。"""
    img = _load(stream)
    img = _center_crop(img, 3, 4)
    master = _shrink_only(img.copy(), COVER_MASTER_MAX)
    if not _has_alpha(master):
        master = master.convert("RGB")
    thumb = img.resize(COVER_THUMB, Image.LANCZOS)
    if not _has_alpha(thumb):
        thumb = thumb.convert("RGB")
    return _to_webp(master), _to_webp(thumb)


def _cover_pair_169(stream, master_max, thumb_size) -> tuple[bytes, bytes]:
    """16:9 封面成对转码的共用实现（广场封面/文章封面同规格）。"""
    img = _load(stream)
    img = _center_crop(img, 16, 9)
    master = _shrink_only(img.copy(), master_max)
    if not _has_alpha(master):
        master = master.convert("RGB")
    thumb = img.resize(thumb_size, Image.LANCZOS)
    if not _has_alpha(thumb):
        thumb = thumb.convert("RGB")
    return _to_webp(master), _to_webp(thumb)


def showcase_cover_pair(stream) -> tuple[bytes, bytes]:
    """XLAB 项目封面：16:9 中心裁切 -> (母版 <=1600x900, 缩略 640x360)，均 WebP q82。"""
    return _cover_pair_169(stream, SHOWCASE_COVER_MASTER_MAX, SHOWCASE_COVER_THUMB)


def article_cover_pair(stream) -> tuple[bytes, bytes]:
    """文章 v2 封面：与广场封面同规格 16:9（母版 <=1600x900 + 缩略 640x360）WebP q82。"""
    return _cover_pair_169(stream, SHOWCASE_COVER_MASTER_MAX, SHOWCASE_COVER_THUMB)


def showcase_gallery_bytes(stream) -> bytes:
    """XLAB 项目图集：保比例缩到最长边 1600 WebP q82（不裁切，画廊原图语义）。"""
    img = _load(stream)
    img = _shrink_only(img, (SHOWCASE_GALLERY_MAX_SIDE, SHOWCASE_GALLERY_MAX_SIDE))
    if not _has_alpha(img):
        img = img.convert("RGB")
    return _to_webp(img)

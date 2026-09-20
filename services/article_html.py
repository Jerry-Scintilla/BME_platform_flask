"""官方富文本推文：HTML 导入 / 清洗 / 图片转存服务。

方案出处：docs/计划/官方富文本推文-调整方案.md（2026-09-20 v1.0，§10-§12）。

定位与边界：
- 唯一安全边界在服务端本模块。浏览器编辑器、管理员身份、前端处理都不是安全边界；
  每次保存草稿/发布/编辑都重新走 clean_for_save（幂等），数据库只存清洗结果。
- 编辑器（Jodit）只负责编辑体验，未来替换编辑器不影响本模块与数据模型。

管道顺序（导入与保存一致）：
  1) normalize_html        剪贴板外壳剥离 + 图片懒加载地址归一化（data-src -> src）
  2) transfer_images       外部/内嵌图片转存为平台对象存储地址；失败的替换为占位块
  3) sanitize_article_html nh3 白名单清洗（标签/属性/URL/CSS 属性与值级规则）

关键常量 FAILED_IMAGE_MARK：转存失败占位块的标识文本，发布校验发现即阻止发布。
"""
import base64
import binascii
import ipaddress
import io
import os
import re
import socket
import uuid

import nh3
import requests

import imaging
from storage import storage

# ── 长度/数量上限（方案 §5.3，可调整）──
RAW_HTML_MAX_BYTES = 3 * 1024 * 1024        # 原始导入 HTML 上限 3MB
CLEAN_HTML_MAX_BYTES = 2 * 1024 * 1024      # 清洗后 HTML 上限 2MB
IMAGES_MAX = 100                            # 单篇正文图片上限
IMAGE_MAX_BYTES = 10 * 1024 * 1024          # 单张原始图片上限 10MB

# 远程图片抓取（SSRF 防护参数，方案 §12.3）
FETCH_TIMEOUT = (5, 15)                     # (连接, 读取) 秒
FETCH_MAX_REDIRECTS = 3
FETCH_USER_AGENT = "BME-Platform-ArticleImport/1.0"
# 首版只允许抓 HTTPS 图源；确有存量 http 图源时通过环境变量放开
ALLOW_HTTP_IMAGE_FETCH = os.getenv("ARTICLE_HTML_FETCH_HTTP", "false").lower() == "true"

# 转存失败占位块标识（发布校验按它在正文中的出现阻止发布）
FAILED_IMAGE_MARK = "[图片转存失败]"

# ── 标签白名单（方案 §11.2）──
ALLOWED_TAGS = frozenset({
    "section", "div", "p", "span",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "strong", "b", "em", "i", "u", "s", "del",
    "blockquote", "ul", "ol", "li",
    "table", "thead", "tbody", "tfoot", "tr", "th", "td", "caption",
    "colgroup", "col",
    "a", "img", "br", "hr",
    "figure", "figcaption",
})

# 连内容一起删除的标签（脚本/交互/替换型元素，保留内容反而危险或无意义）
CLEAN_CONTENT_TAGS = frozenset({
    "script", "style", "iframe", "object", "embed",
    "form", "input", "button", "textarea", "select", "option",
    "video", "audio", "source", "track",
    "canvas", "svg", "math",
    "meta", "link", "base", "template", "title", "noscript",
})

# ── 属性白名单（方案 §11.3）：'*' 通用 + 按标签 ──
ALLOWED_ATTRIBUTES = {
    "*": {"style", "title", "dir", "lang"},
    "a": {"href", "target", "style", "title", "dir", "lang"},
    "img": {"src", "alt", "title", "width", "height", "style", "dir", "lang"},
    "td": {"colspan", "rowspan", "scope", "style", "title", "dir", "lang"},
    "th": {"colspan", "rowspan", "scope", "style", "title", "dir", "lang"},
}

# href 允许的协议；img src 由 _attribute_filter 强制为平台 /media/ 相对地址
ALLOWED_URL_SCHEMES = frozenset({"https", "http", "mailto"})

# ── CSS 属性白名单（方案 §11.5）──
CSS_PROPERTIES = frozenset({
    "display", "box-sizing",
    "width", "min-width", "max-width",
    "height", "min-height", "max-height",
    "margin", "margin-top", "margin-right", "margin-bottom", "margin-left",
    "padding", "padding-top", "padding-right", "padding-bottom", "padding-left",
    "border", "border-width", "border-style", "border-color", "border-radius",
    "background", "background-color",
    "color",
    "font-family", "font-size", "font-weight", "font-style",
    "line-height", "letter-spacing",
    "text-align", "text-decoration", "text-indent",
    "vertical-align",
    "white-space", "word-break", "overflow-wrap",
    "flex", "flex-direction", "flex-wrap", "flex-grow", "flex-shrink", "flex-basis",
    "align-items", "align-content", "align-self",
    "justify-content", "justify-items", "justify-self",
    "gap", "row-gap", "column-gap",
    "transform", "transform-origin",
    "opacity",
    "overflow", "overflow-x", "overflow-y",
    "float", "clear",
})

# 一律整条删除的属性（position/z-index 的 fixed/sticky/absolute 与不受控层叠均不收）
CSS_DROP_PROPERTIES = frozenset({"position", "z-index", "top", "left", "right", "bottom"})

# CSS 值里的危险片段（url()/expression/@import/行为绑定等）
_CSS_VALUE_DANGER = re.compile(r"url\s*\(|expression\s*\(|@|behavior|javascript\s*:", re.IGNORECASE)

# 数值上限（防 transform/尺寸/负边距撑破页面，方案 §11.5「值级上限」）。
# 先摘掉 #hex 颜色再扫数字：否则 #444444 会被当成 444444 误杀（09-20 修复，
# 曾导致灰黑文字色 #333333/#666666 等大批量被清）。
_CSS_NUM_MAX = 10000.0
_CSS_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")
_HEX_COLOR_RE = re.compile(r"#[0-9a-fA-F]{3,8}")

_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(r"""([a-zA-Z_][a-zA-Z0-9_-]*)\s*=\s*("([^"]*)"|'([^']*)')""")

# 懒加载候选属性（微信 data-src 真图、src 常是 1px 占位）
_LAZY_SRC_ATTRS = ("data-src", "data-original", "data-lazy-src")


class HtmlImportError(ValueError):
    """导入/清洗阶段的业务错误（调用方回 400，消息可直接展示给运营）。"""


def detect_source(html, hint=None):
    """弱特征来源识别（仅用于兼容处理与报告，不作安全信任依据，方案 §10.2）。"""
    if hint in ("xiumi", "wechat", "web", "unknown"):
        return hint
    low = (html or "").lower()
    if "xiumi.us" in low:
        return "xiumi"
    if "mmbiz.qpic.cn" in low or "data-src=" in low or "data-ratio=" in low:
        return "wechat"
    if "<" in low:
        return "web"
    return "unknown"


def normalize_html(raw):
    """DOM 归一化（清洗前）：剥剪贴板外壳 + 图片懒加载地址归一化（方案 §10.3）。

    - html/head/body 外壳、注释、编辑器临时标记交给 nh3（html5ever 规范解析重序列化）；
      此处只把 <head> 内会产生文本泄漏的元素提前交给 CLEAN_CONTENT_TAGS 的统计口径。
    - <img> 的 data-src/data-original/data-lazy-src 归一化为 src（微信真图在 data-src）。
    """
    html = raw or ""
    # 只取 <body> 片段（剪贴板常带完整文档外壳；无 body 则原样交给 nh3 unwrap）
    m = re.search(r"<body\b[^>]*>(.*)</body\s*>", html, re.IGNORECASE | re.DOTALL)
    if m:
        html = m.group(1)
    html = re.sub(r"<!--/?(StartFragment|EndFragment)-->", "", html, flags=re.IGNORECASE)

    def _fix_img(tag_match):
        tag = tag_match.group(0)
        attrs = _parse_attrs(tag)
        src = (attrs.get("src") or "").strip()
        for cand in _LAZY_SRC_ATTRS:
            v = (attrs.get(cand) or "").strip()
            if v and v.startswith(("http://", "https://", "//")):
                src = v
                break
        if not src:
            return tag          # 无可用地址：留给后续阶段按失败占位处理
        if src.startswith("//"):
            src = "https:" + src
        return re.sub(r"\ssrc\s*=\s*([\"']).*?\1", f' src="{src}"', tag, count=1,
                      flags=re.IGNORECASE) if re.search(r"\ssrc\s*=", tag, re.IGNORECASE) \
            else tag.replace("<img", f'<img src="{src}"', 1)

    return _IMG_TAG_RE.sub(_fix_img, html)


def _parse_attrs(tag_text):
    """从单个标签文本里解析属性为 dict（仅服务归一化/改写，非安全边界）。"""
    out = {}
    for m in _ATTR_RE.finditer(tag_text):
        name = m.group(1).lower()
        out[name] = m.group(3) if m.group(3) is not None else m.group(4)
    return out


def extract_image_sources(html):
    """提取当前 HTML 中全部 <img src>（去重保序）。"""
    seen, ordered = set(), []
    for m in _IMG_TAG_RE.finditer(html or ""):
        src = (_parse_attrs(m.group(0)).get("src") or "").strip()
        if src and src not in seen:
            seen.add(src)
            ordered.append(src)
    return ordered


# ── 图片转存（方案 §12）────────────────────────────────────────────

def _is_platform_media(src):
    return src.startswith("/media/")


def _put_image(article_id, data):
    """字节流 -> 校验/转码（去元数据、沿用图集压缩策略）-> 对象存储，返回 /media URL。"""
    try:
        webp = imaging.showcase_gallery_bytes(io.BytesIO(data))   # 最长边 1600 WebP q82；GIF 取首帧静态化
    except imaging.ImageError as e:
        raise HtmlImportError(f"不是有效图片：{e}") from e
    key = f"media/articles/html/{int(article_id)}/{uuid.uuid4().hex}.webp"
    storage.put_object(key, io.BytesIO(webp), len(webp), "image/webp")
    return "/" + key


def _decode_data_url(src):
    """data:image/...;base64,xxx -> bytes（超限/非法直接抛业务错误）。"""
    m = re.match(r"^data:image/(png|jpe?g|webp|gif|bmp);base64,(.*)$", src, re.IGNORECASE | re.DOTALL)
    if not m:
        raise HtmlImportError("仅支持 png/jpeg/webp/gif/bmp 内嵌图片")
    b64 = m.group(2)
    if len(b64) * 3 // 4 > IMAGE_MAX_BYTES:
        raise HtmlImportError("内嵌图片超过 10MB 上限")
    try:
        return base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError) as e:
        raise HtmlImportError("内嵌图片编码非法") from e


class _UnsafeHost(ValueError):
    """SSRF 防护：目标主机解析到了不允许访问的地址。"""


def _assert_safe_ip(host):
    """DNS 解析后逐一校验 IP：拒绝环回/内网/链路本地/保留/组播/未指定地址。"""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise _UnsafeHost(f"域名无法解析：{host}") from e
    seen = set()
    for info in infos:
        raw = info[4][0]
        if raw in seen:
            continue
        seen.add(raw)
        ip = ipaddress.ip_address(raw.split("%")[0])     # 去掉 IPv6 zone id
        if ip.version == 6 and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped                          # ::ffff:10.0.0.1 之类的映射地址按 v4 判
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            raise _UnsafeHost(f"目标地址不允许抓取：{host} -> {ip}")


def _fetch_remote(url):
    """SSRF 防护下的远程图片抓取：逐跳重校验、限次数/超时/大小（方案 §12.3）。"""
    scheme = url.split(":", 1)[0].lower()
    if scheme == "http" and not ALLOW_HTTP_IMAGE_FETCH:
        raise HtmlImportError("仅支持 https 图源（http 需管理员在环境变量放开）")
    if scheme not in ("http", "https"):
        raise HtmlImportError(f"不支持的图片协议：{scheme}")

    current, hops = url, 0
    while True:
        from urllib.parse import urlsplit
        parts = urlsplit(current)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise HtmlImportError("重定向目标协议非法")
        _assert_safe_ip(parts.hostname)
        resp = requests.get(current, stream=True, allow_redirects=False,
                            timeout=FETCH_TIMEOUT,
                            headers={"User-Agent": FETCH_USER_AGENT})
        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("Location")
            resp.close()
            hops += 1
            if not location or hops > FETCH_MAX_REDIRECTS:
                raise HtmlImportError("重定向次数超限或目标缺失")
            from urllib.parse import urljoin
            current = urljoin(current, location)
            continue
        if resp.status_code != 200:
            resp.close()
            raise HtmlImportError(f"图源响应 {resp.status_code}")
        # 流式限量读取：Content-Length 不可信，超限即断
        buf = io.BytesIO()
        for chunk in resp.iter_content(chunk_size=128 * 1024):
            buf.write(chunk)
            if buf.tell() > IMAGE_MAX_BYTES:
                resp.close()
                raise HtmlImportError("远程图片超过 10MB 上限")
        resp.close()
        return buf.getvalue()


def _failed_placeholder(src, reason):
    """转存失败占位块：保留醒目标识 + 原地址（title），发布校验按标识阻止发布。"""
    safe_reason = (reason or "").replace('"', "'")[:200]
    return (
        f'<p style="border:2px dashed #f56c6c;padding:12px 8px;margin:8px 0;'
        f'color:#f56c6c;text-align:center;border-radius:8px" title="{src}">'
        f"<strong>{FAILED_IMAGE_MARK}</strong>（{safe_reason}）请删除本块后重新上传图片</p>"
    )


def transfer_images(article_id, html, stats=None):
    """正文图片转存：远程/base64 -> 平台对象存储；平台已有 /media 地址保留。

    返回改写后的 html。失败项替换为占位块并计入 stats['failed_images']。
    同一来源地址单次导入内去重（方案 §12.3）。
    """
    cache = {}
    n_ok = n_fail = 0

    def _replace(m):
        nonlocal n_ok, n_fail
        tag = m.group(0)
        src = (_parse_attrs(tag).get("src") or "").strip()
        if not src:
            n_fail += 1
            return _failed_placeholder("", "图片地址缺失")
        if _is_platform_media(src):
            return tag                              # 平台已有地址：保留
        if src.startswith("blob:"):
            n_fail += 1
            return _failed_placeholder(src, "blob 地址须由前端转文件上传")
        if src in cache:
            outcome = cache[src]
        else:
            try:
                if src.startswith("data:"):
                    data = _decode_data_url(src)
                else:
                    data = _fetch_remote(src)
                outcome = ("ok", _put_image(article_id, data))
            except (_UnsafeHost, HtmlImportError, Exception) as e:      # noqa: B014 - 统一转占位
                outcome = ("fail", f"{type(e).__name__}: {e}" if not isinstance(e, (HtmlImportError, _UnsafeHost)) else str(e))
            cache[src] = outcome
        if outcome[0] == "ok":
            n_ok += 1
            return re.sub(r"\ssrc\s*=\s*([\"']).*?\1", f' src="{outcome[1]}"', tag, count=1)
        n_fail += 1
        return _failed_placeholder(src, outcome[1])

    html = _IMG_TAG_RE.sub(_replace, html or "")
    if stats is not None:
        stats["images_imported"] = stats.get("images_imported", 0) + n_ok
        stats["images_failed"] = stats.get("images_failed", 0) + n_fail
    return html


# ── CSS / 属性清洗（方案 §11.3-§11.5）──────────────────────────────

def _clean_style_value(prop, value):
    """单条 CSS 声明的值级规则；返回清洗后的值或 None（整条删除）。"""
    v = value.strip().rstrip(";").strip()
    v = v.replace("!important", "").strip()
    if not v:
        return None
    if _CSS_VALUE_DANGER.search(v):
        return None
    if prop in CSS_DROP_PROPERTIES:
        return None
    if prop not in CSS_PROPERTIES:
        return None
    if prop == "background" and "url" in v.lower():
        return None
    # 数值上限：transform/尺寸/负边距不允许出现失控大数（hex 颜色已摘除，不参与判定）
    for num in _CSS_NUM_RE.findall(_HEX_COLOR_RE.sub(" ", v)):
        if abs(float(num)) > _CSS_NUM_MAX:
            return None
    return v


def _clean_css(value, stats=None):
    """style 属性整体清洗：属性白名单 + 值级规则；无剩余声明时返回 None 删除属性。"""
    kept, dropped = [], 0
    for decl in (value or "").split(";"):
        if not decl.strip() or ":" not in decl:
            continue
        prop, _, val = decl.partition(":")
        prop = prop.strip().lower()
        cleaned = _clean_style_value(prop, val)
        if cleaned is None:
            dropped += 1
        else:
            kept.append(f"{prop}: {cleaned}")
    if stats is not None and dropped:
        stats["removed_styles"] = stats.get("removed_styles", 0) + dropped
    return "; ".join(kept) if kept else None


def _make_attribute_filter(stats):
    """nh3 attribute_filter：style 白名单清洗、img src 限平台地址、a[target]=_blank。"""
    def _filter(tag, attribute, value):
        if attribute == "style":
            return _clean_css(value, stats)
        if attribute == "target" and tag == "a":
            return "_blank"
        if attribute == "src" and tag == "img":
            return value if _is_platform_media(value) else None
        return value
    return _filter


def sanitize_article_html(html, stats=None):
    """nh3 白名单清洗（标签/属性/URL/CSS）。幂等：多次清洗结果一致。"""
    return nh3.clean(
        html or "",
        tags=set(ALLOWED_TAGS),
        clean_content_tags=set(CLEAN_CONTENT_TAGS),
        attributes={k: set(v) for k, v in ALLOWED_ATTRIBUTES.items()},
        attribute_filter=_make_attribute_filter(stats),
        url_schemes=set(ALLOWED_URL_SCHEMES),
        link_rel="noopener noreferrer nofollow",
        strip_comments=True,
    )


# ── 报告与管道入口 ──────────────────────────────────────────────────

def build_import_report(source, raw, cleaned, stats):
    """导入报告（方案 §7.2 响应结构）：计数 + 警告列表。"""
    warnings = []
    if stats.get("images_failed"):
        warnings.append({
            "code": "IMAGE_FETCH_FAILED",
            "message": f"有 {stats['images_failed']} 张图片转存失败，已替换为占位块，请手动处理后再发布",
        })
    if stats.get("removed_tags"):
        warnings.append({
            "code": "TAGS_REMOVED",
            "message": f"移除了 {stats['removed_tags']} 处不支持的内容（脚本/表单/视频等）",
        })
    if stats.get("removed_styles"):
        warnings.append({
            "code": "STYLES_REMOVED",
            "message": f"清理了 {stats['removed_styles']} 条不支持的样式声明（定位/背景图等）",
        })
    return {
        "source": source,
        "node_count": len(re.findall(r"<[a-zA-Z]", cleaned or "")),
        "images_found": stats.get("images_found", 0),
        "images_imported": stats.get("images_imported", 0),
        "images_failed": stats.get("images_failed", 0),
        "removed_tags": stats.get("removed_tags", 0),
        "removed_attributes": stats.get("removed_attributes", 0),
        "removed_styles": stats.get("removed_styles", 0),
        "warnings": warnings,
    }


def _pre_count_stats(raw):
    """导入前粗统计：黑名单标签与 on* 事件属性的出现次数（报告口径，近似值）。"""
    low = (raw or "").lower()
    removed_tags = sum(len(re.findall(rf"<{t}\b", low)) for t in CLEAN_CONTENT_TAGS)
    removed_attrs = len(re.findall(r"\son[a-zA-Z]+\s*=", low))
    return {"removed_tags": removed_tags, "removed_attributes": removed_attrs}


def import_html(article_id, raw_html, source_hint=None, files=None):
    """导入管道入口（/v2/article/admin/html/import 调用）。

    返回 (清洗后 html, report, 剪贴板文件上传后的 URL 列表)。
    files 是剪贴板里的本地图片文件（Werkzeug FileStorage 列表）。
    """
    raw = raw_html or ""
    if len(raw.encode("utf-8", errors="ignore")) > RAW_HTML_MAX_BYTES:
        raise HtmlImportError("原始内容超过 3MB 上限")

    stats = _pre_count_stats(raw)
    source = detect_source(raw, source_hint)

    html = normalize_html(raw)
    srcs = extract_image_sources(html)
    stats["images_found"] = len(srcs)
    if len(srcs) > IMAGES_MAX:
        raise HtmlImportError(f"单篇正文图片超过 {IMAGES_MAX} 张上限")
    html = transfer_images(article_id, html, stats)
    cleaned = sanitize_article_html(html, stats)

    file_urls = []
    for f in (files or []):
        data = f.read()
        if len(data) > IMAGE_MAX_BYTES:
            raise HtmlImportError(f"剪贴板文件 {f.filename} 超过 10MB 上限")
        file_urls.append(_put_image(article_id, data))

    if len(cleaned.encode("utf-8", errors="ignore")) > CLEAN_HTML_MAX_BYTES:
        raise HtmlImportError("清洗后内容超过 2MB 上限")
    return cleaned, build_import_report(source, raw, cleaned, stats), file_urls


def clean_for_save(article_id, raw_html):
    """保存管道（草稿/发布/编辑每次写库前调用）：与导入同管道，幂等。

    正常流程里图片在导入时已转存为 /media 地址，此处再走一遍是纵深防御：
    绕过导入接口直接提交的远程/内嵌图片同样被转存或替换为占位块。
    返回 (清洗后 html, report)。
    """
    raw = raw_html or ""
    if len(raw.encode("utf-8", errors="ignore")) > RAW_HTML_MAX_BYTES:
        raise HtmlImportError("正文超过 3MB 上限")
    stats = _pre_count_stats(raw)
    html = normalize_html(raw)
    srcs = extract_image_sources(html)
    stats["images_found"] = len(srcs)
    if len(srcs) > IMAGES_MAX:
        raise HtmlImportError(f"单篇正文图片超过 {IMAGES_MAX} 张上限")
    html = transfer_images(article_id, html, stats)
    cleaned = sanitize_article_html(html, stats)
    if len(cleaned.encode("utf-8", errors="ignore")) > CLEAN_HTML_MAX_BYTES:
        raise HtmlImportError("清洗后正文超过 2MB 上限")
    return cleaned, build_import_report("save", raw, cleaned, stats)


def has_failed_images(html):
    """发布校验：正文里是否仍有转存失败占位块。"""
    return FAILED_IMAGE_MARK in (html or "")


def remove_article_html_media(article_id):
    """删除文章时清理其 HTML 媒体目录（media/articles/html/<id>/，best-effort）。"""
    prefix = f"media/articles/html/{int(article_id)}/"
    try:
        keys = storage.list_objects(prefix)
    except Exception:
        return 0
    n = 0
    for key in keys:
        try:
            storage.remove_object(key)
            n += 1
        except Exception:
            pass
    return n

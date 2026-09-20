"""article_html 服务单元测试（无 DB / 无网络，方案 §16.2）。

运行：python scripts/test_article_html.py（项目根，任意 venv；不依赖 Flask app 装配）
覆盖：DOM 归一化、标签/属性/URL/CSS 白名单、清洗幂等、图片转存（stub storage）、
SSRF 地址校验、失败占位、上限、导入报告。
"""
import io
import os
import sys
import traceback

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from services import article_html as ah   # noqa: E402


class _MemStorage:
    """内存版 storage stub：只验证 put/list/remove 调用路径。"""

    def __init__(self):
        self.objects = {}

    def put_object(self, key, stream, length=None, content_type=""):
        self.objects[key] = stream.read()

    def list_objects(self, prefix=""):
        return [k for k in self.objects if k.startswith(prefix)]

    def remove_object(self, key):
        self.objects.pop(key, None)


# 测试用最小 PNG：Pillow 现生成（避免手写 base64 损坏）
import base64  # noqa: E402
from PIL import Image  # noqa: E402

_png_buf = io.BytesIO()
Image.new("RGB", (4, 4), (200, 30, 30)).save(_png_buf, "PNG")
_DATA_URL = "data:image/png;base64," + base64.b64encode(_png_buf.getvalue()).decode()

_FAILED = 0


def check(name, fn):
    global _FAILED
    try:
        fn()
        print(f"[ok] {name}")
    except Exception:
        _FAILED += 1
        print(f"[FAIL] {name}")
        traceback.print_exc()


def _must(cond, msg="assertion failed"):
    if not cond:
        raise AssertionError(msg)


# 1) DOM 归一化
def t_normalize():
    raw = ('<!--StartFragment--><html><head><meta charset="utf-8"></head>'
           '<body><p>hi</p></body></html>')
    out = ah.normalize_html(raw)
    _must("<body" not in out and "<html" not in out, f"body 未剥: {out}")
    _must("<p>hi</p>" in out)


def t_normalize_wechat_lazy():
    raw = ('<img data-src="https://mmbiz.qpic.cn/a.jpg" data-ratio="0.5" '
           'src="data:image/svg+xml,%3Csvg%3E">')
    out = ah.normalize_html(raw)
    _must('src="https://mmbiz.qpic.cn/a.jpg"' in out, f"懒加载未归一: {out}")
    raw2 = '<img data-original="//cdn.xiumi.us/b.png">'
    out2 = ah.normalize_html(raw2)
    _must('src="https://cdn.xiumi.us/b.png"' in out2, f"协议相对未补: {out2}")


# 2) 白名单清洗
MALicious = (
    '<section style="color:#333;position:fixed;top:0;background:url(http://x/1)">'
    '<p onclick="evil()">hi</p><script>alert(1)</script><iframe src="http://x"></iframe>'
    '<form><input></form><svg onload="x()"></svg>'
    '<a href="javascript:alert(1)" target="_self">a</a>'
    '<a href="https://ok.example" target="_self">b</a>'
    '<img src="http://evil/x.jpg" onerror="y()">'
    '<span id="s1" class="c1" data-x="1" style="margin:-99999px;color:red">t</span>'
    '</section>'
)


def t_sanitize_tags():
    out = ah.sanitize_article_html(MALicious)
    for bad in ("<script", "<iframe", "<form", "<input", "<svg", "onclick", "onerror",
                "javascript:"):
        _must(bad not in out, f"未清掉 {bad}: {out}")
    # span 在白名单内应保留，但其 id/class/data-* 须被剥掉（方案 §11.3）
    _must('<span style="color: red">t</span>' in out, f"span 语义/属性异常: {out}")
    _must('id="s1"' not in out and 'class="c1"' not in out and 'data-x' not in out,
          "id/class/data-* 泄漏")
    _must('href="https://ok.example"' in out, f"合法链接被误删: {out}")
    _must('target="_blank"' in out and 'target="_self"' not in out, "target 未统一 _blank")
    _must('rel="noopener noreferrer nofollow"' in out, "rel 未补")


def t_sanitize_css():
    out = ah.sanitize_article_html(MALicious)
    _must("color:" in out, "合法 color 被删")
    _must("position" not in out and "top" not in out.replace("topic", ""), "position 泄漏")
    _must("url(" not in out, "CSS url() 泄漏")
    _must("margin" not in out, "失控负边距未被删")
    _must("-99999" not in out, "失控数值泄漏")


def t_css_hex_color_not_killed():
    # hex 颜色的数字不参与数值上限（09-20 修复：#444444 曾被当 444444 误杀）
    html = ('<section style="color:#444444;background-color:#eef4fb;'
            'border:1px solid #d8e2f0;font-size:14px">x</section>')
    out = ah.sanitize_article_html(html)
    _must("color: #444444" in out, f"hex 颜色被误杀: {out}")
    _must("background-color: #eef4fb" in out, f"hex 背景色被误杀: {out}")
    # 真实失控数值仍要拦：99999px 边距
    out2 = ah.sanitize_article_html('<p style="margin-left: 99999px">x</p>')
    _must("margin-left" not in out2, "失控数值未被拦")


def t_sanitize_img_src_platform_only():
    out = ah.sanitize_article_html('<img src="http://evil/x.jpg" alt="a">'
                                   '<img src="/media/articles/html/1/abc.webp">')
    _must('src="http://evil' not in out, "非平台 img src 未删")
    _must('src="/media/articles/html/1/abc.webp"' in out, "平台地址被误删")


def t_idempotent():
    once = ah.sanitize_article_html(MALicious)
    twice = ah.sanitize_article_html(once)
    _must(once == twice, f"清洗不幂等:\n{once}\n{twice}")


# 3) 图片转存（stub storage + data URL）
def t_transfer_data_url():
    mem = _MemStorage()
    old = ah.storage
    ah.storage = mem
    try:
        html = f'<p><img src="{_DATA_URL}" style="width:100px"></p>'
        out = ah.transfer_images(7, html)
        _must('src="/media/articles/html/7/' in out, f"data URL 未转存: {out}")
        _must(len([k for k in mem.objects if k.startswith("media/articles/html/7/")]) == 1,
              "对象未入 storage")
        # 二次清洗（保存路径）：已是平台地址，不再转存
        cleaned = ah.sanitize_article_html(out)
        _must(cleaned.count('src="/media/articles/html/7/') == 1, "平台地址二次转存或丢失")
    finally:
        ah.storage = old


def t_transfer_failure_placeholder():
    old = ah.storage
    ah.storage = _MemStorage()
    try:
        html = '<p><img src="data:image/png;base64,####"></p>'
        out = ah.transfer_images(7, html)
        _must(ah.FAILED_IMAGE_MARK in out, f"失败未落占位: {out}")
        _must(ah.has_failed_images(out), "has_failed_images 未识别")
        # 占位块可安全通过清洗存活（发布校验依赖它在清洗后仍可见）
        cleaned = ah.sanitize_article_html(out)
        _must(ah.has_failed_images(cleaned), "占位块被清洗删除")
    finally:
        ah.storage = old


def t_transfer_blob_rejected():
    out = ah.transfer_images(7, '<p><img src="blob:xx"></p>')
    _must(ah.has_failed_images(out), "blob 地址应转失败占位")


# 4) SSRF 地址校验（IP 字面量不触发 DNS）
def t_ssrf():
    for host in ("127.0.0.1", "10.0.0.1", "192.168.1.1", "169.254.169.254",
                 "0.0.0.0", "::1", "fe80::1", "fd00::1", "::ffff:127.0.0.1"):
        try:
            ah._assert_safe_ip(host)
            raise AssertionError(f"内网地址放行: {host}")
        except ah._UnsafeHost:
            pass


# 5) 上限
def t_limits():
    imgs = "".join(f'<img src="/media/a{i}.webp">' for i in range(101))
    try:
        ah.clean_for_save(1, f"<p>{imgs}</p>")
        raise AssertionError("超 100 张图未拦截")
    except ah.HtmlImportError:
        pass
    try:
        ah.clean_for_save(1, "<p>" + "x" * (ah.RAW_HTML_MAX_BYTES + 1) + "</p>")
        raise AssertionError("超 3MB 未拦截")
    except ah.HtmlImportError:
        pass


# 6) 导入管道 + 报告
def t_import_report():
    mem = _MemStorage()
    old = ah.storage
    ah.storage = mem
    try:
        raw = ('<html><body><section style="color:#111;position:fixed">'
               f'<img src="{_DATA_URL}"><script>bad()</script></section></body></html>')
        cleaned, report, files = ah.import_html(9, raw, "xiumi")
        _must(report["source"] == "xiumi", "来源识别失败")
        _must(report["images_found"] == 1 and report["images_imported"] == 1, f"图片计数错: {report}")
        _must(report["removed_tags"] >= 1, "黑名单标签未计数")
        _must("<script" not in cleaned, "脚本残留")
        _must("position" not in cleaned, "position 残留")
        _must(report["node_count"] > 0, "node_count 异常")
    finally:
        ah.storage = old


def t_source_detect():
    _must(ah.detect_source("<i data-x='xiumi.us'>x</i>") == "xiumi")
    _must(ah.detect_source('<img data-src="https://mmbiz.qpic.cn/a.jpg">') == "wechat")
    _must(ah.detect_source("<p>plain</p>") == "web")


if __name__ == "__main__":
    tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("t_")]
    for name, fn in tests:
        check(name, fn)
    print(f"\n{len(tests)} 项，失败 {_FAILED} 项")
    sys.exit(1 if _FAILED else 0)

# -*- coding: utf-8 -*-
"""BME 平台统一邮件模板。

所有对外邮件共用一个正式结构（品牌色头部 + 标题 + 正文 + 页脚）；验证码类邮件
在正文中央使用独立的验证码展示区（结构参考 BioNoteBooks 的验证码邮件，
配色对齐平台品牌蓝）。每套模板同时产出纯文本版本，作为不支持 HTML 的客户端
fallback。

样式纪律：邮件客户端兼容性优先，全部内联 style，不引外部资源；
正文与标题一律 HTML 转义，防止通知文案注入邮件结构。

用法：
    from services.mail_templates import render_notification, render_code_email

    tpl = render_notification(title, content)
    mail.send(Message(subject=f'[BME] {title}', recipients=[...],
                      body=tpl['text'], html=tpl['html']))

    tpl = render_code_email(code, title='身份核验验证码',
                            purpose='您正在完成教育邮箱身份核验', ttl_minutes=5)
"""

from html import escape

BRAND = 'BME 卓越工程师在线教育平台'

# 品牌蓝系（对齐前端 --color-primary 家族）
_C_PRIMARY = '#2563eb'
_C_DEEP = '#1e40af'
_C_BG = '#eff6ff'
_C_BORDER = '#bfdbfe'
_C_TEXT = '#1f2937'
_C_MUTED = '#6b7280'
_C_FAINT = '#9ca3af'


def _shell(title, inner_html):
    """统一外框：品牌头 + 内容卡 + 页脚。所有模板共用。"""
    return f"""\
<div style="margin:0;padding:24px 12px;background:#f3f4f6;font-family:'PingFang SC','Microsoft YaHei',Arial,sans-serif;">
  <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="max-width:560px;margin:0 auto;">
    <tr><td style="background:{_C_PRIMARY};border-radius:10px 10px 0 0;padding:16px 24px;">
      <span style="color:#ffffff;font-size:16px;font-weight:700;letter-spacing:1px;">{BRAND}</span>
    </td></tr>
    <tr><td style="background:#ffffff;border:1px solid #e5e7eb;border-top:0;border-radius:0 0 10px 10px;padding:28px 28px 20px;">
{inner_html}
    </td></tr>
    <tr><td style="padding:14px 8px 0;text-align:center;">
      <span style="color:{_C_FAINT};font-size:12px;line-height:1.7;">
        本邮件由系统自动发送，请勿直接回复<br>— {BRAND} —
      </span>
    </td></tr>
  </table>
</div>"""


def render_notification(title, content, footnote=None):
    """通用通知邮件：正式结构，正文按空行分段。"""
    paragraphs = ''.join(
        f'      <p style="margin:0 0 14px;color:{_C_TEXT};font-size:14px;line-height:1.8;">{escape(p)}</p>'
        for p in str(content or '').split('\n\n') if p.strip()
    )
    foot = (f'      <p style="margin:8px 0 0;color:{_C_MUTED};font-size:12px;">{escape(footnote)}</p>'
            if footnote else '')
    inner = f"""\
      <h2 style="margin:0 0 18px;color:{_C_DEEP};font-size:18px;">{escape(title)}</h2>
{paragraphs}{foot}"""
    text = f'{title}\n\n{content}'
    if footnote:
        text += f'\n\n{footnote}'
    return {'text': text, 'html': _shell(title, inner)}


def render_code_email(code, title, purpose, ttl_minutes):
    """验证码邮件：中央独立验证码展示区 + 有效期与安全提示。"""
    digits = ' '.join(str(code))
    inner = f"""\
      <h2 style="margin:0 0 18px;color:{_C_DEEP};font-size:18px;">{escape(title)}</h2>
      <p style="margin:0 0 6px;color:{_C_TEXT};font-size:14px;line-height:1.8;">{escape(purpose)}，验证码为：</p>
      <div style="background:{_C_BG};border:1px solid {_C_BORDER};border-radius:10px;padding:18px 12px;margin:18px 0;text-align:center;">
        <span style="font-size:30px;font-weight:700;letter-spacing:6px;color:{_C_DEEP};font-family:'Courier New',Consolas,monospace;">{digits}</span>
      </div>
      <p style="margin:0 0 6px;color:{_C_TEXT};font-size:14px;line-height:1.8;">
        验证码 <strong>{ttl_minutes} 分钟</strong>内有效，请尽快完成验证。
      </p>
      <p style="margin:16px 0 0;color:{_C_MUTED};font-size:12px;line-height:1.7;">
        如非本人操作，请忽略本邮件，您的账号依然安全。验证码请勿转发给任何人。
      </p>"""
    text = (f'{title}\n\n{purpose}，验证码为：{code}\n\n'
            f'验证码 {ttl_minutes} 分钟内有效。\n如非本人操作，请忽略本邮件；验证码请勿转发给任何人。')
    return {'text': text, 'html': _shell(title, inner)}

"""LLM chat completion 轻量客户端（requests 直调，非 SDK）。

DeepSeek 提供 OpenAI 兼容的 /chat/completions 端点，开发期应用直连即可，
不必经 LiteLLM Proxy（proxy 本地启动遇 litellm[proxy] 的 proxy_server 模块问题；
DeepSeek 直连更简单、少一层）。DEEPSEEK_API_KEY 从环境/.env 读取（app load_dotenv）。

后续若要统一多 provider / 预算 / 日志，再切回 LiteLLM Proxy：把下方 DEEPSEEK_BASE_URL
换成 LITELLM_BASE_URL、key 换成 virtual key 即可（调用形态一致）。
"""
import os
import requests
from flask import current_app

# DeepSeek OpenAI 兼容端点；如走 proxy，改成 LITELLM_BASE_URL + 用 virtual key
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")


def chat_completion(messages, model=None, temperature=0.7, timeout=120, response_json=False):
    """调 DeepSeek chat/completions，返回助手消息文本。

    response_json=True 时要求模型只输出 JSON（response_format=json_object），调用方自行解析。
    DEEPSEEK_API_KEY 从环境读（app 启动时 load_dotenv 注入）。
    """
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        raise RuntimeError("DEEPSEEK_API_KEY 未设置（写入 .env 或 export 到环境）")
    model = model or current_app.config.get("AI_TOPIC_MODEL", "deepseek-chat")
    body = {"model": model, "messages": messages, "temperature": temperature}
    if response_json:
        body["response_format"] = {"type": "json_object"}
    r = requests.post(
        f"{DEEPSEEK_BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json=body,
        timeout=timeout,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]

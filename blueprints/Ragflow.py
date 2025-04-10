import json
from flask import Blueprint, request, redirect, jsonify, send_file, Response, stream_with_context
from wtforms.validators import email
import calendar, time, os

# from openai import OpenAI

bp = Blueprint("Ragflow", __name__, url_prefix="/AI")

# 初始化客户端
# model = "model"
# client = OpenAI(
#     api_key="ragflow-NjYzVkZGUwMDMyZDExZjBiYzZjOTZiMD",
#     # base_url="http://172.25.56.83:3380/api/v1/chats_openai/c5832400021c11f0bd7e96b002ae6e83"
#     base_url="http://172.25.56.83:3380/api/v1/chats_openai/67cbf16e0ba111f0aadc96b002ae6e83"
# )

# 导入api文档模块
from flasgger import swag_from

# 添加新的流式聊天接口
@bp.route('/chat', methods=['POST'])
@swag_from('../apidocs/Ragflow/chat_stream.yaml')
def chat_stream():
    # 解析JSON请求
    user_message = request.json.get('message')
    # print("Received user message:", user_message)
    # 创建流式生成器
    # def generate():
    #     # 调用OpenAI流式API
    #     stream = client.chat.completions.create(
    #         model=model,
    #         messages=[{"role": "user", "content": user_message}],
    #         stream=True
    #
    #     )
    #     # print(stream)
    #     # 逐块流式返回响应
    #     for chunk in stream:
    #         if chunk.choices and chunk.choices[0].delta.content:
    #             content = chunk.choices[0].delta.content
    #             print(content)  # 调试输出
    #             # yield f"data: {content}\n\n"
    #             for char in content:
    #                 yield f"data: {char}\n\n"
    #
    #         # 返回流式响应
    #
    # return Response(generate(), mimetype="text/event-stream")
    pass


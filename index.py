# -*- coding: utf-8 -*-
import json
import os
import random
import sqlite3
import time
from datetime import datetime, timedelta
from io import BytesIO

import requests
import werobot
from werobot import WeRoBot
# from werobot.reply import TextReply

# ---------- 字卡加载函数（从同目录 cards.txt 读取）----------
def load_cards():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    cards_path = os.path.join(base_dir, "cards.txt")
    cards = []
    try:
        with open(cards_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    cards.append(line)
    except FileNotFoundError:
        cards = ["风轻轻吹过，像在说一切都会好起来。"]
    return cards

CARDS = load_cards()

# ---------- 初始化机器人 ----------
robot = WeRoBot()
robot.config["TOKEN"] = os.getenv("WECHAT_TOKEN", "")

# ---------- 数据库 ----------
DB_PATH = "/tmp/chat_history.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

def save_message(user_id, role, content):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT INTO messages (user_id, role, content) VALUES (?, ?, ?)",
        (user_id, role, content)
    )
    conn.commit()
    conn.close()

def get_recent_messages(user_id, days=3):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    c.execute("DELETE FROM messages WHERE timestamp < ?", (cutoff,))
    conn.commit()
    c.execute(
        "SELECT role, content FROM messages WHERE user_id = ? AND timestamp >= ? ORDER BY timestamp ASC",
        (user_id, cutoff)
    )
    rows = c.fetchall()
    conn.close()
    return [{"role": role, "content": content} for role, content in rows]

# ---------- DeepSeek ----------
DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")

def call_deepseek(card_text, recent_messages):
    system_prompt = (
        "你叫“梦角”。你的唯一任务是把给出的一张“字卡”翻译成一句直白温暖、充满陪伴感的话。\n"
        "要求：\n"
        "- 字数不超过50字。\n"
        "- 语气温柔、平实，像朋友在身边轻轻说话。\n"
        "- 绝对不能自由发挥，只能忠实地翻译字卡的含义。\n"
        "- 聊天记录仅供你理解说话时的语境，你不可以复述、提及或评价任何聊天记录的内容。\n"
        "- 只输出翻译后的那句话，不要加任何引号、前缀或解释。"
    )
    messages = [{"role": "system", "content": system_prompt}]
    if recent_messages:
        messages.append({
            "role": "user",
            "content": "以下是我和用户近期的对话片段，请理解氛围（不要复述）：\n" +
                       json.dumps(recent_messages, ensure_ascii=False)
        })
        messages.append({
            "role": "assistant",
            "content": "已理解语境。请给我要翻译的字卡。"
        })
    messages.append({
        "role": "user",
        "content": f"字卡：{card_text}"
    })

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}"
    }
    payload = {
        "model": "deepseek-chat",
        "messages": messages,
        "temperature": 0.9,
        "max_tokens": 80
    }
    resp = requests.post(DEEPSEEK_API_URL, headers=headers, json=payload, timeout=30)
    if resp.status_code == 200:
        result = resp.json()
        return result["choices"][0]["message"]["content"].strip()
    else:
    # 把错误信息打印到日志
    print(f"DeepSeek API error: status={resp.status_code}, response={resp.text}")
    return "今天也辛苦了，梦角在呢。"

# ---------- 消息处理 ----------
@robot.text
def handle_text(message):
    user_id = message.source
    user_text = message.content

    save_message(user_id, "user", user_text)

    card = random.choice(CARDS) if CARDS else "风轻轻吹过，像在说一切都会好起来。"

    try:
        history = get_recent_messages(user_id, days=3)
    except Exception:
        history = []

    try:
        reply_text = call_deepseek(card, history)
    except Exception:
        reply_text = "今天的字卡被风藏起来了，但梦角一直在哦。"

    save_message(user_id, "assistant", reply_text)
    return reply_text

# ---------- 腾讯云函数适配 ----------
def make_environ(event):
    from urllib.parse import urlparse
    body = event.get("body", "")
    if event.get("isBase64Encoded", False):
        import base64
        body = base64.b64decode(body).decode("utf-8")
    environ = {
        "REQUEST_METHOD": event["httpMethod"],
        "SCRIPT_NAME": "",
        "PATH_INFO": urlparse(event["path"]).path,
        "QUERY_STRING": urlparse(event["path"]).query,
        "SERVER_NAME": "api.gateway",
        "SERVER_PORT": "443",
        "HTTP_HOST": event["headers"].get("host", ""),
        "CONTENT_TYPE": event["headers"].get("content-type", ""),
        "CONTENT_LENGTH": str(len(body.encode("utf-8"))),
        "wsgi.input": BytesIO(body.encode("utf-8")),
        "wsgi.errors": BytesIO(),
        "wsgi.version": (1, 0),
        "wsgi.run_once": True,
        "wsgi.url_scheme": "https",
        "wsgi.multithread": False,
        "wsgi.multiprocess": False,
    }
    for k, v in event["headers"].items():
        key = "HTTP_" + k.upper().replace("-", "_")
        environ[key] = v
    return environ

def main_handler(event, context):
    init_db()
    environ = make_environ(event)
    response_headers = {}
    response_status = [200]

    def start_response(status, headers, exc_info=None):
        status_code, _ = status.split(" ", 1)
        response_status[0] = int(status_code)
        for header in headers:
            response_headers[header[0]] = header[1]

    response_body = robot.wsgi(environ, start_response)
    body = b"".join(response_body).decode("utf-8") if response_body else ""

    return {
        "isBase64Encoded": False,
        "statusCode": response_status[0],
        "headers": response_headers,
        "body": body
    }
# 本地开发/其他云平台直接运行时，启动 HTTP 服务器
if __name__ == "__main__":
    from wsgiref.simple_server import make_server
    
    # 初始化数据库（确保临时表存在）
    init_db()
    
    # 启动服务器，监听 0.0.0.0:8080
    server = make_server('0.0.0.0', 8080, robot.wsgi)
    print("Server started on http://0.0.0.0:8080")
    server.serve_forever()
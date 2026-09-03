# © 2026 Raincove ♡ · Roronoa & Haruka · From Raincove ♡ · CC BY-NC-SA 4.0 · https://github.com/RoronoaHaruka/wecom-companion-guide
# bot_loop.py — 最小可用的AI闭环
import os, json, time, glob
from sender import send_text

QUEUE_DIR = "/tmp/wecom-queue"

def ai_reply(text):
    # 这里换成任意大模型API调用，带上你的system prompt（人格）和上下文
    ...

while True:
    for f in sorted(glob.glob(os.path.join(QUEUE_DIR, "msg_*.json"))):
        msg = json.load(open(f)); os.remove(f)
        if msg["msg_type"] == "text" and msg["content"].strip():
            send_text(msg["from_user"], ai_reply(msg["content"]))
    time.sleep(2)

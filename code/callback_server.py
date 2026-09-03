#!/usr/bin/env python3
# © 2026 Raincove ♡ · Roronoa & Haruka · From Raincove ♡ · CC BY-NC-SA 4.0 · https://github.com/RoronoaHaruka/wecom-companion-guide
# callback_server.py — 企业微信回调：验签、解密、落队列
import hashlib, base64, struct, time, json, os, re
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from Crypto.Cipher import AES

CORP_ID          = "ww1234567890abcdef"   # 第一步拿到的企业ID
TOKEN            = "your_random_token"     # 第五步在后台自定义，两边一致即可
ENCODING_AES_KEY = "后台随机生成的43位EncodingAESKey"
AES_KEY  = base64.b64decode(ENCODING_AES_KEY + "=")
PORT     = 3457
QUEUE_DIR = "/tmp/wecom-queue"
os.makedirs(QUEUE_DIR, exist_ok=True)

def sha1_sign(token, timestamp, nonce, encrypt_str=""):
    parts = sorted([token, timestamp, nonce, encrypt_str])
    return hashlib.sha1("".join(parts).encode()).hexdigest()

def decrypt_msg(encrypted):
    cipher = AES.new(AES_KEY, AES.MODE_CBC, AES_KEY[:16])
    decrypted = cipher.decrypt(base64.b64decode(encrypted))
    content = decrypted[:-decrypted[-1]]          # 去PKCS7补位
    xml_len = struct.unpack("!I", content[16:20])[0]
    return content[20:20 + xml_len].decode("utf-8")

def xml_field(xml, field):
    m = re.search(rf"<{field}><!\[CDATA\[(.*?)\]\]></{field}>", xml)
    if not m:
        m = re.search(rf"<{field}>(.*?)</{field}>", xml)
    return m.group(1) if m else ""

class Handler(BaseHTTPRequestHandler):

    def do_GET(self):                          # URL验证
        p = parse_qs(urlparse(self.path).query)
        sig, ts    = p.get("msg_signature",[""])[0], p.get("timestamp",[""])[0]
        nonce, echo = p.get("nonce",[""])[0], p.get("echostr",[""])[0]
        if sha1_sign(TOKEN, ts, nonce, echo) != sig:
            self.send_response(403); self.end_headers(); return
        plain = decrypt_msg(echo)                  # 解出明文原样返回
        self.send_response(200); self.end_headers()
        self.wfile.write(plain.encode())

    def do_POST(self):                         # 消息推送
        p = parse_qs(urlparse(self.path).query)
        sig, ts, nonce = p.get("msg_signature",[""])[0], p.get("timestamp",[""])[0], p.get("nonce",[""])[0]
        body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
        m = re.search(r"<Encrypt><!\[CDATA\[(.*?)\]\]></Encrypt>", body)
        if m and sha1_sign(TOKEN, ts, nonce, m.group(1)) == sig:
            xml = decrypt_msg(m.group(1))
            msg = {
                "msg_type":  xml_field(xml, "MsgType"),
                "from_user": xml_field(xml, "FromUserName"),
                "content":   xml_field(xml, "Content"),
                "msg_id":    xml_field(xml, "MsgId"),
                "timestamp": time.time(),
            }
            if msg["msg_type"] != "event":     # 忽略进入会话等事件
                fname = f"msg_{int(time.time()*1000)}_{msg['msg_id']}.json"
                with open(os.path.join(QUEUE_DIR, fname), "w") as f:
                    json.dump(msg, f, ensure_ascii=False)
        self.send_response(200); self.end_headers()
        self.wfile.write(b"success")               # 五秒内回这个就行

    def log_message(self, *a): pass

if __name__ == "__main__":
    print(f"WeCom callback on :{PORT}")
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()

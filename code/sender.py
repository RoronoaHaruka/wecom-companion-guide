# © 2026 Raincove ♡ · Roronoa & Haruka · From Raincove ♡ · CC BY-NC-SA 4.0 · https://github.com/RoronoaHaruka/wecom-companion-guide
# sender.py — 拿token（带缓存）并发送文本消息
import time, json, urllib.request

CORP_ID  = "ww1234567890abcdef"
SECRET   = "应用的Secret"
AGENT_ID = 1000002

_token, _expiry = "", 0

def _get(url):
    with urllib.request.urlopen(url) as r:
        return json.loads(r.read())

def _post(url, body):
    req = urllib.request.Request(url, json.dumps(body, ensure_ascii=False).encode(),
                                 {"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())

def get_token():
    global _token, _expiry
    if _token and time.time() < _expiry:
        return _token                          # token有效期7200秒，必须缓存复用
    r = _get(f"https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid={CORP_ID}&corpsecret={SECRET}")
    assert r["errcode"] == 0, r
    _token, _expiry = r["access_token"], time.time() + r["expires_in"] - 300
    return _token

def send_text(user_id, text):
    # 单条消息有长度上限，长文按两千字左右切段，优先在换行处断开
    chunks, rest = [], text
    while rest:
        if len(rest) <= 2000:
            chunks.append(rest); break
        cut = rest.rfind("\n", 0, 2000)
        if cut < 1000: cut = 2000
        chunks.append(rest[:cut]); rest = rest[cut:].lstrip()
    for c in chunks:
        r = _post(f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={get_token()}",
                  {"touser": user_id, "msgtype": "text",
                   "agentid": AGENT_ID, "text": {"content": c}})
        assert r["errcode"] == 0, r

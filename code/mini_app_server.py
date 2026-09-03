#!/usr/bin/env python3
# Copyright (c) 2026 Roronoa & Haruka · From Raincove ♡
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Serve a token-scoped observation page for WeCom's built-in browser.

One static page at PAGE_PATH plus two JSON endpoints under API_PREFIX.
Only requests under API_PREFIX accept MINI_APP_TOKEN; everything else is
refused, so a leaked page link cannot open any other door on the server.

Replace load_status() and snapshot() with your own data source. Everything
else is generic. Standard library only.
"""

from __future__ import annotations

import hmac
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

PAGE_PATH = "/mini"
API_PREFIX = "/api/mini/"
PAGE_FILE = Path(os.environ.get("MINI_APP_PAGE", Path(__file__).with_name("mini_app_page.html")))
EXPECTED_TOKEN = os.environ.get("MINI_APP_TOKEN", "")
BIND_HOST = os.environ.get("MINI_APP_BIND_HOST", "127.0.0.1")
PORT = int(os.environ.get("MINI_APP_PORT", "8766"))
STREAM_POLL_SECONDS = 1.0
STREAM_HEARTBEAT_SECONDS = 15.0


def presented_token(query: dict[str, list[str]], authorization_header: str) -> str:
    """Token from ?auth= (EventSource cannot set headers) or a Bearer header."""
    values = query.get("auth") or []
    if values and values[0]:
        return values[0]
    header = authorization_header or ""
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return ""


def is_scoped_request(path: str, presented: str, expected: str) -> bool:
    """Only paths under API_PREFIX accept the mini-app token."""
    if not expected or not presented:
        return False
    if not path.startswith(API_PREFIX):
        return False
    return hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))


# --- Replace these two functions with your real data source -----------------

def load_status() -> list[dict]:
    """Items shown on the home screen. Each needs id, name, alive."""
    return [
        {"id": "demo-a", "name": "示例进程 A", "alive": True, "busy": False, "note": "把 load_status() 换成你自己的数据源"},
        {"id": "demo-b", "name": "示例进程 B", "alive": True, "busy": True, "note": "busy 会让在线点闪烁"},
        {"id": "demo-c", "name": "示例进程 C", "alive": False, "busy": False, "note": "alive=false 显示为灰点"},
    ]


def snapshot(item_id: str) -> dict:
    """Latest text for one item, e.g. tmux capture-pane output or a log tail."""
    known = {item["id"]: item for item in load_status()}
    if item_id not in known:
        return {"error": "unknown item"}
    lines = [f"{time.strftime('%H:%M:%S')}  {known[item_id]['name']}  示例快照第 {int(time.time()) % 1000} 帧",
             "把 snapshot() 换成真实内容，例如 tmux capture-pane 的输出。",
             "文本变化时才推新帧，静止时只发心跳。"]
    return {"id": item_id, "alive": known[item_id]["alive"], "text": "\n".join(lines), "captured_at": time.time()}


# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "MiniApp/1.0"

    def log_message(self, fmt, *args):  # never log the query string (it carries the token)
        path = urlparse(self.path).path
        print(f"{self.address_string()} {self.command} {path}", flush=True)

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        url = urlparse(self.path)
        query = parse_qs(url.query)

        if url.path == PAGE_PATH:
            self._serve_page()
            return

        if not url.path.startswith(API_PREFIX):
            self._json(404, {"error": "not found"})
            return

        token = presented_token(query, self.headers.get("Authorization", ""))
        if not is_scoped_request(url.path, token, EXPECTED_TOKEN):
            self._json(401, {"error": "unauthorized"})
            return

        route = url.path[len(API_PREFIX):]
        if route == "status":
            self._json(200, {"items": load_status(), "server_time": time.time()})
        elif route == "stream":
            self._stream(query.get("id", [""])[0])
        else:
            self._json(404, {"error": "not found"})

    def _serve_page(self) -> None:
        try:
            body = PAGE_FILE.read_bytes()
        except OSError:
            self._json(500, {"error": "page file missing"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _stream(self, item_id: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        last_text = None
        last_beat = time.monotonic()
        try:
            while True:
                frame = snapshot(item_id)
                if frame.get("error"):
                    self._emit("error", frame)
                    return
                if frame["text"] != last_text:
                    last_text = frame["text"]
                    self._emit("snapshot", frame)
                    last_beat = time.monotonic()
                elif time.monotonic() - last_beat >= STREAM_HEARTBEAT_SECONDS:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    last_beat = time.monotonic()
                time.sleep(STREAM_POLL_SECONDS)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _emit(self, event: str, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False)
        self.wfile.write(f"event: {event}\ndata: {data}\n\n".encode("utf-8"))
        self.wfile.flush()


def main() -> None:
    if not EXPECTED_TOKEN:
        raise SystemExit("MINI_APP_TOKEN is empty; generate one with: openssl rand -hex 24")
    server = ThreadingHTTPServer((BIND_HOST, PORT), Handler)
    print(f"mini app on http://{BIND_HOST}:{PORT}{PAGE_PATH}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()

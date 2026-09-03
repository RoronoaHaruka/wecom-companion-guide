#!/usr/bin/env python3
# Copyright (c) 2026 Roronoa & Haruka · From Raincove ♡
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Poll one WeChat Customer Service account into an existing agent queue."""

from __future__ import annotations

import json
import mimetypes
import os
import sqlite3
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path


API = "https://qyapi.weixin.qq.com/cgi-bin"
MEDIA_TYPES = {"image", "voice", "video", "file"}


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"missing required environment variable: {name}")
    return value


def safe_part(value: str, fallback: str = "item") -> str:
    cleaned = "".join(character for character in value if character.isalnum() or character in "._-")
    return cleaned[:100] or fallback


@dataclass(frozen=True)
class Config:
    corp_id: str
    app_secret: str
    open_kfid: str
    route_agent: str
    queue_dir: Path
    data_dir: Path
    state_db: Path
    expected_external_userid: str = ""
    poll_seconds: float = 15.0
    max_media_bytes: int = 25 * 1024 * 1024

    def __post_init__(self) -> None:
        if self.poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        if self.max_media_bytes <= 0:
            raise ValueError("max_media_bytes must be positive")

    @classmethod
    def from_env(cls) -> "Config":
        data_dir = Path(os.environ.get("WECOM_DATA_DIR", "/var/lib/wecom-agent"))
        return cls(
            corp_id=required_env("WECOM_CORP_ID"),
            app_secret=required_env("WECOM_APP_SECRET"),
            open_kfid=required_env("WECOM_OPEN_KFID"),
            route_agent=os.environ.get("WECOM_ROUTE_AGENT", "agent_default"),
            queue_dir=Path(os.environ.get("WECOM_QUEUE_DIR", str(data_dir / "queue"))),
            data_dir=data_dir,
            state_db=Path(os.environ.get("WECOM_STATE_DB", str(data_dir / "state.sqlite3"))),
            expected_external_userid=os.environ.get("WECOM_KF_EXTERNAL_USER_ID", "").strip(),
            poll_seconds=float(os.environ.get("WECOM_KF_POLL_SECONDS", "15")),
            max_media_bytes=int(os.environ.get("WECOM_MAX_MEDIA_BYTES", str(25 * 1024 * 1024))),
        )


class Store:
    def __init__(self, config: Config):
        self.config = config
        self.raw_dir = config.data_dir / "raw" / safe_part(config.open_kfid, "customer-service")
        self.media_dir = config.data_dir / "media" / safe_part(config.open_kfid, "customer-service")
        for directory in (config.data_dir, config.queue_dir, self.raw_dir, self.media_dir, config.state_db.parent):
            directory.mkdir(parents=True, exist_ok=True)
            os.chmod(directory, 0o700)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(config.state_db, timeout=30, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS bridge_state(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS seen_kf_messages "
            "(msg_id TEXT PRIMARY KEY, received_at INTEGER NOT NULL)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS kf_bindings "
            "(open_kfid TEXT PRIMARY KEY, external_userid TEXT NOT NULL)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS reply_budget "
            "(route_key TEXT PRIMARY KEY, source_msg_id TEXT NOT NULL, remaining INTEGER NOT NULL, updated_at INTEGER NOT NULL)"
        )
        self.db.commit()
        os.chmod(config.state_db, 0o600)

    def state_get(self, key: str) -> str:
        with self.lock:
            row = self.db.execute("SELECT value FROM bridge_state WHERE key=?", (key,)).fetchone()
            return row[0] if row else ""

    def state_set(self, key: str, value: str) -> None:
        with self.lock:
            self.db.execute(
                "INSERT INTO bridge_state(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            self.db.commit()

    def seen(self, msg_id: str) -> bool:
        with self.lock:
            return self.db.execute(
                "SELECT 1 FROM seen_kf_messages WHERE msg_id=?", (msg_id,)
            ).fetchone() is not None

    def bound_customer(self) -> str:
        with self.lock:
            row = self.db.execute(
                "SELECT external_userid FROM kf_bindings WHERE open_kfid=?",
                (self.config.open_kfid,),
            ).fetchone()
            return row[0] if row else ""

    def accept_customer(self, external_userid: str) -> bool:
        expected = self.config.expected_external_userid
        if expected and external_userid != expected:
            return False
        with self.lock:
            bound = self.bound_customer()
            if bound:
                return external_userid == bound
            self.db.execute(
                "INSERT INTO kf_bindings(open_kfid,external_userid) VALUES(?,?)",
                (self.config.open_kfid, external_userid),
            )
            self.db.commit()
            return True

    def save_raw(self, message: dict) -> None:
        path = self.raw_dir / f"{safe_part(message.get('msgid', ''), 'message')}.json"
        self.atomic_json(path, message)

    def enqueue_and_commit(self, envelope: dict) -> None:
        msg_id = envelope["msg_id"]
        destination = self.config.queue_dir / f"msg_kf_{safe_part(msg_id)}.json"
        with self.lock:
            if self.seen(msg_id):
                return
            self.atomic_json(destination, envelope)
            route_key = f"{self.config.open_kfid}:{envelope['from_user']}"
            now = int(time.time())
            self.db.execute(
                "INSERT OR IGNORE INTO seen_kf_messages(msg_id,received_at) VALUES(?,?)",
                (msg_id, now),
            )
            self.db.execute(
                "INSERT INTO reply_budget(route_key,source_msg_id,remaining,updated_at) VALUES(?,?,5,?) "
                "ON CONFLICT(route_key) DO UPDATE SET source_msg_id=excluded.source_msg_id,remaining=5,updated_at=excluded.updated_at",
                (route_key, msg_id, now),
            )
            self.db.commit()

    @staticmethod
    def atomic_json(path: Path, value: dict) -> None:
        fd, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent, text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


class CustomerServiceBridge:
    def __init__(self, config: Config):
        self.config = config
        self.store = Store(config)
        self._token = ""
        self._token_expiry = 0.0

    @staticmethod
    def request_json(method: str, url: str, body: dict | None = None) -> dict:
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"} if data is not None else {},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
        if result.get("errcode") not in (None, 0):
            raise RuntimeError(f"WeCom API {result.get('errcode')}: {result.get('errmsg', '')}")
        return result

    def access_token(self) -> str:
        if self._token and time.monotonic() < self._token_expiry:
            return self._token
        query = urllib.parse.urlencode({
            "corpid": self.config.corp_id,
            "corpsecret": self.config.app_secret,
        })
        result = self.request_json("GET", f"{API}/gettoken?{query}")
        self._token = result["access_token"]
        self._token_expiry = time.monotonic() + max(60, int(result.get("expires_in", 7200)) - 300)
        return self._token

    def download_media(self, token: str, media_id: str, stem: str) -> str:
        query = urllib.parse.urlencode({"access_token": token, "media_id": media_id})
        request = urllib.request.Request(f"{API}/media/get?{query}")
        with urllib.request.urlopen(request, timeout=60) as response:
            declared_size = int(response.headers.get("Content-Length") or 0)
            if declared_size > self.config.max_media_bytes:
                raise ValueError("media file exceeds WECOM_MAX_MEDIA_BYTES")
            body = response.read(self.config.max_media_bytes + 1)
            if len(body) > self.config.max_media_bytes:
                raise ValueError("media file exceeds WECOM_MAX_MEDIA_BYTES")
            content_type = (response.headers.get("Content-Type") or "").split(";", 1)[0]
        if content_type == "application/json" or body[:1] == b"{":
            result = json.loads(body.decode("utf-8"))
            raise RuntimeError(f"media/get {result.get('errcode')}: {result.get('errmsg', '')}")
        extension = mimetypes.guess_extension(content_type) or ".bin"
        destination = self.store.media_dir / f"{safe_part(stem)}{extension}"
        fd, temporary = tempfile.mkstemp(prefix=".media-", dir=self.store.media_dir)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, destination)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
        return str(destination)

    @staticmethod
    def decoded_payload(item: dict) -> tuple[str, dict]:
        kind = str(item.get("msgtype") or "unknown")
        raw = item.get("msg_content") or "{}"
        decoded = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(decoded, dict):
            decoded = {}
        payload = decoded.get(kind) if isinstance(decoded.get(kind), dict) else decoded
        return kind, payload

    def render_payload(self, kind: str, payload: dict, token: str, stem: str) -> tuple[str, list[str]]:
        if kind == "text":
            return str(payload.get("content") or ""), []
        if kind == "link":
            text = "\n".join(
                value for value in (
                    f"[链接] {payload.get('title', '')}",
                    str(payload.get("desc") or ""),
                    str(payload.get("url") or ""),
                ) if value
            )
            return text, []
        if kind in MEDIA_TYPES and payload.get("media_id"):
            path = self.download_media(token, str(payload["media_id"]), f"{stem}_{kind}")
            return f"[{kind}] {path}", [path]
        return f"[{kind}] {json.dumps(payload, ensure_ascii=False)}", []

    def normalize(self, message: dict, token: str) -> dict:
        msg_id = str(message["msgid"])
        kind = str(message.get("msgtype") or "unknown")
        media_paths: list[str] = []
        structured: dict = {}
        if kind == "merged_msg":
            merged = message.get("merged_msg") or {}
            lines = [f"[合并聊天记录：{merged.get('title', '聊天记录')}]" ]
            items = []
            for index, item in enumerate(merged.get("item") or [], start=1):
                child_kind, payload = self.decoded_payload(item)
                text, paths = self.render_payload(child_kind, payload, token, f"{msg_id}_{index}")
                media_paths.extend(paths)
                child = {
                    "index": index,
                    "send_time": item.get("send_time"),
                    "sender_name": item.get("sender_name") or "未知发送者",
                    "msgtype": child_kind,
                    "content": payload,
                    "media_paths": paths,
                }
                items.append(child)
                lines.extend([f"{index}. {child['sender_name']}", text])
            content = "\n".join(lines)
            structured = {"title": merged.get("title") or "聊天记录", "items": items}
        else:
            payload = message.get(kind) if isinstance(message.get(kind), dict) else {}
            content, media_paths = self.render_payload(kind, payload, token, msg_id)
            structured = payload

        external_userid = str(message["external_userid"])
        return {
            "source": "wecom_kf",
            "reply_channel": "wecom_kf",
            "route_agent": self.config.route_agent,
            "msg_id": msg_id,
            "msg_type": "text",
            "source_msgtype": kind,
            "from_user": external_userid,
            "content": content,
            "timestamp": int(message.get("send_time") or time.time()),
            "structured_content": structured,
            "media_paths": media_paths,
            "reply": {
                "kind": "kf",
                "open_kfid": self.config.open_kfid,
                "user_id": external_userid,
            },
        }

    def sync_once(self) -> int:
        token = self.access_token()
        cursor_key = f"kf_cursor:{self.config.open_kfid}"
        cursor = self.store.state_get(cursor_key)
        accepted = 0
        while True:
            query = urllib.parse.urlencode({"access_token": token})
            result = self.request_json(
                "POST",
                f"{API}/kf/sync_msg?{query}",
                {
                    "cursor": cursor,
                    "limit": 100,
                    "voice_format": 0,
                    "open_kfid": self.config.open_kfid,
                },
            )
            for message in result.get("msg_list") or []:
                if int(message.get("origin") or 0) != 3:
                    continue
                msg_id = str(message.get("msgid") or "")
                external_userid = str(message.get("external_userid") or "")
                if not msg_id or not external_userid or self.store.seen(msg_id):
                    continue
                if not self.store.accept_customer(external_userid):
                    continue
                self.store.save_raw(message)
                envelope = self.normalize(message, token)
                self.store.enqueue_and_commit(envelope)
                accepted += 1
            next_cursor = str(result.get("next_cursor") or cursor)
            if result.get("has_more") and next_cursor == cursor:
                raise RuntimeError("sync_msg returned has_more without advancing next_cursor")
            cursor = next_cursor
            self.store.state_set(cursor_key, cursor)
            if not result.get("has_more"):
                return accepted

    def run(self) -> None:
        while True:
            try:
                self.sync_once()
            except Exception as error:
                print(f"customer-service sync failed: {error}", flush=True)
            time.sleep(self.config.poll_seconds)


def main() -> None:
    CustomerServiceBridge(Config.from_env()).run()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# Copyright (c) 2026 Roronoa & Haruka · From Raincove ♡
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Receive encrypted WeCom application callbacks and atomically enqueue them."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import sqlite3
import struct
import tempfile
import threading
import time
import xml.etree.ElementTree as StdET
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from Crypto.Cipher import AES
from defusedxml import ElementTree as DefusedET
from defusedxml.common import DefusedXmlException


MAX_BODY_BYTES = 1024 * 1024


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"missing required environment variable: {name}")
    return value


@dataclass(frozen=True)
class Config:
    corp_id: str
    callback_token: str
    encoding_aes_key: str
    agent_id: int
    queue_dir: Path
    state_db: Path
    allowed_user_ids: frozenset[str]
    bind_host: str = "127.0.0.1"
    port: int = 8765
    verify_file: Path | None = None

    @classmethod
    def from_env(cls) -> "Config":
        queue_dir = Path(os.environ.get("WECOM_QUEUE_DIR", "/var/lib/wecom-agent/queue"))
        state_db = Path(os.environ.get("WECOM_STATE_DB", "/var/lib/wecom-agent/state.sqlite3"))
        verify_value = os.environ.get("WECOM_VERIFY_FILE", "").strip()
        verify_file = Path(verify_value) if verify_value else None
        if verify_file and not re.fullmatch(r"WW_verify_[A-Za-z0-9_-]+\.txt", verify_file.name):
            raise RuntimeError("WECOM_VERIFY_FILE must name a WW_verify_*.txt file")
        allowed = frozenset(
            value.strip()
            for value in os.environ.get("WECOM_ALLOWED_USER_IDS", "").split(",")
            if value.strip()
        )
        return cls(
            corp_id=required_env("WECOM_CORP_ID"),
            callback_token=required_env("WECOM_CALLBACK_TOKEN"),
            encoding_aes_key=required_env("WECOM_ENCODING_AES_KEY"),
            agent_id=int(required_env("WECOM_AGENT_ID")),
            queue_dir=queue_dir,
            state_db=state_db,
            allowed_user_ids=allowed,
            bind_host=os.environ.get("WECOM_BIND_HOST", "127.0.0.1"),
            port=int(os.environ.get("WECOM_PORT", "8765")),
            verify_file=verify_file,
        )


class WeComCrypto:
    def __init__(self, token: str, encoding_aes_key: str, corp_id: str):
        try:
            key = base64.b64decode(encoding_aes_key + "=", validate=True)
        except Exception as error:
            raise ValueError("EncodingAESKey is not valid base64") from error
        if len(key) != 32:
            raise ValueError("EncodingAESKey must decode to 32 bytes")
        self.token = token
        self.key = key
        self.corp_id = corp_id.encode("utf-8")

    def signature(self, timestamp: str, nonce: str, encrypted: str) -> str:
        parts = sorted([self.token, timestamp, nonce, encrypted])
        return hashlib.sha1("".join(parts).encode("utf-8")).hexdigest()

    def valid_signature(self, supplied: str, timestamp: str, nonce: str, encrypted: str) -> bool:
        expected = self.signature(timestamp, nonce, encrypted)
        return bool(supplied) and hmac.compare_digest(supplied, expected)

    def decrypt(self, encrypted: str) -> str:
        try:
            ciphertext = base64.b64decode(encrypted, validate=True)
        except Exception as error:
            raise ValueError("encrypted payload is not valid base64") from error
        if not ciphertext or len(ciphertext) % AES.block_size:
            raise ValueError("encrypted payload has an invalid block length")

        padded = AES.new(self.key, AES.MODE_CBC, self.key[:16]).decrypt(ciphertext)
        padding = padded[-1]
        if not 1 <= padding <= 32 or padded[-padding:] != bytes([padding]) * padding:
            raise ValueError("invalid PKCS7 padding")
        content = padded[:-padding]
        if len(content) < 20:
            raise ValueError("decrypted payload is too short")

        message_length = struct.unpack("!I", content[16:20])[0]
        message_end = 20 + message_length
        if message_end > len(content):
            raise ValueError("decrypted message length is invalid")
        corp_id = content[message_end:]
        if not hmac.compare_digest(corp_id, self.corp_id):
            raise ValueError("CorpID mismatch")
        return content[20:message_end].decode("utf-8")


def parse_xml(xml_text: str) -> StdET.Element:
    try:
        return DefusedET.fromstring(xml_text)
    except (StdET.ParseError, DefusedXmlException) as error:
        raise ValueError("invalid XML") from error


def field(root: StdET.Element, name: str) -> str:
    return (root.findtext(name) or "").strip()


class Inbox:
    def __init__(self, queue_dir: Path, state_db: Path):
        self.queue_dir = queue_dir
        self.state_db = state_db
        queue_dir.mkdir(parents=True, exist_ok=True)
        state_db.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(queue_dir, 0o700)
        os.chmod(state_db.parent, 0o700)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(state_db, timeout=30, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS seen_app_messages "
            "(msg_id TEXT PRIMARY KEY, received_at INTEGER NOT NULL)"
        )
        self.db.commit()
        os.chmod(state_db, 0o600)

    def seen(self, msg_id: str) -> bool:
        with self.lock:
            return self.db.execute(
                "SELECT 1 FROM seen_app_messages WHERE msg_id=?", (msg_id,)
            ).fetchone() is not None

    def enqueue(self, envelope: dict) -> bool:
        msg_id = envelope["msg_id"]
        with self.lock:
            if self.seen(msg_id):
                return False

            digest = hashlib.sha256(msg_id.encode("utf-8")).hexdigest()[:24]
            destination = self.queue_dir / f"msg_app_{digest}.json"
            fd, temporary = tempfile.mkstemp(prefix=".incoming-", dir=self.queue_dir, text=True)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(envelope, handle, ensure_ascii=False)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(temporary, 0o600)
                os.replace(temporary, destination)
                self.db.execute(
                    "INSERT OR IGNORE INTO seen_app_messages(msg_id,received_at) VALUES(?,?)",
                    (msg_id, int(time.time())),
                )
                self.db.commit()
                return True
            finally:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass


def message_envelope(root: StdET.Element, config: Config) -> dict:
    sender = field(root, "FromUserName")
    msg_type = field(root, "MsgType") or "unknown"
    msg_id = field(root, "MsgId")
    if not msg_id:
        basis = StdET.tostring(root, encoding="utf-8")
        msg_id = "sha256:" + hashlib.sha256(basis).hexdigest()

    structured = {
        key: value
        for key in (
            "MediaId", "PicUrl", "Format", "Recognition", "Event", "EventKey",
            "Latitude", "Longitude", "Scale", "Label", "Title", "Description", "Url",
        )
        if (value := field(root, key))
    }
    return {
        "source": "wecom_app",
        "reply_channel": "wecom_app",
        "msg_id": msg_id,
        "msg_type": msg_type,
        "from_user": sender,
        "content": field(root, "Content"),
        "timestamp": int(field(root, "CreateTime") or time.time()),
        "structured_content": structured,
        "reply": {
            "kind": "app",
            "agent_id": config.agent_id,
            "user_id": sender,
        },
    }


def make_handler(config: Config, crypto: WeComCrypto, inbox: Inbox):
    class Handler(BaseHTTPRequestHandler):
        server_version = "WeComBridge/1.1"

        def send_text(self, status: int, text: str) -> None:
            body = text.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def query(self) -> dict[str, str]:
            values = parse_qs(urlparse(self.path).query, keep_blank_values=True)
            return {key: items[0] for key, items in values.items() if items}

        def do_GET(self) -> None:
            request_path = urlparse(self.path).path
            if config.verify_file and request_path == f"/{config.verify_file.name}":
                try:
                    if not config.verify_file.is_file() or config.verify_file.stat().st_size > 4096:
                        raise OSError("verification file is missing or too large")
                    self.send_text(200, config.verify_file.read_text(encoding="utf-8"))
                except (OSError, UnicodeError):
                    self.send_text(404, "not found")
                return

            query = self.query()
            signature = query.get("msg_signature", "")
            timestamp = query.get("timestamp", "")
            nonce = query.get("nonce", "")
            encrypted = query.get("echostr", "")
            if not crypto.valid_signature(signature, timestamp, nonce, encrypted):
                self.send_text(403, "forbidden")
                return
            try:
                self.send_text(200, crypto.decrypt(encrypted))
            except ValueError:
                self.send_text(400, "invalid payload")

        def do_POST(self) -> None:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self.send_text(400, "invalid content length")
                return
            if not 0 < length <= MAX_BODY_BYTES:
                self.send_text(413, "payload too large")
                return

            try:
                outer = parse_xml(self.rfile.read(length).decode("utf-8"))
                encrypted = field(outer, "Encrypt")
                query = self.query()
                if not crypto.valid_signature(
                    query.get("msg_signature", ""),
                    query.get("timestamp", ""),
                    query.get("nonce", ""),
                    encrypted,
                ):
                    self.send_text(403, "forbidden")
                    return
                root = parse_xml(crypto.decrypt(encrypted))
            except (UnicodeDecodeError, ValueError):
                self.send_text(400, "invalid payload")
                return

            sender = field(root, "FromUserName")
            callback_agent_id = field(root, "AgentID")
            if callback_agent_id != str(config.agent_id):
                self.send_text(403, "wrong agent")
                return
            if not sender:
                self.send_text(400, "missing sender")
                return
            if config.allowed_user_ids and sender not in config.allowed_user_ids:
                self.send_text(200, "success")
                return
            if field(root, "MsgType") == "event":
                self.send_text(200, "success")
                return

            try:
                envelope = message_envelope(root, config)
            except (OverflowError, ValueError):
                self.send_text(400, "invalid message")
                return
            try:
                inbox.enqueue(envelope)
            except Exception as error:
                print(f"queue write failed: {type(error).__name__}", flush=True)
                self.send_text(500, "temporary failure")
                return
            self.send_text(200, "success")

        def log_message(self, format_string: str, *args) -> None:
            print(f"[{self.log_date_time_string()}] {format_string % args}")

    return Handler


def main() -> None:
    config = Config.from_env()
    crypto = WeComCrypto(config.callback_token, config.encoding_aes_key, config.corp_id)
    inbox = Inbox(config.queue_dir, config.state_db)
    server = ThreadingHTTPServer((config.bind_host, config.port), make_handler(config, crypto, inbox))
    print(f"WeCom application callback listening on {config.bind_host}:{config.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()

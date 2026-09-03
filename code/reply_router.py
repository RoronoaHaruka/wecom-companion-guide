#!/usr/bin/env python3
# Copyright (c) 2026 Roronoa & Haruka · From Raincove ♡
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Return a reply through the same WeCom application or customer-service door."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Callable

from sender import ApplicationSender, Config as AppConfig, WeComAPIError, split_utf8


API = "https://qyapi.weixin.qq.com/cgi-bin"


class ReplyRouter:
    def __init__(self):
        self.app_sender = ApplicationSender(AppConfig.from_env())
        self.open_kfid = os.environ.get("WECOM_OPEN_KFID", "").strip()
        self.state_db = Path(os.environ.get("WECOM_STATE_DB", "/var/lib/wecom-agent/state.sqlite3"))
        self.kf_max_bytes = int(os.environ.get("WECOM_KF_TEXT_BYTES", "2000"))

    def send(
        self,
        envelope: dict,
        text: str,
        *,
        start_index: int = 0,
        on_sent: Callable[[int], None] | None = None,
    ) -> int:
        reply = envelope.get("reply") or {}
        kind = reply.get("kind")
        if kind == "app":
            return self.app_sender.send_text(
                str(reply.get("user_id") or ""),
                text,
                start_index=start_index,
                on_sent=on_sent,
            )
        if kind == "kf":
            return self.send_customer_service(
                reply,
                text,
                source_msg_id=str(envelope.get("msg_id") or ""),
                start_index=start_index,
                on_sent=on_sent,
            )
        raise ValueError("envelope has no recognized reply route")

    def send_customer_service(
        self,
        reply: dict,
        text: str,
        *,
        source_msg_id: str,
        start_index: int = 0,
        on_sent: Callable[[int], None] | None = None,
    ) -> int:
        open_kfid = str(reply.get("open_kfid") or "")
        user_id = str(reply.get("user_id") or "")
        if not self.open_kfid or open_kfid != self.open_kfid:
            raise PermissionError("customer-service account is not allowlisted")
        if not self.bound_customer(open_kfid, user_id):
            raise PermissionError("customer-service user is not the bound customer")

        chunks = split_utf8(text, self.kf_max_bytes)
        if not chunks:
            raise ValueError("message text is empty")
        if len(chunks) > 5:
            raise ValueError("one customer-service reply cannot exceed five message chunks")
        if not 0 <= start_index <= len(chunks):
            raise ValueError("invalid customer-service reply progress")

        token = self.app_sender.access_token()
        for index, chunk in enumerate(chunks[start_index:], start=start_index):
            budget_msg_id = self.reserve_budget(open_kfid, user_id, 1)
            message_id = self.customer_service_message_id(
                open_kfid,
                user_id,
                source_msg_id or budget_msg_id,
                text,
                index,
            )
            query = urllib.parse.urlencode({"access_token": token})
            try:
                self.app_sender.request_json(
                    "POST",
                    f"{API}/kf/send_msg?{query}",
                    {
                        "touser": user_id,
                        "open_kfid": open_kfid,
                        "msgid": message_id,
                        "msgtype": "text",
                        "text": {"content": chunk},
                    },
                )
            except WeComAPIError as error:
                if error.code != 95033:
                    self.restore_budget(open_kfid, user_id, budget_msg_id, 1)
                    raise
            except Exception:
                self.restore_budget(open_kfid, user_id, budget_msg_id, 1)
                raise
            if on_sent is not None:
                on_sent(index + 1)
        return len(chunks)

    @staticmethod
    def customer_service_message_id(
        open_kfid: str,
        user_id: str,
        source_msg_id: str,
        text: str,
        index: int,
    ) -> str:
        material = "\0".join((open_kfid, user_id, source_msg_id, text, str(index)))
        return "r" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:31]

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.state_db, timeout=30)
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def bound_customer(self, open_kfid: str, user_id: str) -> bool:
        with self.connect() as db:
            row = db.execute(
                "SELECT external_userid FROM kf_bindings WHERE open_kfid=?",
                (open_kfid,),
            ).fetchone()
        return bool(row and row[0] == user_id)

    @staticmethod
    def route_key(open_kfid: str, user_id: str) -> str:
        return f"{open_kfid}:{user_id}"

    def reserve_budget(self, open_kfid: str, user_id: str, count: int) -> str:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT source_msg_id,remaining FROM reply_budget WHERE route_key=?",
                (self.route_key(open_kfid, user_id),),
            ).fetchone()
            if not row:
                raise RuntimeError("no customer message has opened a reply window")
            source_msg_id, remaining = row
            if remaining < count:
                raise RuntimeError(
                    f"customer-service reply budget has {remaining} slot(s), but this reply needs {count}"
                )
            db.execute(
                "UPDATE reply_budget SET remaining=?,updated_at=? WHERE route_key=?",
                (remaining - count, int(time.time()), self.route_key(open_kfid, user_id)),
            )
            return str(source_msg_id)

    def restore_budget(
        self,
        open_kfid: str,
        user_id: str,
        source_msg_id: str,
        count: int,
    ) -> None:
        if count <= 0:
            return
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT source_msg_id,remaining FROM reply_budget WHERE route_key=?",
                (self.route_key(open_kfid, user_id),),
            ).fetchone()
            if not row or str(row[0]) != source_msg_id:
                return
            db.execute(
                "UPDATE reply_budget SET remaining=?,updated_at=? WHERE route_key=?",
                (min(5, int(row[1]) + count), int(time.time()), self.route_key(open_kfid, user_id)),
            )


_default_router: ReplyRouter | None = None


def send_reply(
    envelope: dict,
    text: str,
    *,
    start_index: int = 0,
    on_sent: Callable[[int], None] | None = None,
) -> int:
    global _default_router
    if _default_router is None:
        _default_router = ReplyRouter()
    return _default_router.send(
        envelope,
        text,
        start_index=start_index,
        on_sent=on_sent,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--envelope", required=True, help="queued route-envelope JSON file")
    parser.add_argument("--text", help="reply text; stdin is used when omitted")
    args = parser.parse_args()
    envelope = json.loads(Path(args.envelope).read_text(encoding="utf-8"))
    text = args.text if args.text is not None else sys.stdin.read()
    print(json.dumps({"sent": send_reply(envelope, text)}, ensure_ascii=False))


if __name__ == "__main__":
    main()

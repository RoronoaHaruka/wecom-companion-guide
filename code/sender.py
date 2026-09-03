#!/usr/bin/env python3
# Copyright (c) 2026 Roronoa & Haruka · From Raincove ♡
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Send allowlisted WeCom application messages with cached credentials."""

from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable


API = "https://qyapi.weixin.qq.com/cgi-bin"


class WeComAPIError(RuntimeError):
    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(f"WeCom API {code}: {message}")


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"missing required environment variable: {name}")
    return value


def split_utf8(text: str, max_bytes: int = 1800) -> list[str]:
    if not text:
        return []
    if len(text.encode("utf-8")) <= max_bytes:
        return [text]

    chunks: list[str] = []
    remaining = text
    while remaining:
        used = 0
        boundary = 0
        preferred = 0
        for index, character in enumerate(remaining, start=1):
            next_size = used + len(character.encode("utf-8"))
            if next_size > max_bytes:
                break
            used = next_size
            boundary = index
            if character in "\n。！？；":
                preferred = index
        if boundary == 0:
            raise ValueError("max_bytes is too small for one Unicode character")
        split_at = preferred if preferred >= boundary * 0.55 else boundary
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:]
    return chunks


@dataclass(frozen=True)
class Config:
    corp_id: str
    secret: str
    agent_id: int
    allowed_user_ids: frozenset[str]

    @classmethod
    def from_env(cls) -> "Config":
        allowed = frozenset(
            value.strip()
            for value in required_env("WECOM_ALLOWED_USER_IDS").split(",")
            if value.strip()
        )
        if not allowed:
            raise RuntimeError("WECOM_ALLOWED_USER_IDS must contain at least one UserID")
        return cls(
            corp_id=required_env("WECOM_CORP_ID"),
            secret=required_env("WECOM_APP_SECRET"),
            agent_id=int(required_env("WECOM_AGENT_ID")),
            allowed_user_ids=allowed,
        )


class ApplicationSender:
    def __init__(self, config: Config):
        self.config = config
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
            raise WeComAPIError(int(result["errcode"]), str(result.get("errmsg") or ""))
        return result

    def access_token(self) -> str:
        if self._token and time.monotonic() < self._token_expiry:
            return self._token
        query = urllib.parse.urlencode({
            "corpid": self.config.corp_id,
            "corpsecret": self.config.secret,
        })
        result = self.request_json("GET", f"{API}/gettoken?{query}")
        self._token = result["access_token"]
        self._token_expiry = time.monotonic() + max(60, int(result.get("expires_in", 7200)) - 300)
        return self._token

    def send_text(
        self,
        user_id: str,
        text: str,
        *,
        start_index: int = 0,
        on_sent: Callable[[int], None] | None = None,
    ) -> int:
        if user_id not in self.config.allowed_user_ids:
            raise PermissionError("target UserID is not allowlisted")
        chunks = split_utf8(text)
        if not chunks:
            raise ValueError("message text is empty")
        if not 0 <= start_index <= len(chunks):
            raise ValueError("invalid application reply progress")
        token = self.access_token()
        for index, chunk in enumerate(chunks[start_index:], start=start_index):
            query = urllib.parse.urlencode({"access_token": token})
            self.request_json(
                "POST",
                f"{API}/message/send?{query}",
                {
                    "touser": user_id,
                    "msgtype": "text",
                    "agentid": self.config.agent_id,
                    "text": {"content": chunk},
                },
            )
            if on_sent is not None:
                on_sent(index + 1)
        return len(chunks)


_default_sender: ApplicationSender | None = None


def send_text(user_id: str, text: str) -> int:
    global _default_sender
    if _default_sender is None:
        _default_sender = ApplicationSender(Config.from_env())
    return _default_sender.send_text(user_id, text)

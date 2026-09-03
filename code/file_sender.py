#!/usr/bin/env python3
# Copyright (c) 2026 Roronoa & Haruka · From Raincove ♡
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Send one allowlisted local file through the application or customer-service door."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path

from reply_router import ReplyRouter
from sender import WeComAPIError, required_env


API = "https://qyapi.weixin.qq.com/cgi-bin"
DEFAULT_SUFFIXES = ".pdf,.txt,.md,.csv,.json,.png,.jpg,.jpeg,.gif,.mp3,.amr,.mp4,.zip"
MIN_FILE_BYTES = 6
API_MAX_FILE_BYTES = 20 * 1024 * 1024


@dataclass(frozen=True)
class FilePolicy:
    allowed_dirs: tuple[Path, ...]
    allowed_suffixes: frozenset[str]
    max_bytes: int

    @classmethod
    def from_env(cls) -> "FilePolicy":
        directories = tuple(
            Path(value.strip()).resolve()
            for value in required_env("WECOM_FILE_ALLOWED_DIRS").split(",")
            if value.strip()
        )
        if not directories:
            raise RuntimeError("WECOM_FILE_ALLOWED_DIRS must contain at least one directory")
        suffixes = frozenset(
            value.strip().lower()
            for value in os.environ.get("WECOM_FILE_ALLOWED_SUFFIXES", DEFAULT_SUFFIXES).split(",")
            if value.strip().startswith(".")
        )
        if not suffixes:
            raise RuntimeError("WECOM_FILE_ALLOWED_SUFFIXES must contain at least one dotted suffix")
        max_bytes = int(os.environ.get("WECOM_FILE_MAX_BYTES", str(API_MAX_FILE_BYTES)))
        if not 0 < max_bytes <= API_MAX_FILE_BYTES:
            raise ValueError("WECOM_FILE_MAX_BYTES must stay within the 20MB upload-API cap")
        return cls(allowed_dirs=directories, allowed_suffixes=suffixes, max_bytes=max_bytes)

    def validate(self, path: str | Path) -> Path:
        resolved = Path(path).resolve(strict=True)
        if not resolved.is_file():
            raise ValueError("only regular files can be sent")
        if not any(resolved.is_relative_to(root) for root in self.allowed_dirs):
            raise PermissionError("file is outside WECOM_FILE_ALLOWED_DIRS")
        if resolved.suffix.lower() not in self.allowed_suffixes:
            raise ValueError("file suffix is not in WECOM_FILE_ALLOWED_SUFFIXES")
        size = resolved.stat().st_size
        if size < MIN_FILE_BYTES:
            raise ValueError("the upload API rejects files smaller than 6 bytes")
        if size > self.max_bytes:
            raise ValueError("file exceeds WECOM_FILE_MAX_BYTES")
        return resolved


class FileSender:
    def __init__(self, policy: FilePolicy, router: ReplyRouter):
        self.policy = policy
        self.router = router

    @staticmethod
    def multipart(field_name: str, filename: str, body: bytes) -> tuple[bytes, str]:
        boundary = uuid.uuid4().hex
        head = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode("utf-8")
        tail = f"\r\n--{boundary}--\r\n".encode("utf-8")
        return head + body + tail, f"multipart/form-data; boundary={boundary}"

    def upload(self, token: str, path: Path) -> str:
        payload, content_type = self.multipart("media", path.name, path.read_bytes())
        query = urllib.parse.urlencode({"access_token": token, "type": "file"})
        request = urllib.request.Request(
            f"{API}/media/upload?{query}",
            data=payload,
            method="POST",
            headers={"Content-Type": content_type},
        )
        with urllib.request.urlopen(request, timeout=120) as response:
            result = json.loads(response.read().decode("utf-8"))
        if result.get("errcode") not in (None, 0):
            raise WeComAPIError(int(result["errcode"]), str(result.get("errmsg") or ""))
        return str(result["media_id"])

    def send_app_file(self, user_id: str, path: str | Path) -> str:
        approved = self.policy.validate(path)
        app = self.router.app_sender
        if user_id not in app.config.allowed_user_ids:
            raise PermissionError("target UserID is not allowlisted")
        token = app.access_token()
        media_id = self.upload(token, approved)
        query = urllib.parse.urlencode({"access_token": token})
        app.request_json(
            "POST",
            f"{API}/message/send?{query}",
            {
                "touser": user_id,
                "msgtype": "file",
                "agentid": app.config.agent_id,
                "file": {"media_id": media_id},
            },
        )
        return media_id

    def send_kf_file(
        self,
        open_kfid: str,
        user_id: str,
        path: str | Path,
        *,
        source_msg_id: str = "",
    ) -> str:
        approved = self.policy.validate(path)
        router = self.router
        if not router.open_kfid or open_kfid != router.open_kfid:
            raise PermissionError("customer-service account is not allowlisted")
        if not router.bound_customer(open_kfid, user_id):
            raise PermissionError("customer-service user is not the bound customer")
        token = router.app_sender.access_token()
        media_id = self.upload(token, approved)
        budget_msg_id = router.reserve_budget(open_kfid, user_id, 1)
        stat = approved.stat()
        material = "\0".join((
            open_kfid,
            user_id,
            source_msg_id or budget_msg_id,
            str(approved),
            str(stat.st_size),
            str(stat.st_mtime_ns),
        ))
        message_id = "f" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:31]
        query = urllib.parse.urlencode({"access_token": token})
        try:
            router.app_sender.request_json(
                "POST",
                f"{API}/kf/send_msg?{query}",
                {
                    "touser": user_id,
                    "open_kfid": open_kfid,
                    "msgid": message_id,
                    "msgtype": "file",
                    "file": {"media_id": media_id},
                },
            )
        except WeComAPIError as error:
            if error.code != 95033:
                router.restore_budget(open_kfid, user_id, budget_msg_id, 1)
                raise
        except Exception:
            router.restore_budget(open_kfid, user_id, budget_msg_id, 1)
            raise
        return media_id

    def send(self, envelope: dict, path: str | Path) -> str:
        reply = envelope.get("reply") or {}
        kind = reply.get("kind")
        if kind == "app":
            return self.send_app_file(str(reply.get("user_id") or ""), path)
        if kind == "kf":
            return self.send_kf_file(
                str(reply.get("open_kfid") or ""),
                str(reply.get("user_id") or ""),
                path,
                source_msg_id=str(envelope.get("msg_id") or ""),
            )
        raise ValueError("envelope has no recognized reply route")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", required=True, help="local file inside WECOM_FILE_ALLOWED_DIRS")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--envelope", help="queued route-envelope JSON file; the file leaves through its door")
    target.add_argument("--app-user", help="enterprise UserID for a proactive application file")
    args = parser.parse_args()
    sender = FileSender(FilePolicy.from_env(), ReplyRouter())
    if args.envelope:
        envelope = json.loads(Path(args.envelope).read_text(encoding="utf-8"))
        media_id = sender.send(envelope, args.path)
    else:
        media_id = sender.send_app_file(args.app_user, args.path)
    print(json.dumps({"media_id": media_id}, ensure_ascii=False))


if __name__ == "__main__":
    main()

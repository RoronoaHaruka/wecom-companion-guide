#!/usr/bin/env python3
# Copyright (c) 2026 Roronoa & Haruka · From Raincove ♡
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Minimal queue consumer; replace run_agent with your long-lived agent channel."""

from __future__ import annotations

import glob
import json
import os
import shlex
import subprocess
import tempfile
import time
from pathlib import Path

from reply_router import send_reply


QUEUE_DIR = Path(os.environ.get("WECOM_QUEUE_DIR", "/var/lib/wecom-agent/queue"))
AGENT_COMMAND = shlex.split(os.environ.get("AGENT_COMMAND", ""))
AGENT_TIMEOUT_SECONDS = int(os.environ.get("AGENT_TIMEOUT_SECONDS", "300"))


def run_agent(envelope: dict) -> str:
    if not AGENT_COMMAND:
        raise RuntimeError(
            "set AGENT_COMMAND to a command that reads one route-envelope JSON object "
            "on stdin and writes one reply on stdout"
        )
    result = subprocess.run(
        AGENT_COMMAND,
        input=json.dumps(envelope, ensure_ascii=False),
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=AGENT_TIMEOUT_SECONDS,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"agent command exited {result.returncode}: {result.stderr[-300:]}")
    reply = result.stdout.strip()
    if not reply:
        raise RuntimeError("agent command returned an empty reply")
    return reply


def atomic_json(path: Path, value: dict) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".adapter-", dir=path.parent, text=True)
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


def recover_claims() -> None:
    for claimed_name in glob.glob(str(QUEUE_DIR / "*.processing")):
        claimed = Path(claimed_name)
        original = claimed.with_suffix(".json")
        if not original.exists():
            os.replace(claimed, original)


def process(path: Path) -> None:
    claimed = path.with_suffix(".processing")
    os.replace(path, claimed)
    try:
        envelope = json.loads(claimed.read_text(encoding="utf-8"))
        if not envelope.get("msg_id") or not isinstance(envelope.get("reply"), dict):
            raise ValueError("queued message has no route identity")

        pending_reply = envelope.get("_pending_reply")
        if not isinstance(pending_reply, str) or not pending_reply:
            pending_reply = run_agent(envelope)
            envelope["_pending_reply"] = pending_reply
            envelope["_reply_offset"] = 0
            atomic_json(claimed, envelope)

        start_index = int(envelope.get("_reply_offset") or 0)

        def save_progress(next_index: int) -> None:
            envelope["_reply_offset"] = next_index
            atomic_json(claimed, envelope)

        send_reply(
            envelope,
            pending_reply,
            start_index=start_index,
            on_sent=save_progress,
        )
        claimed.unlink()
    except Exception:
        if claimed.exists() and not path.exists():
            os.replace(claimed, path)
        raise


def queued_messages() -> list[Path]:
    paths = [Path(name) for name in glob.glob(str(QUEUE_DIR / "msg_*.json"))]
    return sorted(paths, key=lambda path: (path.stat().st_mtime_ns, path.name))


def main() -> None:
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(QUEUE_DIR, 0o700)
    recover_claims()
    while True:
        for path in queued_messages():
            try:
                process(path)
            except Exception as error:
                print(f"message retained for retry: {error}", flush=True)
                time.sleep(5)
                break
        time.sleep(2)


if __name__ == "__main__":
    main()

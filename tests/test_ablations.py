#!/usr/bin/env python3
# Copyright (c) 2026 Roronoa & Haruka · From Raincove ♡
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Mutation checks proving the release tests fail when a protection is removed."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Ablation:
    name: str
    relative_path: str
    original: str
    replacement: str


ABLATIONS = (
    Ablation(
        "callback signature verification",
        "code/callback_server.py",
        "return bool(supplied) and hmac.compare_digest(supplied, expected)",
        "return True",
    ),
    Ablation(
        "CorpID trailer verification",
        "code/callback_server.py",
        "if not hmac.compare_digest(corp_id, self.corp_id):",
        "if False:",
    ),
    Ablation(
        "exact AgentID routing",
        "code/callback_server.py",
        "if callback_agent_id != str(config.agent_id):",
        "if False:",
    ),
    Ablation(
        "hardened XML parser",
        "code/callback_server.py",
        "return DefusedET.fromstring(xml_text)",
        "return StdET.fromstring(xml_text)",
    ),
    Ablation(
        "persistent inbound de-duplication",
        "code/callback_server.py",
        "if self.seen(msg_id):",
        "if False:",
    ),
    Ablation(
        "application target allowlist",
        "code/sender.py",
        "if user_id not in self.config.allowed_user_ids:",
        "if False:",
    ),
    Ablation(
        "customer binding",
        "code/reply_router.py",
        "if not self.bound_customer(open_kfid, user_id):",
        "if False:",
    ),
    Ablation(
        "customer reply budget",
        "code/reply_router.py",
        "if remaining < count:",
        "if False:",
    ),
    Ablation(
        "source-aware reply routing",
        "code/reply_router.py",
        'if kind == "kf":',
        'if False and kind == "kf":',
    ),
    Ablation(
        "pending reply persistence",
        "code/bot_loop.py",
        'envelope["_pending_reply"] = pending_reply',
        'envelope["_pending_reply"] = ""',
    ),
    Ablation(
        "sent chunk checkpoint",
        "code/bot_loop.py",
        'envelope["_reply_offset"] = next_index',
        'envelope["_reply_offset"] = 0',
    ),
    Ablation(
        "outbound file directory allowlist",
        "code/file_sender.py",
        "if not any(resolved.is_relative_to(root) for root in self.allowed_dirs):",
        "if False:",
    ),
    Ablation(
        "outbound file suffix check",
        "code/file_sender.py",
        "if resolved.suffix.lower() not in self.allowed_suffixes:",
        "if False:",
    ),
    Ablation(
        "outbound file size cap",
        "code/file_sender.py",
        "if size > self.max_bytes:",
        "if False:",
    ),
    Ablation(
        "outbound file recipient allowlist",
        "code/file_sender.py",
        "if user_id not in app.config.allowed_user_ids:",
        "if False:",
    ),
    Ablation(
        "outbound file customer binding",
        "code/file_sender.py",
        "if not router.bound_customer(open_kfid, user_id):",
        "if False:",
    ),
    Ablation(
        "outbound voice text cap",
        "code/voice_sender.py",
        "if len(stripped) > self.max_text_chars:",
        "if False:",
    ),
    Ablation(
        "outbound voice duration cap",
        "code/voice_sender.py",
        "if duration > self.policy.max_seconds:",
        "if False:",
    ),
    Ablation(
        "outbound voice byte cap",
        "code/voice_sender.py",
        "if len(encoded) > self.policy.max_bytes:",
        "if False:",
    ),
    Ablation(
        "outbound voice recipient allowlist",
        "code/voice_sender.py",
        "if user_id not in app.config.allowed_user_ids:",
        "if False:",
    ),
    Ablation(
        "outbound voice customer binding",
        "code/voice_sender.py",
        "if not router.bound_customer(open_kfid, user_id):",
        "if False:",
    ),
)


class AblationTests(unittest.TestCase):
    def test_each_removed_protection_is_detected(self):
        for ablation in ABLATIONS:
            with self.subTest(ablation=ablation.name):
                with tempfile.TemporaryDirectory() as directory:
                    work = Path(directory)
                    shutil.copytree(ROOT / "code", work / "code", ignore=shutil.ignore_patterns("__pycache__"))
                    (work / "tests").mkdir()
                    shutil.copy2(ROOT / "tests" / "test_bridge.py", work / "tests" / "test_bridge.py")

                    target = work / ablation.relative_path
                    source = target.read_text(encoding="utf-8")
                    self.assertEqual(source.count(ablation.original), 1, ablation.name)
                    target.write_text(
                        source.replace(ablation.original, ablation.replacement),
                        encoding="utf-8",
                    )

                    environment = os.environ.copy()
                    environment["PYTHONPATH"] = str(work / "code")
                    result = subprocess.run(
                        [
                            sys.executable,
                            "-m",
                            "unittest",
                            "discover",
                            "-s",
                            str(work / "tests"),
                            "-p",
                            "test_bridge.py",
                            "-q",
                        ],
                        cwd=work,
                        env=environment,
                        text=True,
                        capture_output=True,
                        timeout=30,
                        check=False,
                    )
                    self.assertNotEqual(
                        result.returncode,
                        0,
                        f"ablation survived undetected: {ablation.name}",
                    )


if __name__ == "__main__":
    unittest.main(verbosity=2)

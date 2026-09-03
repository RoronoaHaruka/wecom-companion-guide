#!/usr/bin/env python3
# Copyright (c) 2026 Roronoa & Haruka · From Raincove ♡
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PublicRepositoryTests(unittest.TestCase):
    def test_software_files_carry_noncommercial_spdx_notice(self):
        files = list((ROOT / "code").glob("*.py")) + list((ROOT / "tests").glob("*.py"))
        for path in files:
            header = "\n".join(path.read_text(encoding="utf-8").splitlines()[:5])
            self.assertIn("SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0", header, path)

    def test_required_public_files_exist(self):
        for relative in (
            "LICENSE", "LICENSE-CODE", "NOTICE.md", "SECURITY.md", ".env.example",
            "requirements.txt", "code/example_agent.py", "guide/微信客服双入口.md", "guide/微信客服双入口.html",
            "guide/微信客服双入口.pdf", "systemd/wecom-app-callback.service",
            "systemd/wecom-kf-bridge.service",
        ):
            self.assertTrue((ROOT / relative).is_file(), relative)

    def test_current_guides_do_not_restore_retired_examples(self):
        current = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (
                ROOT / "README.md",
                ROOT / "guide" / "企业微信机器人搭建手册.html",
                ROOT / "guide" / "微信客服双入口.md",
                ROOT / "guide" / "微信客服双入口.html",
            )
        )
        for retired in (
            "企业名称随便填", "/tmp/wecom-queue/", "代码里的TOKEN",
            "代码里的ENCODING_AES_KEY", "stdin读提示", "export QUEUE_DIR=",
            "pkg.cloudflare.com/cloudflared-linux-amd64.rpm",
        ):
            self.assertNotIn(retired, current)

    def test_text_files_do_not_contain_credentials_or_private_infrastructure(self):
        excluded = {"LICENSE", "LICENSE-CODE"}
        text_files = [
            path for path in ROOT.rglob("*")
            if path.is_file()
            and ".git" not in path.parts
            and path.name not in excluded
            and path.suffix.lower() not in {".pdf", ".pyc"}
        ]
        forbidden_literals = (
            "/" + "Users" + "/", "." + "rc-secrets",
            "ANTHROPIC_" + "AUTH_TOKEN=", "OPENROUTER_" + "API_KEY=",
        )
        credential_patterns = (
            re.compile(r"ghp_[A-Za-z0-9]{20,}"),
            re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
            re.compile(r"sk-(?:ant-)?[A-Za-z0-9_-]{20,}"),
            re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
        )
        for path in text_files:
            text = path.read_text(encoding="utf-8", errors="ignore")
            for literal in forbidden_literals:
                self.assertNotIn(literal, text, f"{literal} in {path}")
            for pattern in credential_patterns:
                self.assertIsNone(pattern.search(text), f"credential-like text in {path}")


if __name__ == "__main__":
    unittest.main(verbosity=2)

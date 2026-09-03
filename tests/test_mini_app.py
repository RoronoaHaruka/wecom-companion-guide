#!/usr/bin/env python3
# Copyright (c) 2026 Roronoa & Haruka · From Raincove ♡
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Offline checks for the mini-app token scope."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("mini_app_server", ROOT / "code" / "mini_app_server.py")
mini_app = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mini_app)

TOKEN = "a" * 48


class MiniAppScopeTests(unittest.TestCase):
    def test_correct_token_opens_only_the_mini_prefix(self):
        self.assertTrue(mini_app.is_scoped_request("/api/mini/status", TOKEN, TOKEN))
        self.assertTrue(mini_app.is_scoped_request("/api/mini/stream", TOKEN, TOKEN))

    def test_correct_token_is_refused_everywhere_else(self):
        for path in ("/api/status", "/api/other/secret", "/api/mini", "/mini", "/", "/api/minix/status"):
            self.assertFalse(mini_app.is_scoped_request(path, TOKEN, TOKEN), path)

    def test_wrong_or_missing_token_is_refused(self):
        self.assertFalse(mini_app.is_scoped_request("/api/mini/status", "b" * 48, TOKEN))
        self.assertFalse(mini_app.is_scoped_request("/api/mini/status", "", TOKEN))
        self.assertFalse(mini_app.is_scoped_request("/api/mini/status", TOKEN[:-1], TOKEN))

    def test_empty_expected_token_locks_the_door(self):
        self.assertFalse(mini_app.is_scoped_request("/api/mini/status", "", ""))
        self.assertFalse(mini_app.is_scoped_request("/api/mini/status", TOKEN, ""))

    def test_token_is_read_from_query_or_bearer_header(self):
        self.assertEqual(mini_app.presented_token({"auth": [TOKEN]}, ""), TOKEN)
        self.assertEqual(mini_app.presented_token({}, f"Bearer {TOKEN}"), TOKEN)
        self.assertEqual(mini_app.presented_token({}, f"bearer {TOKEN}"), TOKEN)
        self.assertEqual(mini_app.presented_token({}, "Basic abc"), "")
        self.assertEqual(mini_app.presented_token({"auth": [""]}, ""), "")

    def test_demo_data_source_is_consistent(self):
        ids = {item["id"] for item in mini_app.load_status()}
        for item_id in ids:
            frame = mini_app.snapshot(item_id)
            self.assertNotIn("error", frame)
            self.assertIn("text", frame)
        self.assertIn("error", mini_app.snapshot("does-not-exist"))

    def test_page_template_uses_the_same_prefix(self):
        page = (ROOT / "code" / "mini_app_page.html").read_text(encoding="utf-8")
        self.assertIn(f'var API = "{mini_app.API_PREFIX}"', page)
        self.assertIn("mini_app_key", page)


if __name__ == "__main__":
    unittest.main(verbosity=2)

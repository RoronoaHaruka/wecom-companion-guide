#!/usr/bin/env python3
# Copyright (c) 2026 Roronoa & Haruka · From Raincove ♡
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Smoke-test agent: read one route envelope and return a visible echo reply."""

from __future__ import annotations

import json
import sys


def main() -> None:
    envelope = json.load(sys.stdin)
    source = str(envelope.get("source") or "unknown")
    content = str(envelope.get("content") or "").strip()
    if not content:
        content = f"[{envelope.get('source_msgtype') or envelope.get('msg_type') or 'message'}]"
    print(f"[{source}] 已收到：{content}")


if __name__ == "__main__":
    main()

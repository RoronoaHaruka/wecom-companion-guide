# Guides

Documentation copyright © 2026 Roronoa & Haruka · From Raincove ♡. Licensed under [CC BY-NC-SA 4.0](../LICENSE).

## Current

- [企业微信机器人搭建手册](企业微信机器人搭建手册.html) · [PDF](企业微信机器人搭建手册.pdf)
- [微信客服双入口](微信客服双入口.md) · [HTML](微信客服双入口.html) · [PDF](微信客服双入口.pdf)
- [迷你应用](迷你应用.md) · [HTML](迷你应用.html) · [PDF](迷你应用.pdf)
- [一副声音](原生语音.md)

The main guide covers the self-built application and WeChat plug-in path. The dual-port guide adds a Mac-independent WeChat Customer Service forwarding entrance and routes it into the same existing Agent process. The mini-app guide hangs a token-scoped observation page off the self-built application's home page and bottom menu so it opens with one tap inside personal WeChat, without an ICP-filed domain or a business licence. The voice guide turns one line of text into a native WeCom voice bubble through either door (TTS → AMR-NB → `msgtype=voice`) and covers the web half: a key-free TTS relay, a `/play` link page, and the iOS silent-switch workaround. Its appendix collects field notes on scene-style audio craft and licence-aware places to source effect materials.

## Archive

- [`archive/企业微信机器人搭建手册-v1.0.0.pdf`](archive/企业微信机器人搭建手册-v1.0.0.pdf) preserves the original signed pink layout released with v1.0.0. Its embedded code is historical; use the current files in `code/` for deployment.

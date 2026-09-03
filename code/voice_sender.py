#!/usr/bin/env python3
# Copyright (c) 2026 Roronoa & Haruka · From Raincove ♡
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Speak one short line as a native WeCom voice bubble, through either door."""

from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import hashlib
import json
import os
import subprocess
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable

from file_sender import FileSender
from reply_router import ReplyRouter
from sender import WeComAPIError, required_env


API = "https://qyapi.weixin.qq.com/cgi-bin"
ELEVENLABS_API = "https://api.elevenlabs.io/v1"
AMR_HEADER = b"#!AMR\n"
PCM_RATE = 8000
PCM_FRAME_SAMPLES = 160
API_MAX_VOICE_SECONDS = 60
API_MAX_VOICE_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class VoicePolicy:
    max_text_chars: int
    max_seconds: int
    max_bytes: int

    @classmethod
    def from_env(cls) -> "VoicePolicy":
        max_text_chars = int(os.environ.get("WECOM_VOICE_MAX_TEXT_CHARS", "220"))
        if max_text_chars <= 0:
            raise ValueError("WECOM_VOICE_MAX_TEXT_CHARS must be positive")
        max_seconds = int(os.environ.get("WECOM_VOICE_MAX_SECONDS", str(API_MAX_VOICE_SECONDS)))
        if not 0 < max_seconds <= API_MAX_VOICE_SECONDS:
            raise ValueError("WECOM_VOICE_MAX_SECONDS must stay within the 60-second voice cap")
        max_bytes = int(os.environ.get("WECOM_VOICE_MAX_BYTES", str(API_MAX_VOICE_BYTES)))
        if not 0 < max_bytes <= API_MAX_VOICE_BYTES:
            raise ValueError("WECOM_VOICE_MAX_BYTES must stay within the 2MB voice-upload cap")
        return cls(max_text_chars=max_text_chars, max_seconds=max_seconds, max_bytes=max_bytes)

    def validate_text(self, text: str) -> str:
        stripped = text.strip()
        if not stripped:
            raise ValueError("voice text is empty")
        if len(stripped) > self.max_text_chars:
            raise ValueError("voice text exceeds WECOM_VOICE_MAX_TEXT_CHARS")
        return stripped


def elevenlabs_tts(text: str) -> bytes:
    """Default synthesizer. Any callable returning MP3/WAV bytes can replace it."""
    key = required_env("ELEVENLABS_API_KEY")
    voice_id = required_env("ELEVENLABS_VOICE_ID")
    model_id = os.environ.get("ELEVENLABS_MODEL_ID", "eleven_v3").strip() or "eleven_v3"
    request = urllib.request.Request(
        f"{ELEVENLABS_API}/text-to-speech/{urllib.parse.quote(voice_id)}",
        data=json.dumps({"text": text, "model_id": model_id}, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"xi-api-key": key, "Content-Type": "application/json", "Accept": "audio/mpeg"},
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        audio = response.read()
    if not audio or audio[:1] in (b"{", b"["):
        raise RuntimeError(f"TTS returned an error payload: {audio[:200]!r}")
    return audio


def decode_pcm(audio: bytes) -> bytes:
    """Decode any ffmpeg-readable audio to the 8kHz mono s16le stream AMR-NB expects."""
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", "pipe:0", "-ar", str(PCM_RATE), "-ac", "1", "-f", "s16le", "pipe:1"],
        input=audio,
        capture_output=True,
        timeout=60,
    )
    if result.returncode != 0 or not result.stdout:
        raise RuntimeError(f"ffmpeg decode failed: {result.stderr.decode('utf-8', 'replace')[:200]}")
    return result.stdout


def encode_amr(pcm: bytes) -> bytes:
    """Encode 8kHz mono s16le PCM as AMR-NB 12.2kbps via libopencore-amrnb."""
    library = ctypes.util.find_library("opencore-amrnb") or "libopencore-amrnb.so.0"
    codec = ctypes.CDLL(library)
    codec.Encoder_Interface_init.argtypes = [ctypes.c_int]
    codec.Encoder_Interface_init.restype = ctypes.c_void_p
    codec.Encoder_Interface_Encode.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_short),
        ctypes.POINTER(ctypes.c_ubyte),
        ctypes.c_int,
    ]
    codec.Encoder_Interface_Encode.restype = ctypes.c_int
    codec.Encoder_Interface_exit.argtypes = [ctypes.c_void_p]

    frame_bytes = PCM_FRAME_SAMPLES * 2
    if len(pcm) % frame_bytes:
        pcm += b"\0" * (frame_bytes - len(pcm) % frame_bytes)
    state = codec.Encoder_Interface_init(0)
    if not state:
        raise RuntimeError("AMR encoder initialization failed")
    try:
        encoded = bytearray(AMR_HEADER)
        output = (ctypes.c_ubyte * 64)()
        for offset in range(0, len(pcm), frame_bytes):
            samples = (ctypes.c_short * PCM_FRAME_SAMPLES).from_buffer_copy(pcm[offset:offset + frame_bytes])
            size = codec.Encoder_Interface_Encode(state, 7, samples, output, 0)
            if size <= 0:
                raise RuntimeError(f"AMR encoding failed: {size}")
            encoded.extend(bytes(output[:size]))
        return bytes(encoded)
    finally:
        codec.Encoder_Interface_exit(state)


class VoiceSender:
    def __init__(
        self,
        policy: VoicePolicy,
        router: ReplyRouter,
        *,
        synthesize: Callable[[str], bytes] = elevenlabs_tts,
        decode: Callable[[bytes], bytes] = decode_pcm,
        encode: Callable[[bytes], bytes] = encode_amr,
    ):
        self.policy = policy
        self.router = router
        self.synthesize = synthesize
        self.decode = decode
        self.encode = encode

    def render(self, text: str) -> bytes:
        approved = self.policy.validate_text(text)
        pcm = self.decode(self.synthesize(approved))
        duration = len(pcm) / (PCM_RATE * 2)
        if duration > self.policy.max_seconds:
            raise ValueError(f"voice is {duration:.1f}s; WECOM_VOICE_MAX_SECONDS allows {self.policy.max_seconds}s")
        encoded = self.encode(pcm)
        if len(encoded) > self.policy.max_bytes:
            raise ValueError("encoded voice exceeds WECOM_VOICE_MAX_BYTES")
        return encoded

    def upload(self, token: str, encoded: bytes) -> str:
        payload, content_type = FileSender.multipart("media", "voice.amr", encoded)
        query = urllib.parse.urlencode({"access_token": token, "type": "voice"})
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

    def send_app_voice(self, user_id: str, text: str) -> str:
        app = self.router.app_sender
        if user_id not in app.config.allowed_user_ids:
            raise PermissionError("target UserID is not allowlisted")
        encoded = self.render(text)
        token = app.access_token()
        media_id = self.upload(token, encoded)
        query = urllib.parse.urlencode({"access_token": token})
        app.request_json(
            "POST",
            f"{API}/message/send?{query}",
            {
                "touser": user_id,
                "msgtype": "voice",
                "agentid": app.config.agent_id,
                "voice": {"media_id": media_id},
            },
        )
        return media_id

    def send_kf_voice(
        self,
        open_kfid: str,
        user_id: str,
        text: str,
        *,
        source_msg_id: str = "",
    ) -> str:
        router = self.router
        if not router.open_kfid or open_kfid != router.open_kfid:
            raise PermissionError("customer-service account is not allowlisted")
        if not router.bound_customer(open_kfid, user_id):
            raise PermissionError("customer-service user is not the bound customer")
        encoded = self.render(text)
        token = router.app_sender.access_token()
        media_id = self.upload(token, encoded)
        budget_msg_id = router.reserve_budget(open_kfid, user_id, 1)
        material = "\0".join((open_kfid, user_id, source_msg_id or budget_msg_id, text))
        message_id = "v" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:31]
        query = urllib.parse.urlencode({"access_token": token})
        try:
            router.app_sender.request_json(
                "POST",
                f"{API}/kf/send_msg?{query}",
                {
                    "touser": user_id,
                    "open_kfid": open_kfid,
                    "msgid": message_id,
                    "msgtype": "voice",
                    "voice": {"media_id": media_id},
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

    def send(self, envelope: dict, text: str) -> str:
        reply = envelope.get("reply") or {}
        kind = reply.get("kind")
        if kind == "app":
            return self.send_app_voice(str(reply.get("user_id") or ""), text)
        if kind == "kf":
            return self.send_kf_voice(
                str(reply.get("open_kfid") or ""),
                str(reply.get("user_id") or ""),
                text,
                source_msg_id=str(envelope.get("msg_id") or ""),
            )
        raise ValueError("envelope has no recognized reply route")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--text", required=True, help="short line to speak; capped by WECOM_VOICE_MAX_TEXT_CHARS")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--envelope", help="queued route-envelope JSON file; the voice leaves through its door")
    target.add_argument("--app-user", help="enterprise UserID for a proactive application voice")
    args = parser.parse_args()
    tool = VoiceSender(VoicePolicy.from_env(), ReplyRouter())
    if args.envelope:
        envelope = json.loads(open(args.envelope, encoding="utf-8").read())
        media_id = tool.send(envelope, args.text)
    else:
        media_id = tool.send_app_voice(args.app_user, args.text)
    print(json.dumps({"media_id": media_id}, ensure_ascii=False))


if __name__ == "__main__":
    main()

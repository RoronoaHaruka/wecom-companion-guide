#!/usr/bin/env python3
# Copyright (c) 2026 Roronoa & Haruka · From Raincove ♡
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import base64
import http.client
import json
import os
import sqlite3
import struct
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from Crypto.Cipher import AES


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

import bot_loop
import callback_server
import customer_service_bridge
import reply_router
import sender


ENCODING_KEY = "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG"


def encrypt(xml: str, corp_id: str = "ww_example") -> str:
    key = base64.b64decode(ENCODING_KEY + "=")
    raw = b"0123456789abcdef" + struct.pack("!I", len(xml.encode())) + xml.encode() + corp_id.encode()
    padding = 32 - len(raw) % 32
    raw += bytes([padding]) * padding
    return base64.b64encode(AES.new(key, AES.MODE_CBC, key[:16]).encrypt(raw)).decode()


class BridgeTests(unittest.TestCase):
    def test_utf8_split_preserves_text_and_byte_limit(self):
        text = "中文段落。" * 900 + "\nend"
        chunks = sender.split_utf8(text, 1800)
        self.assertEqual("".join(chunks), text)
        self.assertTrue(all(len(chunk.encode("utf-8")) <= 1800 for chunk in chunks))

    def test_crypto_validates_signature_padding_and_corp_id(self):
        crypto = callback_server.WeComCrypto("token", ENCODING_KEY, "ww_example")
        xml = "<xml><MsgType><![CDATA[text]]></MsgType></xml>"
        encrypted = encrypt(xml)
        signature = crypto.signature("1", "2", encrypted)
        self.assertTrue(crypto.valid_signature(signature, "1", "2", encrypted))
        self.assertFalse(crypto.valid_signature("0" * 40, "1", "2", encrypted))
        self.assertEqual(crypto.decrypt(encrypted), xml)
        with self.assertRaisesRegex(ValueError, "CorpID mismatch"):
            crypto.decrypt(encrypt(xml, "ww_other"))
        damaged = bytearray(base64.b64decode(encrypted))
        damaged[-1] ^= 1
        with self.assertRaises(ValueError):
            crypto.decrypt(base64.b64encode(damaged).decode())
        with self.assertRaisesRegex(ValueError, "invalid XML"):
            callback_server.parse_xml(
                '<!DOCTYPE x [<!ENTITY a "expanded">]><xml><Content>&a;</Content></xml>'
            )

    def test_application_queue_is_atomic_and_deduplicated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = callback_server.Config(
                corp_id="ww_example",
                callback_token="token",
                encoding_aes_key=ENCODING_KEY,
                agent_id=1000002,
                queue_dir=root / "queue",
                state_db=root / "state.sqlite3",
                allowed_user_ids=frozenset({"user_example"}),
            )
            inbox = callback_server.Inbox(config.queue_dir, config.state_db)
            message = callback_server.parse_xml(
                "<xml><FromUserName>user_example</FromUserName><CreateTime>1</CreateTime>"
                "<MsgType>text</MsgType><Content>hello</Content><MsgId>m1</MsgId>"
                "<AgentID>1000002</AgentID></xml>"
            )
            envelope = callback_server.message_envelope(message, config)
            self.assertTrue(inbox.enqueue(envelope))
            self.assertFalse(inbox.enqueue(envelope))
            files = list(config.queue_dir.glob("msg_*.json"))
            self.assertEqual(len(files), 1)
            self.assertEqual(json.loads(files[0].read_text())["content"], "hello")
            self.assertEqual(files[0].stat().st_mode & 0o777, 0o600)

    def test_callback_serves_exact_verification_file_and_rejects_missing_agent_id(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            verify_file = root / "WW_verify_example.txt"
            verify_file.write_text("verification-value", encoding="utf-8")
            config = callback_server.Config(
                corp_id="ww_example",
                callback_token="token",
                encoding_aes_key=ENCODING_KEY,
                agent_id=1000002,
                queue_dir=root / "queue",
                state_db=root / "state.sqlite3",
                allowed_user_ids=frozenset({"user_example"}),
                verify_file=verify_file,
            )
            crypto = callback_server.WeComCrypto("token", ENCODING_KEY, "ww_example")
            inbox = callback_server.Inbox(config.queue_dir, config.state_db)
            server = callback_server.ThreadingHTTPServer(
                ("127.0.0.1", 0),
                callback_server.make_handler(config, crypto, inbox),
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                connection.request("GET", "/WW_verify_example.txt")
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(response.read().decode(), "verification-value")
                connection.close()

                inner = (
                    "<xml><FromUserName>user_example</FromUserName><CreateTime>1</CreateTime>"
                    "<MsgType>text</MsgType><Content>hello</Content><MsgId>m-http</MsgId></xml>"
                )
                encrypted = encrypt(inner)
                signature = crypto.signature("1", "2", encrypted)
                outer = f"<xml><Encrypt><![CDATA[{encrypted}]]></Encrypt></xml>"
                path = f"/?msg_signature={signature}&timestamp=1&nonce=2"
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                connection.request(
                    "POST",
                    path,
                    body=outer.encode(),
                    headers={"Content-Type": "text/xml", "Content-Length": str(len(outer.encode()))},
                )
                response = connection.getresponse()
                self.assertEqual(response.status, 403)
                response.read()
                connection.close()
                self.assertEqual(list(config.queue_dir.glob("msg_*.json")), [])
            finally:
                server.shutdown()
                server.server_close()
                inbox.db.close()
                thread.join(timeout=5)

    def test_customer_service_merged_message_and_budget_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = customer_service_bridge.Config(
                corp_id="ww_example",
                app_secret="secret",
                open_kfid="wk_example",
                route_agent="agent_default",
                queue_dir=root / "queue",
                data_dir=root / "data",
                state_db=root / "state.sqlite3",
                expected_external_userid="wm_example",
            )
            bridge = customer_service_bridge.CustomerServiceBridge(config)
            message = {
                "msgid": "kf1",
                "external_userid": "wm_example",
                "origin": 3,
                "send_time": 1,
                "msgtype": "merged_msg",
                "merged_msg": {
                    "title": "聊天记录",
                    "item": [{
                        "send_time": 1,
                        "sender_name": "示例用户",
                        "msgtype": "text",
                        "msg_content": json.dumps({"msgtype": "text", "text": {"content": "第一条"}}, ensure_ascii=False),
                    }],
                },
            }
            envelope = bridge.normalize(message, "unused-token")
            bridge.store.enqueue_and_commit(envelope)
            self.assertIn("第一条", envelope["content"])
            with bridge.store.lock:
                budget = bridge.store.db.execute(
                    "SELECT remaining FROM reply_budget WHERE route_key=?",
                    ("wk_example:wm_example",),
                ).fetchone()
            self.assertEqual(budget, (5,))

    def test_reply_budget_is_shared_across_calls_and_restorable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_db = root / "state.sqlite3"
            config = customer_service_bridge.Config(
                corp_id="ww_example",
                app_secret="secret",
                open_kfid="wk_example",
                route_agent="agent_default",
                queue_dir=root / "queue",
                data_dir=root / "data",
                state_db=state_db,
                expected_external_userid="wm_example",
            )
            store = customer_service_bridge.Store(config)
            with store.lock:
                store.db.execute(
                    "INSERT INTO kf_bindings(open_kfid,external_userid) VALUES(?,?)",
                    ("wk_example", "wm_example"),
                )
                store.db.execute(
                    "INSERT INTO reply_budget(route_key,source_msg_id,remaining,updated_at) VALUES(?,?,5,0)",
                    ("wk_example:wm_example", "source-1"),
                )
                store.db.commit()
            environment = {
                "WECOM_CORP_ID": "ww_example",
                "WECOM_APP_SECRET": "secret",
                "WECOM_AGENT_ID": "1000002",
                "WECOM_ALLOWED_USER_IDS": "user_example",
                "WECOM_OPEN_KFID": "wk_example",
                "WECOM_STATE_DB": str(state_db),
            }
            with patch.dict(os.environ, environment, clear=False):
                router = reply_router.ReplyRouter()
                source_id = router.reserve_budget("wk_example", "wm_example", 3)
                self.assertEqual(source_id, "source-1")
                with self.assertRaisesRegex(RuntimeError, "has 2 slot"):
                    router.reserve_budget("wk_example", "wm_example", 3)
                router.restore_budget("wk_example", "wm_example", source_id, 1)
                self.assertEqual(router.reserve_budget("wk_example", "wm_example", 3), "source-1")

    def test_agent_receives_the_full_route_envelope(self):
        envelope = {
            "source": "wecom_kf",
            "msg_id": "kf1",
            "content": "hello",
            "media_paths": ["/private/example.jpg"],
            "reply": {"kind": "kf", "user_id": "wm_example"},
        }
        command = [sys.executable, "-c", "import sys; print(sys.stdin.read())"]
        with patch.object(bot_loop, "AGENT_COMMAND", command):
            received = json.loads(bot_loop.run_agent(envelope))
        self.assertEqual(received, envelope)

    def test_reply_router_uses_the_route_carried_by_each_envelope(self):
        environment = {
            "WECOM_CORP_ID": "ww_example",
            "WECOM_APP_SECRET": "secret",
            "WECOM_AGENT_ID": "1000002",
            "WECOM_ALLOWED_USER_IDS": "user_example",
            "WECOM_OPEN_KFID": "wk_example",
        }
        with patch.dict(os.environ, environment, clear=False):
            router = reply_router.ReplyRouter()
        with patch.object(router.app_sender, "access_token", return_value="token"):
            with patch.object(router.app_sender, "request_json", return_value={"errcode": 0}):
                with self.assertRaisesRegex(PermissionError, "allowlisted"):
                    router.app_sender.send_text("user_other", "blocked")
        with patch.object(router.app_sender, "send_text", return_value=1) as send_app:
            self.assertEqual(router.send({
                "msg_id": "app1",
                "reply": {"kind": "app", "user_id": "user_example"},
            }, "app reply"), 1)
            send_app.assert_called_once_with(
                "user_example", "app reply", start_index=0, on_sent=None
            )
        with patch.object(router, "send_customer_service", return_value=1) as send_kf:
            self.assertEqual(router.send({
                "msg_id": "kf1",
                "reply": {"kind": "kf", "open_kfid": "wk_example", "user_id": "wm_example"},
            }, "kf reply"), 1)
            send_kf.assert_called_once_with(
                {"kind": "kf", "open_kfid": "wk_example", "user_id": "wm_example"},
                "kf reply",
                source_msg_id="kf1",
                start_index=0,
                on_sent=None,
            )

    def test_adapter_persists_reply_and_chunk_progress_before_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "msg_kf_example.json"
            path.write_text(json.dumps({
                "source": "wecom_kf",
                "msg_id": "kf1",
                "content": "hello",
                "reply": {"kind": "kf", "user_id": "wm_example"},
            }), encoding="utf-8")

            def fail_after_first_chunk(envelope, text, *, start_index, on_sent):
                self.assertEqual(start_index, 0)
                on_sent(1)
                raise RuntimeError("temporary send failure")

            with patch.object(bot_loop, "run_agent", return_value="long reply") as run_agent:
                with patch.object(bot_loop, "send_reply", side_effect=fail_after_first_chunk):
                    with self.assertRaisesRegex(RuntimeError, "temporary send failure"):
                        bot_loop.process(path)
                run_agent.assert_called_once()

            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["_pending_reply"], "long reply")
            self.assertEqual(saved["_reply_offset"], 1)

            def finish_retry(envelope, text, *, start_index, on_sent):
                self.assertEqual(text, "long reply")
                self.assertEqual(start_index, 1)
                on_sent(2)
                return 2

            with patch.object(bot_loop, "run_agent") as run_agent:
                with patch.object(bot_loop, "send_reply", side_effect=finish_retry):
                    bot_loop.process(path)
                run_agent.assert_not_called()
            self.assertFalse(path.exists())

    def test_customer_service_duplicate_message_id_finishes_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_db = root / "state.sqlite3"
            config = customer_service_bridge.Config(
                corp_id="ww_example",
                app_secret="secret",
                open_kfid="wk_example",
                route_agent="agent_default",
                queue_dir=root / "queue",
                data_dir=root / "data",
                state_db=state_db,
                expected_external_userid="wm_example",
            )
            store = customer_service_bridge.Store(config)
            with store.lock:
                store.db.execute(
                    "INSERT INTO kf_bindings(open_kfid,external_userid) VALUES(?,?)",
                    ("wk_example", "wm_example"),
                )
                store.db.execute(
                    "INSERT INTO reply_budget(route_key,source_msg_id,remaining,updated_at) VALUES(?,?,5,0)",
                    ("wk_example:wm_example", "source-1"),
                )
                store.db.commit()
            environment = {
                "WECOM_CORP_ID": "ww_example",
                "WECOM_APP_SECRET": "secret",
                "WECOM_AGENT_ID": "1000002",
                "WECOM_ALLOWED_USER_IDS": "user_example",
                "WECOM_OPEN_KFID": "wk_example",
                "WECOM_STATE_DB": str(state_db),
            }
            with patch.dict(os.environ, environment, clear=False):
                router = reply_router.ReplyRouter()
                with self.assertRaisesRegex(PermissionError, "bound customer"):
                    router.send_customer_service(
                        {"open_kfid": "wk_example", "user_id": "wm_other"},
                        "reply",
                        source_msg_id="source-1",
                    )
                progress = []
                with patch.object(router.app_sender, "access_token", return_value="token"):
                    with patch.object(
                        router.app_sender,
                        "request_json",
                        side_effect=sender.WeComAPIError(95033, "duplicate msgid"),
                    ):
                        sent = router.send_customer_service(
                            {"open_kfid": "wk_example", "user_id": "wm_example"},
                            "reply",
                            source_msg_id="source-1",
                            on_sent=progress.append,
                        )
            self.assertEqual(sent, 1)
            self.assertEqual(progress, [1])
            with sqlite3.connect(state_db) as db:
                remaining = db.execute(
                    "SELECT remaining FROM reply_budget WHERE route_key=?",
                    ("wk_example:wm_example",),
                ).fetchone()
            self.assertEqual(remaining, (4,))


if __name__ == "__main__":
    unittest.main(verbosity=2)

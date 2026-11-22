from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from qsick.config import RuntimeConfig
from qsick.runtime import Runtime
from qsick.transport.headless import HeadlessTransport
from qsick.webui.app import create_app

import http.server
import json
import threading


async def test_headless_ingest_and_message_store(tmp_path: Path, monkeypatch):
    config = RuntimeConfig(data_dir=str(tmp_path / "data"))
    config.transport.mode = "headless"
    config.plugins.dirs = []
    config.headless_root = str(tmp_path / "hosts")
    host_dir = Path(config.headless_root) / "C0"
    host_dir.mkdir(parents=True)
    (host_dir / "config.json").write_text(json.dumps({"bridgeToken": "test-secret"}), encoding="utf-8")
    runtime = Runtime(config)
    # Native group lookup is unavailable in this event-ingress-only fixture.
    from qsick.transport.base import SendResult
    from unittest.mock import AsyncMock
    monkeypatch.setattr(runtime.transport, "send", AsyncMock(return_value=SendResult.failure("unsupported", 1502)))
    await runtime.start(serve=False)
    try:
        transport = httpx.ASGITransport(app=create_app(runtime))
        async with httpx.AsyncClient(transport=transport, base_url="http://ui", headers={"Authorization": "Bearer test-secret", "X-QSICK-Instance": "C0"}) as web:
            login = await web.post(
                "/api/headless/event",
                json={"kind": "login", "payload": {"logged_in": True, "uin": "3905548034", "nickname": "Headless"}},
            )
            assert login.status_code == 200

            message = await web.post(
                "/api/headless/event",
                json={
                    "kind": "message",
                    "payload": {
                        "message_id": 1001,
                        "seq": 1,
                        "time": 1794310000,
                        "chat": {"type": "group", "id": 2169010946},
                        "sender": {"uin": 20001, "nickname": "Alice", "card": "A"},
                        "elements": [{"type": "text", "data": {"text": "hello headless"}}],
                        "account": "3905548034",
                    },
                },
            )
            assert message.status_code == 200
            await asyncio.sleep(0.2)

            status = await web.get("/api/status")
            assert status.json()["uin"] == "3905548034"
            assert status.json()["transport"] == "headless"

            messages = await web.get("/messages")
            assert "hello headless" in messages.text

            groups = await web.get("/api/chat/groups?account=3905548034")
            assert groups.json()["groups"][0]["group_id"] == 2169010946
    finally:
        await runtime.stop()


async def test_headless_transport_sends_text_to_host():
    received = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("content-length", 0))
            received.update(json.loads(self.rfile.read(length)))
            assert self.path == "/action"
            body = json.dumps({"ok": True, "data": {"message_id": 42}}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        transport = HeadlessTransport(server.server_port)
        result = await transport.send(
            "message.send",
            {"chat": {"type": "group", "id": 2169010946}, "elements": [{"type": "text", "data": {"text": "hi"}}]},
        )
        assert result.ok is True
        assert result.data["message_id"] == 42
        assert received == {"op": "message.send", "account": "", "params": {"chat": {"type": "group", "id": 2169010946}, "elements": [{"type": "text", "data": {"text": "hi"}}]}}
    finally:
        server.shutdown()


def test_per_message_logging_stays_at_debug():
    """A busy group must not flood the log with one INFO line per message."""
    import inspect
    from qsick.transport import headless as headless_transport

    source = inspect.getsource(headless_transport.HeadlessTransport.push)
    assert 'log.debug("headless message received"' in source
    assert 'log.info("headless message received"' not in source

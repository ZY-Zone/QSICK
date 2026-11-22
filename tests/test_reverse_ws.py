from __future__ import annotations

import asyncio
import json
import socket

import pytest
import websockets

from qsick.config import RuntimeConfig
from qsick.runtime import Runtime


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def test_reverse_ws_receives_lifecycle_and_answers_actions(config: RuntimeConfig):
    port = free_port()
    received: list[dict] = []
    headers: dict[str, str] = {}
    replied = asyncio.Event()

    async def handler(ws):
        headers.update(dict(ws.request.headers))
        received.append(json.loads(await ws.recv()))
        await ws.send(json.dumps({"action": "get_login_info", "params": {}, "echo": "rev"}))
        received.append(json.loads(await ws.recv()))
        replied.set()

    async with websockets.serve(handler, "127.0.0.1", port):
        config.onebot.access_token = "secret"
        config.onebot.reverse_ws_urls = [f"ws://127.0.0.1:{port}/ws"]
        config.onebot.reverse_reconnect_seconds = 1.0
        runtime = Runtime(config)
        await runtime.start(serve=False)
        try:
            await asyncio.wait_for(replied.wait(), timeout=5)
        finally:
            await runtime.stop()

    assert received[0]["meta_event_type"] == "lifecycle"
    assert received[0]["self_id"] == 10001
    assert received[1]["status"] == "ok"
    assert received[1]["echo"] == "rev"
    assert received[1]["data"]["user_id"] == 10001
    assert headers.get("authorization") == "Bearer secret"
    assert headers.get("x-client-role") == "Universal"


async def test_reverse_ws_receives_runtime_events_not_only_replies(config: RuntimeConfig):
    port = free_port()
    received: list[dict] = []
    event_seen = asyncio.Event()

    async def handler(ws):
        received.append(json.loads(await ws.recv()))  # lifecycle
        # Server pushes an event the way OneBot reverse consumers expect.
        await runtime.reverse.broadcast(
            {"post_type": "message", "message_type": "group", "group_id": 2169010946,
             "message_id": 1001, "self_id": 10001, "raw_message": "reverse push"}
        )
        while True:
            frame = json.loads(await ws.recv())
            received.append(frame)
            if frame.get("post_type") == "message":
                event_seen.set()
                return

    async with websockets.serve(handler, "127.0.0.1", port):
        config.onebot.reverse_ws_urls = [f"ws://127.0.0.1:{port}/ws"]
        config.onebot.reverse_reconnect_seconds = 1.0
        runtime = Runtime(config)
        await runtime.start(serve=False)
        try:
            await asyncio.wait_for(event_seen.wait(), timeout=5)
        finally:
            await runtime.stop()

    message = next(frame for frame in received if frame.get("post_type") == "message")
    assert message["raw_message"] == "reverse push"
    assert message["group_id"] == 2169010946


async def test_reverse_ws_reconnects_after_disconnect(config: RuntimeConfig):
    port = free_port()
    connections: list[float] = []

    async def handler(ws):
        connections.append(asyncio.get_running_loop().time())
        await ws.recv()  # lifecycle
        await ws.close()

    async with websockets.serve(handler, "127.0.0.1", port):
        config.onebot.reverse_ws_urls = [f"ws://127.0.0.1:{port}/ws"]
        config.onebot.reverse_reconnect_seconds = 1.0
        runtime = Runtime(config)
        await runtime.start(serve=False)
        try:
            for _ in range(100):
                if len(connections) >= 2:
                    break
                await asyncio.sleep(0.05)
        finally:
            await runtime.stop()

    assert len(connections) >= 2, f"only {len(connections)} connection(s)"

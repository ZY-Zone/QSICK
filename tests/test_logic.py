from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from qsick.config import RuntimeConfig
from qsick.onebot import models
from qsick.onebot.actions.loader import build_registry, load_catalog
from qsick.onebot.server import EventHub
from qsick.runtime import Runtime
from qsick.transport.base import PacketEvent


def make_config(tmp_path: Path, dedupe: float = 120.0) -> RuntimeConfig:
    config = RuntimeConfig(data_dir=str(tmp_path))
    config.transport.mode = "mock"
    config.plugins.dirs = []
    config.plugins.watch = False
    config.onebot.dedupe_seconds = dedupe
    return config


def message_event(message_id: int, text: str = "hi") -> PacketEvent:
    body = json.dumps(
        {
            "message_id": message_id,
            "chat_type": "group",
            "chat_id": 30001,
            "sender_uin": 10001,
            "sender_name": "tester",
            "elements": [{"type": "text", "data": {"text": text}}],
        }
    ).encode()
    return PacketEvent(cmd="message.group", body=body, kind="packet")


async def test_duplicate_message_events_are_dropped(tmp_path: Path):
    runtime = Runtime(make_config(tmp_path))
    await runtime.start(serve=False)
    try:
        event = message_event(777)
        await runtime._handle(event)
        await runtime._handle(event)
        await runtime._handle(message_event(778))
        assert runtime.events_seen == 3
        assert runtime.events_duplicate == 1
    finally:
        await runtime.stop()


async def test_dedupe_can_be_disabled(tmp_path: Path):
    runtime = Runtime(make_config(tmp_path, dedupe=0))
    await runtime.start(serve=False)
    try:
        event = message_event(777)
        await runtime._handle(event)
        await runtime._handle(event)
        assert runtime.events_duplicate == 0
    finally:
        await runtime.stop()


async def test_send_actions_are_serialised_per_chat(tmp_path: Path):
    runtime = Runtime(make_config(tmp_path))
    await runtime.start(serve=False)
    try:
        order: list[str] = []

        async def fake_dispatch(action: str, params: dict, ctx: object) -> dict:
            order.append("start")
            await asyncio.sleep(0.02)
            order.append("end")
            return models.ok({"message_id": 1})

        runtime.registry.dispatch = fake_dispatch  # type: ignore[method-assign]
        await asyncio.gather(
            runtime.invoke("send_group_msg", {"group_id": 1, "message": "a"}),
            runtime.invoke("send_group_msg", {"group_id": 1, "message": "b"}),
        )
        assert order == ["start", "end", "start", "end"]
        assert runtime.action_stats()["send_group_msg"]["calls"] == 2
    finally:
        await runtime.stop()


async def test_sends_to_different_chats_may_overlap(tmp_path: Path):
    runtime = Runtime(make_config(tmp_path))
    await runtime.start(serve=False)
    try:
        order: list[str] = []

        async def fake_dispatch(action: str, params: dict, ctx: object) -> dict:
            order.append(("start", params.get("group_id")))
            await asyncio.sleep(0.02)
            order.append(("end", params.get("group_id")))
            return models.ok({})

        runtime.registry.dispatch = fake_dispatch  # type: ignore[method-assign]
        await asyncio.gather(
            runtime.invoke("send_group_msg", {"group_id": 1, "message": "a"}),
            runtime.invoke("send_group_msg", {"group_id": 2, "message": "b"}),
        )
        assert order[0][0] == "start" and order[1][1] == 2
    finally:
        await runtime.stop()


def test_registry_rejects_duplicate_names():
    catalog = load_catalog()
    item = catalog["actions"][0]
    with pytest.raises(ValueError, match="duplicate action name"):
        build_registry({"actions": [item, dict(item)], "aliases": {}})
    with pytest.raises(ValueError, match="collides"):
        build_registry({"actions": [item], "aliases": {item["name"]: item["name"]}})


async def test_event_hub_drops_oldest_for_slow_sse():
    hub = EventHub()
    queue = hub.add_sse()
    for index in range(300):
        await hub.broadcast({"seq": index})
    assert hub.stats()["dropped_sse"] > 0
    assert queue.qsize() <= 256


async def test_read_only_queries_are_cached(tmp_path: Path):
    runtime = Runtime(make_config(tmp_path))
    await runtime.start(serve=False)
    try:
        calls = 0
        original = runtime.transport.send

        async def counting(op: str, params: dict | None = None, *, timeout: float | None = None):
            nonlocal calls
            calls += 1
            return await original(op, params, timeout=timeout)

        runtime.transport.send = counting  # type: ignore[method-assign]
        first = await runtime.invoke("get_group_list", {})
        second = await runtime.invoke("get_group_list", {})
        assert first == second
        assert calls == 1
        assert runtime.cache.stats()["hits"] == 1
    finally:
        await runtime.stop()


async def test_onebot_listener_hot_restart(tmp_path: Path):
    config = make_config(tmp_path)
    config.onebot.port = 0
    config.onebot.enable_sse = True
    config.webui.port = 0
    runtime = Runtime(config)
    await runtime.start(serve=True)
    try:
        first = runtime._onebot_server
        assert first is not None and first.started
        assert await runtime.restart_onebot() is True
        second = runtime._onebot_server
        assert second is not None and second is not first and second.started

        runtime.config.onebot.enable_http = False
        runtime.config.onebot.enable_ws = False
        runtime.config.onebot.enable_sse = False
        assert await runtime.restart_onebot() is False
        assert runtime._onebot_server is None
    finally:
        await runtime.stop()

from __future__ import annotations

import asyncio

import pytest

from qsick import log as logmod
from conftest import wait_for


async def test_mock_runtime_ingests_the_demo_message(runtime):
    assert await wait_for(lambda: len(runtime.store) >= 1, timeout=3)
    first = runtime.store.recent(1)[0]
    assert first.is_group
    assert first.chat_id == 30001
    assert first.text == "hello from mock"
    assert runtime.events_seen >= 1


async def test_mock_send_roundtrip_updates_store(runtime):
    before = len(runtime.store)
    result = await runtime.invoke(
        "send_group_msg", {"group_id": 30001, "message": "from test"}
    )
    assert result["status"] == "ok"
    message_id = result["data"]["message_id"]
    stored = runtime.store.get(message_id)
    # message_sent is broadcast asynchronously; wait for it to land
    assert await wait_for(lambda: len(runtime.store) > before, timeout=3)
    assert stored is None or stored.message_id == message_id


async def test_typing_noise_is_trace_only_and_not_forwarded(runtime, monkeypatch):
    forwarded: list[dict] = []

    async def capture(payload):
        forwarded.append(payload)

    monkeypatch.setattr(runtime.hub, "broadcast", capture)
    assert await wait_for(
        lambda: any(r.category == "Noise" and r.level == logmod.TRACE for r in logmod.BUFFER.tail(500)),
        timeout=4,
    )
    for payload in forwarded:
        assert payload.get("notice_type") != "input_status"


async def test_forward_noise_flag_opt_in(runtime, monkeypatch):
    runtime.config.forward_noise = True
    captured: list[dict] = []

    async def capture(payload):
        captured.append(payload)

    monkeypatch.setattr(runtime.hub, "broadcast", capture)
    # wait for the second demo beat: typing arrives after the message
    assert await wait_for(lambda: any(p.get("sub_type") == "input_status" for p in captured), timeout=5)


async def test_guard_drops_before_the_store(runtime):
    from test_guard import message, set_guard

    set_guard(runtime, mode="blacklist", groups={"whitelist": [], "blacklist": [30001]})
    runtime.events_seen = 0
    runtime.events_dropped = 0
    runtime.transport._publish_event  # noqa: B018 - keep attribute referenced for clarity
    await runtime._handle(
        type(
            "E",
            (),
            {
                "cmd": "message.group",
                "kind": "message",
                "json": lambda self=None: {
                    "message_id": 77,
                    "seq": 7,
                    "time": 1,
                    "chat": {"type": "group", "id": 30001},
                    "sender": {"uin": 20001, "nickname": "Alice"},
                    "elements": [{"type": "text", "data": {"text": "blocked"}}],
                },
            },
        )()
    )
    assert runtime.events_dropped == 1
    assert runtime.store.get(77) is None


async def test_info_reports_expected_keys(runtime):
    info = runtime.info()
    for key in ("version", "transport", "uin", "events_seen", "actions", "onebot", "webui"):
        assert key in info
    assert info["transport"] == "mock"
    assert info["actions"] > 100


async def test_rkey_refresh_from_payload(runtime):
    tokens = await runtime.rkey.refresh()
    assert tokens["group"].startswith("MOCK_GROUP_RKEY")
    assert tokens["private"].startswith("MOCK_PRIVATE_RKEY")


async def test_media_resolver_builds_an_image_url(runtime):
    from qsick.protocol import elements as E

    element = E.normalize({"type": "image", "data": {"file_id": "30abc"}})
    url = await runtime.media.url_for(element, scope="group")
    assert url.startswith("https://mock.invalid/") or "rkey=" in url

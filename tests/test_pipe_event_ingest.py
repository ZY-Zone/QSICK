"""An externally reported message must reach the same handlers a transport's own events do.

The chain under test is the one that was silently broken: the injected DLL appends a message to
`<root>/Pipe/owndata/events.jsonl`, the runtime's poller reads it and calls `transport.push`, and `push` puts
it on the transport's event stream -- which `_consume` dispatches exactly like any other event.

The gap was `MockTransport` having no `push` at all. `start.bat` with no argument runs in mock mode, so the
poller found nothing to call and dropped every message: the listener registered, the file grew, and nothing
ever appeared in the store. Only a real subscriber reveals that, which is why this test drives the whole
runtime instead of calling `push` in isolation.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from qsick import headless as headless_module
from qsick.config import RuntimeConfig
from qsick.runtime import Runtime


GROUP_MESSAGE = {
    "message_id": 7710274593888217657,
    "seq": 1,
    "time": 1795188134,
    "chat": {"type": "group", "id": "2169010946"},
    "sender": {"uin": "2208948865", "nickname": "A"},
    "elements": [{"type": "text", "text": "hello"}],
}


def stream_path(root: Path) -> Path:
    path = root / headless_module.PIPE_INSTANCE_ID / "owndata" / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def feed(root: Path, payload: dict) -> None:
    """Append one line the way a session does: while the runtime is running.

    Appending after `start` rather than writing the file beforehand matters: the poller deliberately begins at
    the current end of the stream, because a line written before this process started proves an account *was*
    logged in, not that it is now (see `headless.pipe_event_offset`). Pre-writing the file made the runtime
    learn a dead session's account and open a network config for it.
    """
    with stream_path(root).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload) + "\n")


async def test_a_pipe_event_reaches_the_dispatcher(tmp_path: Path):
    """Seen from the outside: a line in the event file becomes a handled event.

    `_handle` is wrapped so the test observes dispatch without depending on what the store or a protocol
    layer does with a message afterwards -- those have their own tests, and this one is about the wiring.
    """
    root = tmp_path / "headlessClient"
    stream_path(root).write_text("", encoding="utf-8")

    config = RuntimeConfig(data_dir=str(tmp_path / "data"))
    config.transport.mode = "mock"
    config.headless_root = str(root)
    config.log.level = "WARN"

    runtime = Runtime(config)
    handled: list = []
    original = runtime._handle

    async def spy(event):
        handled.append(event)
        return await original(event)

    runtime._handle = spy  # type: ignore[method-assign]
    await runtime.start(serve=False)
    try:
        feed(root, {"type": "message", "uin": "3905548034", "protocol": GROUP_MESSAGE})
        for _ in range(60):
            await __import__("asyncio").sleep(0.05)
            if handled:
                break
    finally:
        await runtime.stop()

    assert handled, "事件没有进入派发"
    event = handled[0]
    assert event.cmd == "message.group"
    assert event.instance == "3905548034", "归属必须是账号，不是客户端目录"
    payload = json.loads(event.body)
    assert payload["message_id"] == GROUP_MESSAGE["message_id"]


async def test_a_notice_line_reaches_the_dispatcher(tmp_path: Path):
    """A recall is the first non-message event a pipe session reports.

    The poller used to handle only `message`/`sent` lines and drop everything else, so a pipe instance
    reported no notices at all -- recalls, members joining or leaving -- while the identical wrappers in
    `HeadlessTransport` worked. Nothing failed; the events simply never existed. That is why anti-recall and
    relay plugins saw nothing on a pipe account.
    """
    root = tmp_path / "headlessClient"
    recall = {
        "type": "notice",
        "cmd": "notice.group_recall",
        "uin": "3905548034",
        "payload": {
            "notice_type": "group_recall",
            "self_id": 3905548034,
            "time": 1795203726,
            "message_id": 7784558310870480810,
            "msg_seq": "1307",
            "user_id": 3905548034,
            "operator_id": 3905548034,
            "group_id": "2169010946",
        },
    }
    stream_path(root).write_text("", encoding="utf-8")

    config = RuntimeConfig(data_dir=str(tmp_path / "data"))
    config.transport.mode = "mock"
    config.headless_root = str(root)
    config.log.level = "WARN"

    runtime = Runtime(config)
    handled: list = []
    original = runtime._handle

    async def spy(event):
        handled.append(event)
        return await original(event)

    runtime._handle = spy  # type: ignore[method-assign]
    await runtime.start(serve=False)
    try:
        feed(root, recall)
        for _ in range(60):
            await __import__("asyncio").sleep(0.05)
            if handled:
                break
    finally:
        await runtime.stop()

    assert handled, "通知没有进入派发"
    event = handled[0]
    assert event.cmd == "notice.group_recall"
    assert event.instance == "3905548034"
    payload = json.loads(event.body)
    assert payload["group_id"] == "2169010946"
    assert payload["message_id"] == 7784558310870480810


def test_a_notice_without_a_matching_cmd_is_ignored(tmp_path: Path):
    """`push` requires `cmd` to start with the kind; a malformed line must not raise inside the poller."""
    root = tmp_path / "headlessClient"
    stream_path(root).write_text(
        json.dumps({"type": "notice", "cmd": "group_recall", "uin": "1", "payload": {}}) + "\n",
        encoding="utf-8")
    # The poller skips it before calling push, so nothing to assert beyond it not blowing up in the reader.
    from qsick import headless as headless_module

    result = headless_module.read_pipe_events(str(root), offsets={})
    assert result.get("error") == ""


async def test_the_pushed_event_matches_napcats_shape(tmp_path: Path):
    """Field-by-field check against NapCat's own message schema.

    Compared with `packages/napcat-onebot/types/message.ts` and its `initializeMessage`, two fields were
    missing: `message_format` (declared as a required union of 'array' | 'string') and, for group messages,
    `group_name` -- which cannot be filled because the normalised payload has no group name.

    `font` is 14 in NapCat rather than 0. QQ NT has no font concept, but consumers echo it back, so matching
    keeps their logs clean.
    """
    from qsick.onebot.events import EventMapper
    from qsick.protocol.message import Message

    mapper = EventMapper(type("C", (), {})(), store=None, media=None, self_id=3905548034)
    message = Message.from_payload({
        "message_id": 7710283723552558614,
        "seq": 245775,
        "time": 1795190322,
        "chat": {"type": "group", "id": "2169010946"},
        "sender": {"uin": "2208948865", "nickname": "A", "card": "卡片", "role": "member"},
        "account": "3905548034",
        "elements": [{"type": "text", "text": "hello"}],
    })

    # `store=None` would break the write; drive only the shaping by stubbing the two async steps.
    mapper.store = type("S", (), {"put": lambda *a, **k: None})()
    event = await mapper.message(message)

    for field in ("post_type", "message_type", "sub_type", "message_id", "real_id",
                  "message_seq", "user_id", "time", "self_id", "message", "raw_message",
                  "font", "sender", "group_id", "message_format"):
        assert field in event, f"缺少 {field}"

    assert event["message_format"] == "array"
    assert event["font"] == 14
    assert event["self_id"] == 3905548034
    assert event["group_id"] == 2169010946
    assert event["sender"]["user_id"] == 2208948865
    assert isinstance(event["message"], list)


async def test_the_account_becomes_self_id_so_the_handshake_can_name_it(tmp_path: Path):
    """`X-Self-ID` is sent once, in the WebSocket handshake, from `runtime.self_id`.

    A pipe session never sends a login frame, so that value stayed 0 while every event carried the real uin.
    A consumer registers the bot under the handshake value, so it then could not match the events and logged
    "找不到对应Bot，忽略消息" for all of them.
    """
    root = tmp_path / "headlessClient"
    stream_path(root).write_text("", encoding="utf-8")

    config = RuntimeConfig(data_dir=str(tmp_path / "data"))
    config.transport.mode = "mock"
    config.headless_root = str(root)
    config.log.level = "WARN"

    runtime = Runtime(config)
    await runtime.start(serve=False)
    try:
        # The mock transport reports a synthetic account (10001) on start. A real pipe transport sends no
        # login frame at all, so this clears it to reproduce the production starting state.
        runtime._logins.clear()
        runtime._primary_instance = None
        assert runtime.self_id == 0, "测试前提：此时还不知道账号"
        feed(root, {"type": "message", "uin": "3905548034", "protocol": GROUP_MESSAGE})
        for _ in range(60):
            await __import__("asyncio").sleep(0.05)
            if runtime.self_id:
                break
        assert runtime.self_id == 3905548034, "收到消息后仍未学到账号"
    finally:
        await runtime.stop()


def test_the_handshake_header_uses_the_learned_account():
    """The header mirrors `runtime.self_id`, so the fix above is what makes it non-zero."""
    from qsick.onebot.reverse import ReverseWebSocketClient

    runtime = type("R", (), {
        "self_id": 3905548034,
        "config": type("C", (), {"onebot": type("O", (), {"access_token": ""})()})(),
    })()
    headers = ReverseWebSocketClient(runtime, ["ws://localhost:2536/OneBotv11"])._headers()

    assert headers["X-Self-ID"] == "3905548034"
    assert headers["X-Client-Role"] == "Universal"


async def test_the_mock_transport_accepts_an_external_message():
    """`push` must exist on the transport `start.bat` actually runs."""
    import asyncio

    from qsick.transport.mock import MockTransport

    transport = MockTransport(emit_demo=False)
    await transport.start()
    try:
        # One iterator: `events()` hands back a fresh stream per call, so taking a new one per read would
        # skip whatever was published between calls.
        stream = transport.events().__aiter__()
        transport.push({"kind": "message", "instance": "3905548034", "payload": GROUP_MESSAGE})
        got = None
        for _ in range(10):
            candidate = await asyncio.wait_for(stream.__anext__(), timeout=3)
            if candidate.cmd.startswith("message."):
                got = candidate
                break
    finally:
        await transport.stop()

    assert got is not None, "推送的消息没有出现在事件流里"
    assert got.cmd == "message.group"
    assert got.account == "3905548034"


def test_a_malformed_external_message_is_rejected():
    """A bad event should raise where it is pushed, not travel on to fail downstream."""
    from qsick.transport.mock import MockTransport

    transport = MockTransport(emit_demo=False)
    with pytest.raises(ValueError):
        transport.push({"kind": "message", "instance": "1",
                        "payload": {**GROUP_MESSAGE, "chat": {"type": "channel", "id": "1"}}})

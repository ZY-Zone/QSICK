"""The real per-session JSONL poller must deliver DLL OLPush frames safely.

The shared runtime/config fixtures use mock transport and an isolated pipe-event
root: these tests never launch QQ or read the operator's event streams.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from conftest import wait_for
from qsick import headless as headless_module
from qsick import runtime as runtime_module


CMD = "trpc.msg.olpush.OlPushService.MsgPush"
MAX_BYTES = 1024 * 1024


@pytest.fixture(autouse=True)
def quiet_mock_transport(monkeypatch):
    """Keep the shared runtime fixture, but do not mix its timed demo traffic into assertions."""
    from qsick.transport.mock import MockTransport

    monkeypatch.setattr(runtime_module.Runtime, "_make_transport", lambda self: MockTransport(emit_demo=False))


def _varint(value: int) -> bytes:
    out = bytearray()
    while value > 127:
        out.append((value & 127) | 128)
        value >>= 7
    out.append(value)
    return bytes(out)


def _field(number: int, value: int | bytes) -> bytes:
    if isinstance(value, int):
        return _varint(number << 3) + _varint(value)
    return _varint((number << 3) | 2) + _varint(len(value)) + value


def _join_frame(uin: str, *, recipient: bool = True) -> bytes:
    """Type 33, joining self: requires ResponseHead uid warm-up before mapping."""
    uid = f"u_self_{uin}".encode()
    response = _field(5, int(uin)) + _field(6, uid) if recipient else b""
    change = _field(1, 7) + _field(3, uid) + _field(4, 131)
    body = _field(1, response) + _field(2, _field(1, 33)) + _field(3, _field(2, change))
    return _field(1, body)


def _packet(data: bytes | None = None, *, uin: str = "111", **overrides) -> dict:
    data = _join_frame(uin) if data is None else data
    return {
        "type": "packet", "uin": uin, "sessionid": f"session-{uin}",
        "cmd": CMD, "seq": 17, "body_hex": data.hex(),
        "byte_length": len(data), "truncated": False, **overrides,
    }


def _append(runtime, *items: dict, session: str = "session-a", stream: str = "packet-events.jsonl") -> Path:
    root = Path(runtime_module._pipe_event_root(runtime.config))
    path = root / "Pipe" / "dll" / session / stream
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for item in items:
            handle.write(json.dumps(item) + "\n")
    return path


def test_reset_queue_files_clears_packet_stream_but_keeps_session_identity(tmp_path):
    session = tmp_path / "Pipe" / "dll" / "session-a"
    session.mkdir(parents=True)
    packet = session / "packet-events.jsonl"
    packet.write_text(json.dumps(_packet()) + "\n", encoding="utf-8")
    identity = session / "qsick_pipe.json"
    identity.write_text('{"sessionid":"session-a"}', encoding="utf-8")
    result = headless_module.reset_queue_files(tmp_path)
    assert result == {"files": 1, "dirs": 0}
    assert not packet.exists()
    assert identity.is_file()


@pytest.mark.parametrize("enabled", [True, False])
async def test_captured_admin_direction_survives_milky_encoding(runtime, monkeypatch, enabled):
    response = _field(5, 111) + _field(6, b"u_self_111")
    admin = _field(1, 7) + _field(4, _field(2 if enabled else 1, _field(1, b"u_self_111")))
    data = _field(1, _field(1, response) + _field(2, _field(1, 44)) + _field(3, _field(2, admin)))
    published = AsyncMock()
    monkeypatch.setattr(runtime, "_publish", published)
    await runtime._feed_pipe_packet(_packet(data))
    event = published.await_args.args[0]
    assert event["sub_type"] == ("set" if enabled else "unset")
    encoded = runtime.pipelines.encode_event("milky", event)
    assert encoded["event_type"] == "group_admin_change"
    assert encoded["data"]["is_set"] is enabled


@pytest.mark.parametrize("stream", ["packet-events.jsonl", "events.jsonl"])
async def test_polling_packets_runs_real_feed_packet_for_two_accounts(runtime, monkeypatch, stream):
    """JSONL -> poller -> real feed_packet -> plugin dispatch -> publish, for both accounts."""
    feed = AsyncMock(wraps=runtime.feed_packet)
    dispatch = AsyncMock(side_effect=lambda event: event)
    publish = AsyncMock()
    monkeypatch.setattr(runtime, "feed_packet", feed)
    monkeypatch.setattr(runtime.plugins, "dispatch_event", dispatch)
    monkeypatch.setattr(runtime, "_publish", publish)
    first, second = _join_frame("111"), _join_frame("222")
    _append(runtime, _packet(first), session="session-a", stream=stream)
    _append(runtime, _packet(second, uin="222"), session="session-b", stream=stream)

    assert await wait_for(lambda: publish.await_count == 2)
    feed.assert_any_await(first, instance="111", account="111")
    feed.assert_any_await(second, instance="222", account="222")
    notices = [call.args[0] for call in publish.await_args_list]
    assert {(e["self_id"], e["user_id"], e["group_id"]) for e in notices} == {
        (111, 111, 7), (222, 222, 7),
    }
    assert all(e["notice_type"] == "group_increase" for e in notices)
    assert dispatch.await_count == 2
    assert runtime._resolve_uid("u_self_111") == "111"
    assert runtime._resolve_uid("u_self_222") == "222"
    assert 7 in runtime._olpush_self_joins["111"]
    assert 7 in runtime._olpush_self_joins["222"]


@pytest.mark.parametrize("overrides", [
    {"uin": ""}, {"uin": "0"}, {"uin": "000"}, {"uin": "01"},
    {"uin": "-1"}, {"uin": "+1"}, {"uin": " 111 "}, {"uin": "１１１"},
    {"uin": 111}, {"uin": True}, {"uin": None}, {"uin": []},
    {"cmd": "other.command"}, {"cmd": CMD + " "}, {"cmd": None},
    {"body_hex": "0"}, {"body_hex": "zz"}, {"body_hex": "00 00"},
    {"body_hex": "00\n"}, {"body_hex": "0x00"}, {"body_hex": ""},
    {"body_hex": None}, {"body_hex": []},
    {"byte_length": 0}, {"byte_length": -1}, {"byte_length": 1},
    {"byte_length": True}, {"byte_length": "1"}, {"byte_length": 1.0},
    {"byte_length": None}, {"truncated": True},
    {"body_hex": _join_frame("222").hex()},
], ids=lambda item: repr(item))
async def test_bad_packet_is_rejected_without_losing_following_packet(runtime, monkeypatch, overrides):
    feed = AsyncMock(return_value=0)
    warning = []
    monkeypatch.setattr(runtime, "feed_packet", feed)
    logger = Mock(wraps=runtime_module.log)
    logger.warn.side_effect = lambda message, **fields: warning.append((message, fields))
    monkeypatch.setattr(runtime_module, "log", logger)
    good = _packet()
    _append(runtime, {**good, **overrides}, good)

    assert await wait_for(lambda: feed.await_count == 1)
    feed.assert_awaited_once_with(_join_frame("111"), instance="111", account="111")
    assert len(warning) == 1
    assert warning[0][0] == "pipe packet rejected"


@pytest.mark.parametrize("missing", ["uin", "cmd", "body_hex", "byte_length"])
async def test_missing_required_packet_fields_do_not_stop_polling(runtime, monkeypatch, missing):
    feed = AsyncMock(return_value=0)
    monkeypatch.setattr(runtime, "feed_packet", feed)
    bad = _packet()
    del bad[missing]
    _append(runtime, bad, _packet())
    assert await wait_for(lambda: feed.await_count == 1)
    feed.assert_awaited_once_with(_join_frame("111"), instance="111", account="111")


async def test_mismatch_cannot_warm_another_accounts_identity(runtime, monkeypatch):
    publish = AsyncMock()
    monkeypatch.setattr(runtime, "_publish", publish)
    _append(runtime, _packet(_join_frame("222"), uin="111"), _packet())
    assert await wait_for(lambda: publish.await_count == 1)
    assert runtime._resolve_uid("u_self_222") == ""
    assert "222" not in runtime._olpush_self_joins
    assert publish.await_args.args[0]["self_id"] == 111


async def test_missing_response_recipient_and_uppercase_hex_are_accepted(runtime, monkeypatch):
    feed = AsyncMock(return_value=0)
    monkeypatch.setattr(runtime, "feed_packet", feed)
    frame = _join_frame("111", recipient=False)
    item = _packet(frame, body_hex=frame.hex().upper())
    del item["truncated"]  # The contract rejects true, not an absent optional marker.
    _append(runtime, item)
    assert await wait_for(lambda: feed.await_count == 1)
    feed.assert_awaited_once_with(frame, instance="111", account="111")


async def test_one_mib_is_delivered_intact_and_larger_frames_warn_and_are_refused(runtime, monkeypatch):
    feed = AsyncMock(return_value=0)
    warnings = []
    monkeypatch.setattr(runtime, "feed_packet", feed)
    logger = Mock(wraps=runtime_module.log)
    logger.warn.side_effect = lambda message, **fields: warnings.append(fields)
    monkeypatch.setattr(runtime_module, "log", logger)
    # A valid protobuf unknown field, so parsing the response header remains cheap.
    data = _field(10, b"a" * (MAX_BYTES - 4))
    assert len(data) == MAX_BYTES
    oversize = _field(10, b"a" * (MAX_BYTES - 3))
    _append(runtime, _packet(oversize), _packet(data, byte_length=MAX_BYTES + 1),
            _packet(oversize, byte_length=1), _packet(data))
    assert await wait_for(lambda: feed.await_count == 1)
    feed.assert_awaited_once_with(data, instance="111", account="111")
    assert len(warnings) == 3
    assert all("1 MiB" in item["error"] and "without truncation" in item["error"] for item in warnings)


async def test_decoder_failure_and_bad_protobuf_do_not_stop_later_poll_rounds(runtime, monkeypatch):
    original = runtime.feed_packet
    feed = AsyncMock(side_effect=[RuntimeError("synthetic decoder failure"), 0])
    monkeypatch.setattr(runtime, "feed_packet", feed)
    _append(runtime, _packet(), _packet())
    assert await wait_for(lambda: feed.await_count == 2)

    # Restore the real decoder and append after the previous batch was consumed.
    monkeypatch.setattr(runtime, "feed_packet", original)
    publish = AsyncMock()
    monkeypatch.setattr(runtime, "_publish", publish)
    _append(runtime, _packet(b"\xff"), _packet())
    assert await wait_for(lambda: publish.await_count == 1)
    assert publish.await_args.args[0]["self_id"] == 111


@pytest.mark.parametrize("broken", [
    b"\xff",  # Truncated wire key.
    b"\x0a\x05abc",  # Incomplete top-level length-delimited body.
    _field(1, _field(2, b"\xff")),  # Malformed content head inside a complete outer envelope.
])
async def test_bad_protobuf_warns_but_valid_unknown_type_stays_quiet(runtime, monkeypatch, broken):
    logger = Mock(wraps=runtime_module.log)
    monkeypatch.setattr(runtime_module, "log", logger)
    feed = AsyncMock(wraps=runtime.feed_packet)
    monkeypatch.setattr(runtime, "feed_packet", feed)
    publish = AsyncMock()
    monkeypatch.setattr(runtime, "_publish", publish)
    unknown = _field(1, _field(2, _field(1, 9999)))
    _append(runtime, _packet(broken), _packet(unknown), _packet())

    assert await wait_for(lambda: publish.await_count == 1)
    logger.warn.assert_called_once()
    assert logger.warn.call_args.args == ("pipe packet rejected",)
    assert "invalid packet protobuf" in logger.warn.call_args.kwargs["error"]
    assert feed.await_count == 2  # Bad wire bytes never reach the silent mapper.
    feed.assert_any_await(unknown, instance="111", account="111")
    assert publish.await_args.args[0]["self_id"] == 111


async def test_partial_line_waits_for_completion_and_normal_message_path_is_unchanged(runtime, monkeypatch):
    feed = AsyncMock(return_value=0)
    monkeypatch.setattr(runtime, "feed_packet", feed)
    # The running poller may already have captured transport.push; observe _handle instead.
    handled = AsyncMock()
    monkeypatch.setattr(runtime, "_handle", handled)
    item = _packet()
    path = _append(runtime)
    line = json.dumps(item)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line[:len(line) // 2])
    # Wait for a second stream's complete packet, proving a poll took place.
    _append(runtime, _packet(uin="222"), session="session-b")
    assert await wait_for(lambda: feed.await_count == 1)
    feed.assert_awaited_once_with(_join_frame("222"), instance="222", account="222")
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line[len(line) // 2:] + "\n")
    payload = {
        "message_id": 123, "seq": 1, "time": 1795188134,
        "chat": {"type": "group", "id": "7"},
        "sender": {"uin": "333", "nickname": "A"},
        "elements": [{"type": "text", "text": "still a message"}],
    }
    _append(runtime, {"type": "message", "uin": "111", "protocol": payload}, stream="events.jsonl")
    assert await wait_for(lambda: feed.await_count == 2 and any(
        call.args[0].cmd == "message.group" for call in handled.await_args_list))
    messages = [call.args[0] for call in handled.await_args_list if call.args[0].cmd == "message.group"]
    assert len(messages) == 1
    assert messages[0].instance == "111"
    assert json.loads(messages[0].body) == payload


async def test_startup_cleanup_uses_the_same_isolated_root_as_reader(config, tmp_path, monkeypatch):
    effective = tmp_path / 'effective'
    cleanup_roots = []
    monkeypatch.setattr(runtime_module, '_pipe_event_root', lambda cfg: str(effective))
    monkeypatch.setattr(headless_module, 'reset_queue_files',
                        lambda root: cleanup_roots.append(Path(root)) or {'files': 0, 'dirs': 0})
    config.headless_auto_start = False
    rt = runtime_module.Runtime(config)
    try:
        await rt.start(serve=False)
        assert cleanup_roots == [effective]
    finally:
        await rt.stop()

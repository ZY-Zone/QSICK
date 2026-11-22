"""A pipe session's messages must reach the normal event pipeline.

What this covers: a pipe session can call every action, but until now nothing reported that a message
*arrived* -- the injected DLL has no callback into QSICK, only the task queue it reads from. So OneBot
forwarding had nothing to forward for pipe instances, while headless instances worked (their host POSTs to
`/api/headless/event`).

The injected side now registers a kernel listener and appends each received message to
`Pipe/owndata/events.jsonl`, and a runtime task tails that file and hands the events to `transport.push` --
the same entry point the headless host uses.

The stream is **shared**: every session runs from the same `Pipe/` client and writes to the same `owndata`,
so one file carries them all and each line names its own `uin`. That was deliberate -- the earlier
per-session directory needed a DLL path change that crashed QQ in ntdll.

These tests pin the halves that can regress silently:

    read_pipe_events   incremental, byte-offset based, tolerant of a half-written line
    _poll_pipe_events  attributes each message to its account and skips unusable ones
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from qsick import headless as headless_module


def stream_path(root: Path) -> Path:
    """The legacy shared stream: `Pipe/owndata/events.jsonl`.

    Kept because an un-migrated session still writes there and the reader must not ignore it.
    """
    path = root / headless_module.PIPE_INSTANCE_ID / "owndata" / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def append_events(root: Path, *items: dict) -> None:
    with stream_path(root).open("a", encoding="utf-8") as handle:
        for item in items:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")


PROTOCOL_MESSAGE = {
    "message_id": 42,
    "sender": {"uin": "111", "nickname": "A"},
    "chat": {"type": "group", "id": "222"},
    "elements": [],
}


def test_events_are_read_incrementally(tmp_path: Path):
    """Only new lines are returned, so polling does not replay history.

    Called every 400 ms, re-reading the whole file would re-deliver every message the client had ever
    seen -- filling the store with duplicates and re-firing every network profile.
    """
    root = tmp_path / "headlessClient"
    append_events(root, {"type": "message", "uin": "111", "protocol": PROTOCOL_MESSAGE})

    offsets: dict[str, int] = {}
    first = headless_module.read_pipe_events(root, offsets=offsets)
    assert len(first["events"]) == 1
    assert first["events"][0]["uin"] == "111"

    second = headless_module.read_pipe_events(root, offsets=offsets)
    assert second["events"] == []

    append_events(root, {"type": "message", "uin": "111", "protocol": PROTOCOL_MESSAGE})
    third = headless_module.read_pipe_events(root, offsets=offsets)
    assert len(third["events"]) == 1


def test_events_from_several_sessions_arrive_on_one_stream(tmp_path: Path):
    """The file is shared, so interleaved lines from different accounts must each keep their own uin.

    Attribution is by the `uin` field, never by which file the line came from -- there is only one file.
    """
    root = tmp_path / "headlessClient"
    append_events(root,
                  {"type": "message", "uin": "111", "protocol": PROTOCOL_MESSAGE},
                  {"type": "message", "uin": "222", "protocol": PROTOCOL_MESSAGE})

    result = headless_module.read_pipe_events(root, offsets={})
    assert [item["uin"] for item in result["events"]] == ["111", "222"]


def test_a_half_written_line_is_left_for_next_time(tmp_path: Path):
    """The writer appends while the reader reads.

    Consuming a partial line would drop that message entirely: the JSON is invalid, so it is skipped, and
    the offset would already have moved past it.
    """
    root = tmp_path / "headlessClient"
    path = stream_path(root)
    path.write_text(json.dumps({"type": "message", "uin": "111"}) + "\n", encoding="utf-8")
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"type":"message","uin":"111"')  # no closing brace, no newline

    offsets: dict[str, int] = {}
    result = headless_module.read_pipe_events(root, offsets=offsets)
    assert len(result["events"]) == 1

    # The writer finishes the line.
    with path.open("a", encoding="utf-8") as handle:
        handle.write(',"protocol":null}\n')
    result = headless_module.read_pipe_events(root, offsets=offsets)
    assert len(result["events"]) == 1
    assert result["events"][0]["uin"] == "111"


def test_a_truncated_file_starts_over(tmp_path: Path):
    """The stream can be cleared between runs; a stale offset must not seek past the new content."""
    root = tmp_path / "headlessClient"
    append_events(root, {"type": "message", "uin": "111"}, {"type": "message", "uin": "111"})

    offsets: dict[str, int] = {}
    headless_module.read_pipe_events(root, offsets=offsets)
    # One offset per stream now, keyed by the path: several sessions are read in the same pass.
    assert offsets[str(stream_path(root))] > 0

    stream_path(root).write_text(
        json.dumps({"type": "message", "uin": "999"}) + "\n", encoding="utf-8")
    result = headless_module.read_pipe_events(root, offsets=offsets)
    assert len(result["events"]) == 1
    assert result["events"][0]["uin"] == "999"


def test_a_missing_stream_is_not_an_error(tmp_path: Path):
    """Before the first message arrives the file does not exist; that is normal, not a failure."""
    result = headless_module.read_pipe_events(tmp_path / "headlessClient", offsets={})
    assert result["events"] == []
    assert result["error"] == ""


async def test_poll_attributes_messages_to_the_account(tmp_path: Path):
    """Each message is pushed under its account, not under the client directory.

    All sessions share one stream, so using `Pipe` as the instance would put several accounts' messages in
    one context -- and network configs are owned per account, so nothing would match.
    """
    from qsick.runtime import Runtime

    root = tmp_path / "headlessClient"
    append_events(root, {"type": "message", "uin": "3905548034", "protocol": PROTOCOL_MESSAGE})

    pushed: list[dict] = []
    runtime = Runtime.__new__(Runtime)          # only the poller is under test
    runtime._stopping = asyncio.Event()
    runtime.config = type("C", (), {"headless_root": str(root)})()

    class FakeTransport:
        def push(self, event):
            pushed.append(event)

    runtime.transport = FakeTransport()

    task = asyncio.create_task(runtime._poll_pipe_events())
    for _ in range(40):
        await asyncio.sleep(0.05)
        if pushed:
            break
    runtime._stopping.set()
    await asyncio.wait_for(task, timeout=3)

    assert pushed, "消息没有被推送"
    assert pushed[0]["instance"] == "3905548034"
    assert pushed[0]["kind"] == "message"
    assert pushed[0]["payload"]["message_id"] == 42


async def test_poll_skips_messages_without_a_normalised_payload(tmp_path: Path):
    """A message whose normalisation failed has no protocol shape, so there is nothing to dispatch.

    The raw message stays in the line for diagnosis; pushing it would make the protocol layer fail on every
    poll instead.
    """
    from qsick.runtime import Runtime

    root = tmp_path / "headlessClient"
    append_events(root, {"type": "message", "uin": "111", "protocol": None, "message": {"raw": True}})

    pushed: list[dict] = []
    runtime = Runtime.__new__(Runtime)
    runtime._stopping = asyncio.Event()
    runtime.config = type("C", (), {"headless_root": str(root)})()

    class FakeTransport:
        def push(self, event):
            pushed.append(event)

    runtime.transport = FakeTransport()

    task = asyncio.create_task(runtime._poll_pipe_events())
    await asyncio.sleep(0.6)
    runtime._stopping.set()
    await asyncio.wait_for(task, timeout=3)

    assert pushed == []


def session_stream_path(root: Path, tmp_id: str) -> Path:
    """Where a migrated session appends events: `Pipe/dll/<tmp_id>/events.jsonl`."""
    path = root / headless_module.PIPE_INSTANCE_ID / "dll" / tmp_id / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def test_each_session_stream_is_read_separately(tmp_path: Path):
    """A session owns its own stream, so which session wrote a line is known from the path.

    The shared stream carried a `uin` per line instead, and with two sessions live a message one of them sent was
    recorded against the other. Reading per-session files is what removes that question.
    """
    root = tmp_path / "headlessClient"
    for tmp_id, uin in (("aaaa", "111"), ("bbbb", "222")):
        with session_stream_path(root, tmp_id).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"type": "message", "uin": uin}) + "\n")

    offsets: dict[str, int] = {}
    result = headless_module.read_pipe_events(root, offsets=offsets)
    assert sorted(item["uin"] for item in result["events"]) == ["111", "222"]

    # Nothing new the second time, and the two streams keep independent offsets.
    again = headless_module.read_pipe_events(root, offsets=offsets)
    assert again["events"] == []
    assert len(offsets) == 2

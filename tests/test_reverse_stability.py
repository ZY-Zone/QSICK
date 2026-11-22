"""The reverse connection must not be rebuilt, nor lose an answer, for no reason.

Both failures were observed live, and both come from the same place: a connection the consumer was actively
using got torn down, and the answers in flight went with it.

  * `reload_reverse()` runs on every login transition. Rebuilding unconditionally dropped the socket, and the
    consumer's pending-request table is per-connection -- so the answers arrived on the replacement where
    nothing awaited their echoes. The consumer logged them as "未知消息" and waited out its 60 s timeout
    re-asking. That is the "request timed out" line in its log, for requests it had already had answered.

  * Frames were handled one at a time inside the read loop, so a slow action held every frame behind it. A
    consumer issues its lookups back to back on connect, so one slow action became a chain of timeouts.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from qsick import network_profiles as netprofiles  # noqa: E402
from qsick.config import RuntimeConfig  # noqa: E402
from qsick.onebot import reverse as reverse_mod  # noqa: E402
from qsick.onebot.reverse import ReverseWebSocketClient  # noqa: E402
from qsick.runtime import Runtime  # noqa: E402
from qsick.transport.base import LoginState  # noqa: E402

ACCOUNT = "3905548034"
URL = "ws://127.0.0.1:2536/onebot"


class _StubLog:
    """Collects `warn` calls. `qsick.log.Logger` refuses instance-level attribute assignment, so the module's
    logger is replaced wholesale rather than patched on the object."""

    def __init__(self, sink: list[tuple[str, dict]]) -> None:
        self.sink = sink

    def warn(self, message: str, **kwargs) -> None:
        self.sink.append((message, kwargs))

    def debug(self, *args, **kwargs) -> None:  # pragma: no cover - not under test
        pass

    def info(self, *args, **kwargs) -> None:  # pragma: no cover - not under test
        pass


def build(tmp_path: Path, *, with_profile: bool = True) -> Runtime:
    config = RuntimeConfig(data_dir=str(tmp_path / "data"))
    config.transport.mode = "mock"
    config.log.level = "WARN"
    runtime = Runtime(config)
    runtime._logins[ACCOUNT] = LoginState(logged_in=True, uin=ACCOUNT, online=True)
    if with_profile:
        netprofiles.save(config, [{
            "id": "p1", "kind": "ws_client", "name": "反向", "enabled": True,
            "url": URL, "protocol": "onebotv11", "instance": ACCOUNT,
        }])
    return runtime


class TestUnchangedTargetsAreKept:
    async def test_an_unchanged_live_client_is_not_replaced(self, tmp_path: Path):
        runtime = build(tmp_path)
        first = ReverseWebSocketClient(runtime, [(URL, ACCOUNT)])
        first.start()
        runtime.reverse = first
        try:
            kept = await runtime.reload_reverse()
            assert kept is True
            assert runtime.reverse is first, "未变化的连接不该被重建——重建会丢在途的响应"
        finally:
            await runtime.reverse.stop()
            await runtime.stop()

    async def test_a_stopped_client_is_replaced(self, tmp_path: Path):
        # The counterpart: a client whose loop has ended must be replaced, otherwise the connection would never
        # come back. Only a working client is preserved.
        runtime = build(tmp_path)
        dead = ReverseWebSocketClient(runtime, [(URL, ACCOUNT)])
        runtime.reverse = dead
        try:
            assert dead.alive() is False
            previous = runtime.reverse
            await runtime.reload_reverse()
            assert runtime.reverse is not previous, "已停止的客户端必须被替换"
        finally:
            await runtime.reverse.stop()
            await runtime.stop()


class TestTargets:
    def test_the_owner_travels_with_the_url(self, tmp_path: Path):
        client = ReverseWebSocketClient(build(tmp_path, with_profile=False),
                                        [("ws://a/onebot", "111"), ("ws://b/onebot", "222")])
        assert client.targets == [("ws://a/onebot", "111"), ("ws://b/onebot", "222")]
        assert client.urls == ["ws://a/onebot", "ws://b/onebot"]

    def test_targets_come_from_the_accounts_own_profiles(self, tmp_path: Path):
        runtime = build(tmp_path)
        assert runtime._reverse_targets() == [(URL, ACCOUNT)]


class TestAnswerRouting:
    async def test_an_event_never_reaches_another_accounts_connection(self, tmp_path: Path):
        # The consumer keys its bot by the handshake's `X-Self-ID`, so an event belonging to another account is
        # either discarded or -- worse -- acted on as if this account had received it.
        client = ReverseWebSocketClient(build(tmp_path, with_profile=False),
                                        [("ws://a/onebot", "111"), ("ws://b/onebot", "222")])
        queue_a: asyncio.Queue[str] = asyncio.Queue()
        queue_b: asyncio.Queue[str] = asyncio.Queue()
        client._queues = {("ws://a/onebot", "111"): queue_a, ("ws://b/onebot", "222"): queue_b}

        await client.broadcast({"self_id": 111, "post_type": "message"})
        assert queue_a.qsize() == 1, "属主应当收到自己的事件"
        assert queue_b.qsize() == 0, "另一个号的事件不该送到这条连接"

    async def test_an_unowned_connection_still_receives_everything(self, tmp_path: Path):
        # The legacy configuration has no owner recorded. Keeping it subscribed is what makes an old
        # `reverse_ws_urls` entry keep working.
        client = ReverseWebSocketClient(build(tmp_path, with_profile=False), ["ws://a/onebot"])
        queue: asyncio.Queue[str] = asyncio.Queue()
        client._queues = {("ws://a/onebot", ""): queue}
        await client.broadcast({"self_id": 111, "post_type": "message"})
        assert queue.qsize() == 1

    async def test_two_accounts_may_share_one_consumer_url(self, tmp_path: Path):
        """The ordinary multi-bot setup: one consumer, one connection per account.

        Keying the connection state by URL collapsed this. The second session overwrote the first one's queue
        and socket, so one account's events had nowhere to go and the surviving connection was treated as the
        other account -- which is exactly "my message went to the wrong bot".
        """
        client = ReverseWebSocketClient(
            build(tmp_path, with_profile=False),
            [("ws://consumer/onebot", "111"), ("ws://consumer/onebot", "222")])
        queue_111: asyncio.Queue[str] = asyncio.Queue()
        queue_222: asyncio.Queue[str] = asyncio.Queue()
        client._queues = {("ws://consumer/onebot", "111"): queue_111,
                          ("ws://consumer/onebot", "222"): queue_222}

        await client.broadcast({"self_id": 111, "post_type": "message"})
        await client.broadcast({"self_id": 222, "post_type": "message"})

        assert queue_111.qsize() == 1, "同一 URL 下第一个号的事件不该丢"
        assert queue_222.qsize() == 1, "同一 URL 下第二个号的事件不该丢"

    async def test_each_connection_keeps_its_own_identity(self, tmp_path: Path):
        # `X-Self-ID` is what the consumer registers the bot under, so it has to be this connection's account --
        # not whatever the runtime's primary account happens to be.
        client = ReverseWebSocketClient(
            build(tmp_path, with_profile=False),
            [("ws://consumer/onebot", "111"), ("ws://consumer/onebot", "222")])
        assert client._headers(111)["X-Self-ID"] == "111"
        assert client._headers(222)["X-Self-ID"] == "222"


class TestLostAnswerIsVisible:
    async def test_a_lost_answer_warns_instead_of_vanishing(self, tmp_path: Path, monkeypatch):
        # The consumer waits its full timeout for a frame it never receives, so losing one silently is what turns
        # a dropped connection into a minute of apparent hangs.
        warnings: list[tuple[str, dict]] = []
        monkeypatch.setattr(reverse_mod, "log", _StubLog(warnings))
        client = ReverseWebSocketClient(build(tmp_path, with_profile=False), [("ws://a/onebot", "111")])

        class Dead:
            async def send(self, _text):
                raise ConnectionResetError("socket is closed")

        await client._send(Dead(), {"status": "ok", "echo": "01M80J", "action": "get_friend_list"})
        assert warnings, "答案发不出去必须留下告警"
        assert "answer lost" in warnings[0][0]
        assert warnings[0][1].get("echo") == "01M80J"

    async def test_an_event_send_failure_stays_quiet(self, tmp_path: Path, monkeypatch):
        # An event has no echo, so nothing is waiting on it; a dropped event is a reconnect, not a hang.
        warnings: list[tuple[str, dict]] = []
        monkeypatch.setattr(reverse_mod, "log", _StubLog(warnings))
        client = ReverseWebSocketClient(build(tmp_path, with_profile=False), [("ws://a/onebot", "111")])

        class Dead:
            async def send(self, _text):
                raise ConnectionResetError("socket is closed")

        await client._send(Dead(), {"post_type": "message", "self_id": 111})
        assert not warnings

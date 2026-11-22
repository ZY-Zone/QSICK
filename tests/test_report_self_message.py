"""`report_self_message` decides per profile, and the event keeps its array shape.

The switch was stored and never read, so turning it off changed nothing: the account's own outgoing messages
still reached the consumer, which then had its own sends quoted back at it as incoming commands. The rule the
operator asked for:

    自身 -> 自身    always forwarded -- writing to your own account is an ordinary conversation
    自身 -> 别人    only to the profiles that enabled the option

Two profiles may disagree, which is why the decision is made per connection rather than once per event.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from qsick import network_profiles as netprofiles  # noqa: E402
from qsick.config import RuntimeConfig  # noqa: E402
from qsick.onebot.reverse import (  # noqa: E402
    ReverseWebSocketClient,
    is_self_sent_to_others,
)
from qsick.runtime import Runtime  # noqa: E402
from qsick.transport.base import LoginState  # noqa: E402

ACCOUNT = "3905548034"
OTHER = "2208948865"
URL = "ws://127.0.0.1:2536/onebot"


def build(tmp_path: Path) -> Runtime:
    config = RuntimeConfig(data_dir=str(tmp_path / "data"))
    config.transport.mode = "mock"
    config.log.level = "WARN"
    return config


class TestClassification:
    def test_a_private_message_to_somebody_else_is_self_sent(self):
        payload = {"post_type": "message_sent", "message_type": "private",
                   "self_id": ACCOUNT, "user_id": ACCOUNT, "target_id": OTHER}
        assert is_self_sent_to_others(payload) is True

    def test_a_private_message_to_itself_is_not(self):
        # 自身 -> 自身: an ordinary conversation, always forwarded.
        payload = {"post_type": "message_sent", "message_type": "private",
                   "self_id": ACCOUNT, "user_id": ACCOUNT, "target_id": ACCOUNT}
        assert is_self_sent_to_others(payload) is False

    def test_a_group_message_we_sent_is_self_sent(self):
        payload = {"post_type": "message_sent", "message_type": "group",
                   "self_id": ACCOUNT, "user_id": ACCOUNT, "group_id": 2169010946}
        assert is_self_sent_to_others(payload) is True

    def test_our_own_group_message_arriving_as_incoming_is_self_sent(self):
        # The same message can come back as a normal `message` event; it must be gated too.
        payload = {"post_type": "message", "message_type": "group",
                   "self_id": ACCOUNT, "user_id": ACCOUNT, "group_id": 2169010946}
        assert is_self_sent_to_others(payload) is True

    def test_somebody_elses_message_is_not(self):
        payload = {"post_type": "message", "message_type": "group",
                   "self_id": ACCOUNT, "user_id": OTHER, "group_id": 2169010946}
        assert is_self_sent_to_others(payload) is False

    def test_an_incoming_private_message_is_not(self):
        payload = {"post_type": "message", "message_type": "private",
                   "self_id": ACCOUNT, "user_id": OTHER, "target_id": ACCOUNT}
        assert is_self_sent_to_others(payload) is False

    def test_a_meta_event_is_not_a_message(self):
        assert is_self_sent_to_others({"post_type": "meta_event", "self_id": ACCOUNT}) is False


class TestPerProfileDelivery:
    def _client(self, tmp_path: Path) -> ReverseWebSocketClient:
        # Two profiles on one consumer URL that disagree about self-messages.
        return ReverseWebSocketClient(
            build(tmp_path),
            [(URL, "111", False), (URL, "222", True)],
        )

    async def test_only_the_reporting_profile_receives_a_self_sent_message(self, tmp_path: Path):
        client = self._client(tmp_path)
        quiet: asyncio.Queue[str] = asyncio.Queue()
        loud: asyncio.Queue[str] = asyncio.Queue()
        client._queues = {(URL, "111"): quiet, (URL, "222"): loud}
        client._report_self = {(URL, "111"): False, (URL, "222"): True}

        payload = {"post_type": "message_sent", "message_type": "private",
                   "self_id": "111", "user_id": "111", "target_id": OTHER,
                   "message": [{"type": "text", "data": {"text": "hi"}}]}
        await client.broadcast(payload)

        assert quiet.qsize() == 0, "关闭上报自身消息的配置不该收到自身->别人"
        # `self_id` routing still applies, so only the 111 connection is eligible at all.
        assert loud.qsize() == 0

    async def test_the_reporting_profile_receives_its_own_self_sent_message(self, tmp_path: Path):
        client = self._client(tmp_path)
        loud: asyncio.Queue[str] = asyncio.Queue()
        client._queues = {(URL, "222"): loud}
        client._report_self = {(URL, "222"): True}

        await client.broadcast({"post_type": "message_sent", "message_type": "private",
                                "self_id": "222", "user_id": "222", "target_id": OTHER})
        assert loud.qsize() == 1

    async def test_self_to_self_reaches_every_profile(self, tmp_path: Path):
        client = self._client(tmp_path)
        quiet: asyncio.Queue[str] = asyncio.Queue()
        client._queues = {(URL, "111"): quiet}
        client._report_self = {(URL, "111"): False}

        # 自身 -> 自身 is forwarded regardless of the option.
        await client.broadcast({"post_type": "message", "message_type": "private",
                                "self_id": "111", "user_id": "111", "target_id": "111"})
        assert quiet.qsize() == 1

    async def test_other_peoples_messages_are_never_filtered(self, tmp_path: Path):
        client = self._client(tmp_path)
        quiet: asyncio.Queue[str] = asyncio.Queue()
        client._queues = {(URL, "111"): quiet}
        client._report_self = {(URL, "111"): False}

        await client.broadcast({"post_type": "message", "message_type": "group",
                                "self_id": "111", "user_id": OTHER, "group_id": 2169010946})
        assert quiet.qsize() == 1, "别人发的消息与这个开关无关"


class TestMessageStaysAnArray:
    async def test_a_forwarded_message_keeps_its_array_shape(self, tmp_path: Path):
        """The consumer is told `message_format: array`; distribution must not turn it into a string."""
        import json

        client = ReverseWebSocketClient(build(tmp_path), [(URL, "111", True)])
        queue: asyncio.Queue[str] = asyncio.Queue()
        client._queues = {(URL, "111"): queue}
        client._report_self = {(URL, "111"): True}

        segments = [{"type": "text", "data": {"text": "hi"}},
                    {"type": "image", "data": {"file": "a.png"}}]
        await client.broadcast({"post_type": "message", "message_type": "group",
                                "self_id": "111", "user_id": OTHER,
                                "group_id": 2169010946, "message_format": "array",
                                "message": segments})

        delivered = json.loads(queue.get_nowait())
        assert isinstance(delivered["message"], list), "message 必须是数组"
        assert delivered["message"] == segments, "分段不能被改写"
        assert delivered["message_format"] == "array"


class TestTargetsCarryTheOption:
    def test_the_profile_option_reaches_the_target_list(self, tmp_path: Path):
        config = build(tmp_path)
        runtime = Runtime(config)
        runtime._logins[ACCOUNT] = LoginState(logged_in=True, uin=ACCOUNT, online=True)
        netprofiles.save(config, [
            {"id": "p1", "kind": "ws_client", "name": "不上报", "enabled": True,
             "url": URL, "protocol": "onebotv11", "instance": ACCOUNT, "report_self_message": False},
            {"id": "p2", "kind": "ws_client", "name": "上报", "enabled": True,
             "url": "ws://127.0.0.1:9999/onebot", "protocol": "onebotv11", "instance": ACCOUNT,
             "report_self_message": True},
        ])
        targets = runtime._reverse_targets()
        assert (URL, ACCOUNT, False) in targets
        assert ("ws://127.0.0.1:9999/onebot", ACCOUNT, True) in targets

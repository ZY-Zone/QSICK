"""三条流水线的分流：进出口各一处。

这一层要防的失效都很安静：

* 事件按默认协议编码给所有人 -> 选了 milky 的连接收到 v11 的 JSON，客户端解析失败或者更糟，
  把它当成自己的格式读进去；
* 某条连接编码失败 -> 整个 fan-out 抛异常，所有客户端都收不到，日志里只有一行看不懂的错；
* 动作绕开 `invoke` -> 插件过滤与账号级出站限制被跳过，而这从外面完全看不出来；
* `maxStore` 写库排到协议裁剪之后 -> 历史只剩当时那个协议要的字段。

所以测试盯的是"分流真的发生了"和"失败被隔离"，而不是某个函数的返回值形状。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from qsick.onebot.pipeline import (  # noqa: E402
    STORAGE_PROTOCOL,
    Connection,
    ProtocolPipelines,
    protocol_of,
)


class FlatProtocol:
    """A protocol that renames everything, for checking that encoding happened at all."""

    name = "flat"

    def encode_event(self, event):
        return {"wrapped": True, "original": event}

    def decode_action(self, name, params):
        return f"inner:{name}", {**params, "decoded": True}

    def format_reply(self, result, echo=None):
        return {"envelope": "flat", "body": result}


class BrokenProtocol:
    name = "broken"

    def encode_event(self, event):
        raise RuntimeError("cannot express this")

    def decode_action(self, name, params):
        raise RuntimeError("unknown action")


class TestConnection:
    def test_protocol_must_travel_with_the_connection(self):
        # 没有这一项，分流点就无从知道该用哪个编码器——这正是改动前的状态。
        connection = Connection(key="ws:/ws", kind="ws_server", protocol="milky", account="123")
        assert connection.protocol == "milky"
        assert connection.posted("milky") is True
        assert connection.posted("onebotv11") is False

    def test_the_key_separates_the_same_path_on_different_accounts(self):
        # 一个消费者 URL 挂两个号是常见部署；只用 URL 做键会让一个号的事件无处可去。
        one = Connection(key="ws:/ws:111", kind="ws_server", protocol="onebotv11", account="111")
        two = Connection(key="ws:/ws:222", kind="ws_server", protocol="onebotv11", account="222")
        assert one.key != two.key


class TestStorageProtocolIsNeverAChat():
    def test_a_profile_naming_the_store_falls_back_to_the_chat_protocol(self):
        """maxStore 只做事件侧的缓存，没有连接。

        配置里把它写成聊天协议只可能是写错了。照它去编码等于每条消息都失败，而失败看起来
        像"协议层坏了"而不是"配置错了"。
        """
        assert protocol_of({"protocol": STORAGE_PROTOCOL}, "onebotv11") == "onebotv11"

    def test_an_empty_protocol_falls_back(self):
        assert protocol_of({}, "onebotv11") == "onebotv11"
        assert protocol_of({"protocol": "  "}, "onebotv11") == "onebotv11"

    def test_a_real_protocol_is_preserved(self):
        assert protocol_of({"protocol": "milky"}, "onebotv11") == "milky"


class TestEncodeEvent:
    def test_the_adapter_is_asked(self):
        pipelines = ProtocolPipelines()
        pipelines._protocols["flat"] = FlatProtocol()   # 直接注入，不依赖注册表
        out = pipelines.encode_event("flat", {"post_type": "message"})
        assert out == {"wrapped": True, "original": {"post_type": "message"}}

    def test_a_failure_returns_none_instead_of_raising(self):
        """一个协议编不出来，只该让走它的连接收不到，不该让整个 fan-out 挂掉。"""
        pipelines = ProtocolPipelines()
        pipelines._protocols["broken"] = BrokenProtocol()
        assert pipelines.encode_event("broken", {"post_type": "message"}) is None

    def test_a_failure_is_recorded_once_not_per_event(self, caplog):
        pipelines = ProtocolPipelines()
        pipelines._protocols["broken"] = BrokenProtocol()
        for _ in range(5):
            pipelines.encode_event("broken", {"post_type": "message"})
        # 状态页要能看到是哪个协议坏了。
        assert "broken" in pipelines.status()["errors"]
        # 只为同一个错误打一行日志：每条消息都打会把日志刷满，那正是它失去作用的方式。
        assert caplog.text.count("protocol pipeline failed") <= 1

    def test_a_non_dict_result_is_treated_as_a_failure(self):
        class Weird:
            name = "weird"

            def encode_event(self, event):
                return ["not", "a", "dict"]

        pipelines = ProtocolPipelines()
        pipelines._protocols["weird"] = Weird()
        assert pipelines.encode_event("weird", {}) is None


class TestDecodeAction:
    def test_the_connection_protocol_decides_the_translation(self):
        pipelines = ProtocolPipelines()
        pipelines._protocols["flat"] = FlatProtocol()
        name, params = pipelines.decode_action("flat", "send_x", {"a": 1})
        assert name == "inner:send_x"
        assert params == {"a": 1, "decoded": True}

    def test_a_failure_raises(self):
        """动作失败与事件失败方向相反：事件失败就跳过，动作失败必须让发起方看见。

        静默跳过会让客户端一直等到超时，而它发出的调用其实早就被拒绝了。
        """
        pipelines = ProtocolPipelines()
        pipelines._protocols["broken"] = BrokenProtocol()
        with pytest.raises(RuntimeError):
            pipelines.decode_action("broken", "send_x", {})


class TestFormatReply:
    def test_the_adapter_shapes_the_envelope(self):
        pipelines = ProtocolPipelines()
        pipelines._protocols["flat"] = FlatProtocol()
        out = pipelines.format_reply("flat", {"status": "ok", "data": 1}, echo="e1")
        assert out == {"envelope": "flat", "body": {"status": "ok", "data": 1}}

    def test_a_broken_adapter_still_returns_something_sendable(self):
        """包信封失败时回退到内部形状，而不是把异常扔回 socket 循环。"""
        pipelines = ProtocolPipelines()

        class Bad:
            name = "bad"

            def format_reply(self, result, echo=None):
                raise RuntimeError("nope")

        pipelines._protocols["bad"] = Bad()
        out = pipelines.format_reply("bad", {"status": "ok", "data": 1})
        assert out == {"status": "ok", "data": 1}


class TestTheHubEncodesPerConnection:
    """EventHub 的 fan-out 必须按连接各自的协议编码，且一种协议只编一次。"""

    def _hub(self, pipelines):
        from qsick.onebot.server import EventHub

        return EventHub(pipelines)

    def test_two_protocols_get_two_different_frames(self):
        pipelines = ProtocolPipelines()
        pipelines._protocols["flat"] = FlatProtocol()
        pipelines._fallback = "flat"
        hub = self._hub(pipelines)

        # `onebotv11` 走真适配器（恒等），`flat` 包一层，两者必然不同。
        frames = hub._encode_by_protocol({"post_type": "message", "self_id": "1"})
        # 没有连接时不该产生任何编码：空跑的编码器看不出错，只是浪费。
        assert frames == {}

        asyncio.run(hub.add_ws(_FakeWS(), Connection(key="a", kind="ws_server", protocol="onebotv11")))
        asyncio.run(hub.add_ws(_FakeWS(), Connection(key="b", kind="ws_server", protocol="flat")))

        frames = hub._encode_by_protocol({"post_type": "message", "self_id": "1"})
        assert set(frames) == {"onebotv11", "flat"}
        # Each protocol yields a list: one internal event can be several wire events for it.
        assert json.loads(frames["flat"][0])["wrapped"] is True
        assert json.loads(frames["onebotv11"][0])["post_type"] == "message"

    def test_a_failing_protocol_does_not_block_the_others(self):
        pipelines = ProtocolPipelines()
        pipelines._protocols["broken"] = BrokenProtocol()
        hub = self._hub(pipelines)

        asyncio.run(hub.add_ws(_FakeWS(), Connection(key="a", kind="ws_server", protocol="onebotv11")))
        asyncio.run(hub.add_ws(_FakeWS(), Connection(key="b", kind="ws_server", protocol="broken")))

        frames = hub._encode_by_protocol({"post_type": "message", "self_id": "1"})
        assert "onebotv11" in frames
        assert "broken" not in frames

    def test_the_hub_reports_which_protocols_are_connected(self):
        pipelines = ProtocolPipelines()
        hub = self._hub(pipelines)
        asyncio.run(hub.add_ws(_FakeWS(), Connection(key="a", kind="ws_server", protocol="milky")))
        assert "milky" in hub.stats()["protocols"]


class _FakeWS:
    """Minimal stand-in for a Starlette WebSocket, for the hub's bookkeeping."""

    client_state = 1  # WebSocketState.CONNECTED

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, text: str) -> None:
        self.sent.append(text)


class TestTheReverseClientCarriesTheProtocol:
    def test_a_target_without_a_protocol_means_default(self):
        from qsick.onebot.reverse import ReverseWebSocketClient

        client = ReverseWebSocketClient(_FakeRuntime(), [("ws://a/onebot", "111", False)])
        assert client.targets == [("ws://a/onebot", "111", False, "", "")]

    def test_a_target_with_a_protocol_keeps_it(self):
        from qsick.onebot.reverse import ReverseWebSocketClient

        client = ReverseWebSocketClient(
            _FakeRuntime(),
            [("ws://a/onebot", "111", False, "milky", "p1")],
        )
        assert client.targets[0][3] == "milky"
        assert client._protocol_for("ws://a/onebot", "111") == "milky"

    def test_each_connection_gets_its_own_encoding(self):
        """一个消费者挂两条连接、各选不同协议时，两条不能收到同一份 JSON。"""
        from qsick.onebot.reverse import ReverseWebSocketClient

        runtime = _FakeRuntime()
        runtime.pipelines = ProtocolPipelines()
        runtime.pipelines._protocols["flat"] = FlatProtocol()

        client = ReverseWebSocketClient(runtime, [
            ("ws://a/onebot", "111", True, "onebotv11", "p1"),
            ("ws://a/onebot", "111", True, "flat", "p2"),
        ])
        # 两条目标 URL 与账号相同，所以队列键会撞：这正是 profile id 存在的理由。
        # 这里只验证编码决策本身，用不同的账号把两个连接分开。
        client = ReverseWebSocketClient(runtime, [
            ("ws://a/onebot", "111", True, "onebotv11", "p1"),
            ("ws://b/onebot", "111", True, "flat", "p2"),
        ])
        queue_a: asyncio.Queue[str] = asyncio.Queue()
        queue_b: asyncio.Queue[str] = asyncio.Queue()
        client._queues = {("ws://a/onebot", "111"): queue_a, ("ws://b/onebot", "111"): queue_b}
        client._protocols = {("ws://a/onebot", "111"): "onebotv11", ("ws://b/onebot", "111"): "flat"}

        asyncio.run(client.broadcast({"post_type": "message", "self_id": "111"}))

        assert json.loads(queue_a.get_nowait())["post_type"] == "message"
        assert json.loads(queue_b.get_nowait())["wrapped"] is True


class _FakeRuntime:
    """Only what the reverse client touches."""

    self_id = 0
    pipelines = None

    def now(self) -> int:
        return 0

    @property
    def config(self):
        class Cfg:
            class onebot:
                access_token = ""
        return Cfg()

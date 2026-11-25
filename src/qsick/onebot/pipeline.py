"""三条流水线：`milky` / `onebotv11` / `maxStore`。

## 为什么要有这一层

在此之前，协议字段是**装饰**：网络配置里存了 `protocol`，界面上有下拉框，但一条消息
进来后所有连接收到的 JSON 完全相同，动作进来时也不问是哪条连接发的。`docs/PROTOCOLS.md`
写的"按每条配置选定的协议编码"只是意图。

这一层把意图变成结构。分流点只有两处，进出口各一处：

    进（事件推送）
      QQ 宿主 ──▶ runtime._dispatch_event
                     │  ① maxStore 流水线：原始 payload 落该实例的库（先于一切裁剪）
                     │  ② onebotv11 / milky：按**每条连接自己的协议**编码后投递
                     ▼
                  各连接

    出（动作执行）
      连接收到调用 ──▶ pipeline.decode_action(该连接的协议) ──▶ runtime.invoke
                        │                                       │
                        └─ 协议名/参数翻译成内部名              └─ 插件、账号限制、
                                                                  缓存、发送锁全在这里

## 三条流水线各自的职责

| 流水线 | 事件推送 | 动作执行 | 说明 |
| --- | --- | --- | --- |
| `maxStore` | ✅ | ❌ | 只写消息缓存。它是**汇**不是通道：没有连接、没有端口 |
| `onebotv11` | ✅ | ✅ | 默认协议；事件本来就是 v11 形状，转换是恒等 |
| `milky` | ✅ | ✅ | 事件与调用名都要翻译 |

`maxStore` 没有动作侧，因为它不是聊天协议——协议注册表里它本来就被禁止选作聊天协议。
把它摆在这一层是为了让"事件先进库再分发"这个次序**看得见**：它此前藏在
`_dispatch_event` 的注释里，一次重构就可能被挪到裁剪之后，那时历史数据就只剩当时
那个协议要的字段了。

## 为什么要按连接记协议

`EventHub` 原先只往集合里放 `WebSocket` 对象，即使想按协议编码也无从知道该用哪个。
所以 `Connection` 这个值对象随连接一起登记：谁连上来的、用哪条配置、什么协议、
哪个账号，一次说清。分流点只读它，不认识任何具体协议。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .. import log as logmod

log = logmod.get_logger("OneBot.Pipeline")

#: maxStore 是存储协议，永远不作为聊天协议出现在连接上。
STORAGE_PROTOCOL = "maxStore"


@dataclass(frozen=True, slots=True)
class Connection:
    """一条活着的连接，以及它是谁。

    ``key`` 必须全局唯一：同一份配置可以同时挂 HTTP 与 WS，同一个 URL 可以服务多个
    账号。用 profile id 单独做键会在多账号下撞车，用 URL 做键会在"一个消费者挂两个
    号"时撞车——后者曾经让一个账号的事件无处可去、动作按另一个号执行。
    """

    key: str
    kind: str
    protocol: str
    account: str = ""
    profile_id: str = ""
    instance: str = ""
    #: 该配置是否要收到账号自己发出去的消息（反向 WS 的 `report_self_message`）。
    report_self: bool = True

    def posted(self, protocol: str) -> bool:
        """这条连接是否走指定协议。"""
        return self.protocol == protocol


@dataclass(slots=True)
class ProtocolPipelines:
    """按协议缓存适配器，并保证"一种协议只编码一次"。

    缓存是必须的：编码器无状态，但每条消息都要用；按连接各构造一份只是浪费。而
    "一种协议只编码一次"是因为同一个协议的 N 条连接要的是同一份 JSON——编码 N 遍
    既慢又可能给出不一致的结果（编码器里若有排序，两次调用会不会得到同一份取决于
    实现细节）。

    编码失败按协议隔离：某个协议编不出来，只跳过走它的那些连接，其余照发。一条消息
    喂不了某个协议，不该让所有客户端都收不到。
    """

    _protocols: dict[str, Any] = field(default_factory=dict)
    #: 记录每个协议最近一次失败的原因，供 /api/status 显示。失败是静默的灾难：
    #: 客户端"什么都没收到"而服务端没有一行日志时，没人能查出是哪一层丢了。
    _errors: dict[str, str] = field(default_factory=dict)
    _fallback: str = ""

    def protocol(self, protocol_id: str) -> Any:
        """取（并缓存）一个协议的适配器。空 id 回落到默认协议。"""
        wanted = str(protocol_id or "").strip() or self.default()
        cached = self._protocols.get(wanted)
        if cached is not None:
            return cached
        from ..protocol_bridge import load_protocol

        adapter = load_protocol(wanted)
        self._protocols[wanted] = adapter
        return adapter

    def default(self) -> str:
        """默认聊天协议。

        空串表示"还没读过"；读出来是空表（清单坏了）时回落到 ``onebotv11``——
        一个常量比"没有协议"好：后者会让所有事件都无处可去。
        """
        if not self._fallback:
            from ..protocol_bridge import protocol_names

            names = protocol_names()
            self._fallback = names[0] if names else "onebotv11"
        return self._fallback

    # ------------------------------------------------------------------ 出口

    def encode_event(self, protocol_id: str, event: dict[str, Any]) -> dict[str, Any] | None:
        """把内部事件翻译成某协议的事件。失败返回 ``None`` 并记下原因。"""
        encoded = self.encode_events(protocol_id, event)
        return encoded[0] if encoded else None

    def encode_events(self, protocol_id: str, event: dict[str, Any]) -> list[dict[str, Any]]:
        """一个内部事件 -> 该协议的若干事件（通常只有一条）。

        A protocol can genuinely need more than one wire event for one internal event. Milky is
        the case that forced this: a private message carrying a file is both `message_receive`
        and `friend_file_upload`, and the spec defines the latter as its own event rather than a
        field of the message. Encoding once and delivering the list preserves the "encode once
        per protocol" rule that stops two consumers of one protocol from receiving different JSON.

        Adapters that need only one event expose no `encode_events`; their `encode_event` result
        is normalized here, so both shapes work.
        """
        try:
            adapter = self.protocol(protocol_id)
            hook = getattr(adapter, "encode_events", None)
            produced = hook(event) if callable(hook) else adapter.encode_event(event)
        except Exception as exc:  # noqa: BLE001 - 一个协议失败只影响它自己
            self._note_error(protocol_id, exc)
            return []
        if isinstance(produced, dict):
            produced = [produced]
        if not isinstance(produced, (list, tuple)):
            self._note_error(protocol_id, TypeError(f"encode_event 返回 {type(produced).__name__}"))
            return []
        frames = [item for item in produced if isinstance(item, dict)]
        if not frames:
            # Nothing usable: keep the reason rather than reporting success with an empty list.
            self._note_error(protocol_id, TypeError("encode returned no event object"))
            return []
        self._errors.pop(str(protocol_id or ""), None)
        return frames

    def format_reply(self, protocol_id: str, result: dict[str, Any], echo: Any = None) -> dict[str, Any]:
        """把内部动作结果包成某协议的应答信封。"""
        try:
            return self.protocol(protocol_id).format_reply(result, echo)
        except Exception as exc:  # noqa: BLE001
            self._note_error(protocol_id, exc)
            # 宁可回一个最小但正确的 v11 信封，也不要把异常扔回 socket 循环。
            reply = dict(result or {})
            if echo is not None:
                reply["echo"] = echo
            return reply

    # ------------------------------------------------------------------ 入口

    def decode_action(self, protocol_id: str, name: str, params: dict[str, Any]
                      ) -> tuple[str, dict[str, Any]]:
        """把某协议的调用翻译成内部动作名与参数。

        翻译失败向上抛：调用方要回一个"不认识这个动作"，这与"事件编码失败就跳过这条
        连接"不同——动作的失败必须让发起方看见，否则它只能等到超时。
        """
        return self.protocol(protocol_id).decode_action(name, dict(params or {}))

    # ---------------------------------------------------------------- 诊断

    def _note_error(self, protocol_id: str, exc: BaseException) -> None:
        key = str(protocol_id or "")
        message = f"{type(exc).__name__}: {exc}"
        # 只在从"正常"变成"出错"时打一行 WARN。每条消息都打会让日志被同一个错误刷满，
        # 那正是它失去作用的方式。
        if self._errors.get(key) != message:
            log.warn("protocol pipeline failed", protocol=key or "(default)", error=message)
        self._errors[key] = message

    def status(self) -> dict[str, Any]:
        return {
            "loaded": sorted(self._protocols),
            "default": self.default(),
            "errors": dict(self._errors),
        }


def protocol_of(profile: dict[str, Any] | None, fallback: str = "") -> str:
    """一条网络配置该用哪个协议。

    存储协议没有连接，所以它出现在这里只可能是配置写错了：回落到默认聊天协议而不是
    照它去编码——那样每条消息都会失败，而失败看起来像"协议层坏了"。
    """
    raw = str((profile or {}).get("protocol") or "").strip()
    if not raw or raw == STORAGE_PROTOCOL:
        return fallback
    return raw

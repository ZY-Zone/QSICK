"""消息处理器：在协议层与运行时之间搬运消息与动作。

存在意义很具体：同一个运行时里可能同时挂着「纯 OneBot v11」和「OneBot v11 +
EXTemotions」两种网络配置，聊天栏又是第三种消费方。如果各自直接对着运行时的
内部结构读写，每加一处就要加一遍转换逻辑，格式迟早走偏。

所以所有跨界的地方都过这里，只做两件事：

1. **分发**：把内部部件按目标网络配置选定的协议编码好再交出去；
2. **回收**：把各协议报上来的段解码成内部部件，再交给存储与聊天栏。

动作（发送消息、撤回、踢人……）的执行本身不在这里——那属于
``src/qsick/onebot/``。这里的 ``run_action`` 只负责把参数按协议编码、把结果
按协议解回内部形状，执行交给注入的 ``dispatch`` 可调用对象。
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from . import registry
from .base import (
    KIND_EMOTION,
    KIND_TEXT,
    KIND_UNKNOWN,
    PartError,
    normalize_part,
)

_LOGGER = logging.getLogger(__name__)


class DispatchError(RuntimeError):
    """消息或动作无法按目标协议表达。"""


class MessageProcessor:
    """按协议编码/解码消息与动作。

    ``dispatch`` 是执行动作的可调用对象，签名 ``(action, params) -> awaitable``，
    通常就是运行时的动作注册表。传 ``None`` 时 :meth:`run_action` 只做编码，
    便于单独测试协议转换。
    """

    def __init__(self, protocol_id: str | None = None,
                 dispatch: Callable[[str, dict[str, Any]], Any] | None = None,
                 *, enrich: Callable[[dict[str, Any]], dict[str, Any]] | None = None) -> None:
        self._protocol_id = protocol_id or registry.default_protocol_id()
        self._protocol = registry.load_protocol(self._protocol_id)
        self._dispatch = dispatch
        # ``enrich`` 把只有 id 的表情补成完整的 emotion 对象（查本地表情表、
        # 嗅探格式）。注入而不是直接 import，是因为它属于上层目录知识，
        # 协议层不该反向依赖 WebUI。
        self._enrich = enrich

    # ---------------------------------------------------------------- 属性

    @property
    def protocol_id(self) -> str:
        return self._protocol_id

    @property
    def protocol(self) -> Any:
        return self._protocol

    def capabilities(self) -> dict[str, Any]:
        return self._protocol.capabilities()

    # ------------------------------------------------------------ 出站编码

    def prepare(self, parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """把内部部件整理成该协议能表达的形状。

        表情在编码前先补全成完整对象：只带 ``id`` 的 emotion 在纯 v11 里够用，
        但在 EXTemotions 里缺 ``moveable``/``fileType`` 就会退化成"不知道是不是
        动态"，正是要修的毛病。
        """
        prepared: list[dict[str, Any]] = []
        for raw in parts:
            part = normalize_part(raw)
            if part["kind"] == KIND_EMOTION and self._enrich is not None:
                part = {"kind": KIND_EMOTION, "emotion": self._enrich(part["emotion"])}
            prepared.append(part)
        return prepared

    def encode_outbound(self, parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """内部部件 -> 目标协议的消息段，可直接发给网络配置。"""
        if not parts:
            # 空消息在哪个协议里都非法，与其发出去让对端拒绝，不如在本地说清楚。
            raise DispatchError("消息内容为空")
        return self._protocol.encode_message(self.prepare(parts))

    def encode_text(self, text: str) -> list[dict[str, Any]]:
        """纯文本的快捷方式。"""
        return self.encode_outbound([{"kind": KIND_TEXT, "text": text}])

    # ------------------------------------------------------------ 入站解码

    def decode_inbound(self, segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """目标协议的消息段 -> 内部部件。

        解码出的 emotion 可能仍只有 ``id``：那是对端真实的表达，不在这里补全。
        补全属于"我们查到的"而非"对端说的"，混在一起就分不清消息原本是什么样了。
        """
        if not isinstance(segments, list):
            raise DispatchError("消息段必须是数组")
        return self._protocol.decode_message(segments)

    def summarize(self, parts: list[dict[str, Any]]) -> str:
        """给会话列表用的一行摘要。表情按它的占位语义显示，不显示 id。"""
        pieces: list[str] = []
        for part in parts:
            if part.get("kind") == KIND_TEXT:
                pieces.append(str(part.get("text") or ""))
            elif part.get("kind") == KIND_EMOTION:
                emotion = part.get("emotion") or {}
                summary = str(emotion.get("summary") or "")
                if summary:
                    pieces.append(summary)
                else:
                    name = str(emotion.get("name") or "")
                    pieces.append(f"[{name}]" if name else "[表情]")
            elif part.get("kind") == KIND_UNKNOWN:
                pieces.append(f"[{part.get('type') or '未知'}]")
            else:
                pieces.append(f"[{part.get('kind')}]")
        return "".join(pieces).strip()

    # ---------------------------------------------------------------- 动作

    async def run_action(self, action: str, params: dict[str, Any] | None = None) -> Any:
        """按目标协议执行一个动作。

        编码走协议层，执行走运行时；两边都不越界。``encode_action`` 返回的是
        ``{"action": ..., "params": ...}`` 信封（协议可以把动作名也改写），所以
        执行时用信封里的名字与参数，而不是调用方原始的 ``action``。
        没有注入 ``dispatch`` 时只返回编码结果，方便单测协议转换本身。
        """
        encoded = self._protocol.encode_action(action, dict(params or {}))
        if self._dispatch is None:
            return encoded
        if isinstance(encoded, dict) and "action" in encoded:
            name = str(encoded.get("action") or action)
            payload = encoded.get("params")
            return await self._dispatch(name, dict(payload) if isinstance(payload, dict) else {})
        return await self._dispatch(action, dict(params or {}))

    async def send_message(self, target: dict[str, Any],
                           parts: list[dict[str, Any]]) -> Any:
        """把一条消息发到指定会话。``target`` 形如 ``{"kind":"group","id":"123"}``。"""
        if not isinstance(target, dict):
            raise DispatchError("target 必须是对象")
        kind = str(target.get("kind") or "")
        chat_id = str(target.get("id") or "").strip()
        if kind not in ("group", "private"):
            raise DispatchError(f"不支持的会话类型 {kind!r}")
        if not chat_id:
            raise DispatchError("target 缺少 id")
        segments = self.encode_outbound(parts)
        action = "send_group_msg" if kind == "group" else "send_private_msg"
        params = {"group_id" if kind == "group" else "user_id": int(chat_id),
                  "message": segments}
        return await self.run_action(action, params)


def processor_for(protocol_id: str | None = None,
                  dispatch: Callable[[str, dict[str, Any]], Any] | None = None,
                  *, enrich: Callable[[dict[str, Any]], dict[str, Any]] | None = None
                  ) -> MessageProcessor:
    """构造一个处理器；协议 id 非法时回落到默认协议并记日志。

    回落而不是抛异常：一条网络配置写错了协议名，不该让整个运行时起不来。
    """
    try:
        return MessageProcessor(protocol_id, dispatch, enrich=enrich)
    except (registry.ProtocolError, PartError) as exc:
        fallback = registry.default_protocol_id()
        _LOGGER.warning("协议 %r 不可用（%s），回落到 %r", protocol_id, exc, fallback)
        return MessageProcessor(fallback, dispatch, enrich=enrich)

"""Transport contract shared by the mock and the native payload bridge."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator


class TransportError(RuntimeError):
    """The transport could not connect, or the payload rejected the call."""

    def __init__(self, message: str, *, code: int = -1) -> None:
        super().__init__(message)
        self.code = code


@dataclass(slots=True)
class LoginState:
    logged_in: bool = False
    uin: str = "0"
    nickname: str = ""
    online: bool = True
    # "被踢"是一个需要持续展示的状态，不只是"没登录"：QQ 主动结束了会话，
    # 用户需要知道发生了什么（换设备登录？安全策略？）。丢掉这两个字段，
    # 界面就只剩一句无从查起的"未登录"。
    kicked: bool = False
    kick_reason: str = ""
    security_kicked: bool = False


@dataclass(slots=True)
class SendResult:
    ok: bool
    status: int = 0
    message: str = ""
    body: bytes = b""
    # Decoded payload for convenience (the payload returns JSON on the wire).
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def failure(cls, message: str, status: int = -1) -> "SendResult":
        return cls(ok=False, status=status, message=message)


@dataclass(slots=True)
class PacketEvent:
    """One thing the payload observed inside QQ."""

    cmd: str
    body: bytes = b""
    value: int = 0
    kind: str = "packet"  # packet | login | runtime
    #: 产生这条事件的实例（C0、C1 …）。空串表示单通道载具（mock/pipe），
    #: 由网络配置的账号过滤按"不限制"处理。
    instance: str = ""
    #: 该实例登录的 QQ 号。消息/事件要用它作为 ``self_id``，多实例下这个值
    #: 不能从全局登录状态里读——那会给 C1 的消息打上 C0 的号。
    account: str = ""

    def json(self) -> dict[str, Any]:
        import json

        if not self.body:
            return {}
        try:
            parsed = json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}


class Transport(ABC):
    """Payload-agnostic I/O.

    Implementations must be safe to call from the asyncio loop thread; the pipe
    implementation marshals blocking reads onto a worker thread.
    """

    name = "base"

    @property
    @abstractmethod
    def login(self) -> LoginState: ...

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def send(
        self,
        op: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> SendResult:
        """Call a high-level payload operation (``message.send``, ``group.mute``...)."""

    @abstractmethod
    def events(self) -> AsyncIterator[PacketEvent]:
        """Yield payload events until :meth:`stop`."""

    async def ping(self) -> float:
        """Round-trip time in milliseconds; overridden by real transports."""
        return 0.0


class EventFanout:
    """Bridges a callback-based producer to asyncio consumers."""

    def __init__(self) -> None:
        self._queues: set[asyncio.Queue[PacketEvent]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def publish(self, event: PacketEvent) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(self._deliver, event)

    def _deliver(self, event: PacketEvent) -> None:
        for queue in list(self._queues):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # Slow consumer: drop the oldest rather than blocking the payload.
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    queue.put_nowait(event)
                except asyncio.QueueFull:
                    pass

    async def stream(self, maxsize: int = 1024) -> AsyncIterator[PacketEvent]:
        queue: asyncio.Queue[PacketEvent] = asyncio.Queue(maxsize=maxsize)
        self._queues.add(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            self._queues.discard(queue)

    def close(self) -> None:
        self._queues.clear()

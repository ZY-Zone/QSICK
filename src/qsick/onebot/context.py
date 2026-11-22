"""Everything an action handler is allowed to touch."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..config import RuntimeConfig
from ..protocol.media import MediaResolver
from ..protocol.rkey import RKeyManager
from ..storage import MessageStore
from ..transport.base import LoginState, SendResult, Transport
from .cache import TTLCache

if TYPE_CHECKING:  # pragma: no cover
    from ..runtime import Runtime


@dataclass(slots=True)
class ActionContext:
    runtime: "Runtime"
    transport: Transport
    config: RuntimeConfig
    store: MessageStore
    rkey: RKeyManager
    media: MediaResolver
    login: LoginState
    cache: TTLCache = field(default_factory=TTLCache)
    #: Which account must execute this request.
    #:
    #: Set from the request's `account` parameter and used by `actions.base.call` to stamp every transport call.
    #: Without it each handler built its own payload and dropped the account, so the transport could only guess a
    #: session -- with one session running that guess was right, with two it addressed the wrong account or none.
    account: str = ""

    @property
    def self_id(self) -> int:
        try:
            return int(self.login.uin)
        except (TypeError, ValueError):
            return 0

    @property
    def protocol(self) -> str:
        """The chat protocol this instance speaks, e.g. ``onebotv11``.

        Read from the instance profile rather than the runtime config because the protocol is a
        per-network-profile setting: two instances can run different protocols from the same config.

        The instance name comes from the runtime's primary instance, since an action does not carry one
        -- the OneBot server is per-instance but the action context is built once. Empty when the
        instance cannot be resolved, which callers treat as "no protocol-specific handling" rather than
        an error: a missing profile must not make actions fail.
        """
        runtime = self.runtime
        try:
            hub = getattr(runtime, "instances", None)
            if hub is None:
                return ""
            name = str(getattr(runtime, "_primary_instance", "") or "C0")
            return str(getattr(hub.instance(name), "protocol", "") or "")
        except Exception:  # noqa: BLE001
            return ""

    async def call(self, op: str, params: dict[str, Any] | None = None, *, timeout: float | None = None) -> SendResult:
        return await self.transport.send(op, params, timeout=timeout)

"""The reverse-WS identity watcher must fire once, or not at all.

`X-Self-ID` goes out in the handshake, and a consumer registers the bot under that value. A pipe session has
no login frame, so the header went out as "0" while every event carried the real uin -- the consumer then
could not match the bot and dropped all messages ("找不到对应Bot，忽略消息").

The fix drops the connection once the account is known so the next handshake can name it. Getting that wrong
is worse than not having it: closing on *any* identity change makes the client flap, and every close loses
whatever actions the consumer had in flight. Measured: closing per change drove the reconnect backoff to its
300 s ceiling and the consumer reported plain "请求超时" on every action, with nothing in QSICK looking broken.

So these pin the two properties that keep it safe:

    * it never fires when the handshake already carried a real id
    * it fires exactly once for the 0 -> uin transition, then stops
"""
from __future__ import annotations

import asyncio

import pytest

from qsick.onebot.reverse import ReverseWebSocketClient


class FakeSocket:
    def __init__(self) -> None:
        self.closed = 0

    async def close(self) -> None:
        self.closed += 1


class FakeRuntime:
    def __init__(self, self_id: int) -> None:
        self.self_id = self_id


def client(self_id: int) -> ReverseWebSocketClient:
    runtime = FakeRuntime(self_id)
    runtime.config = type("C", (), {"onebot": type("O", (), {"access_token": ""})()})()
    return ReverseWebSocketClient(runtime, ["ws://localhost:2536/OneBotv11"])


async def test_a_known_identity_never_closes_the_socket():
    """The common case: the account was known before the handshake. Nothing to fix, nothing to drop."""
    c = client(3905548034)
    socket = FakeSocket()
    # Returns at once rather than watching -- there is nothing it could usefully do.
    await asyncio.wait_for(c._reconnect_when_identity_known(socket, 3905548034), timeout=2)
    assert socket.closed == 0


async def test_the_zero_to_uin_transition_closes_exactly_once():
    c = client(0)
    socket = FakeSocket()
    task = asyncio.create_task(c._reconnect_when_identity_known(socket, 0))
    # Let one poll observe self_id == 0, then publish the account.
    await asyncio.sleep(1.2)
    assert socket.closed == 0, "账号还未知时不该断开"
    c.runtime.self_id = 3905548034
    await asyncio.wait_for(task, timeout=3)
    assert socket.closed == 1, "应当断开一次以便重新握手"


async def test_it_does_not_keep_watching_after_the_identity_is_known():
    """After the one reconnect the watcher is done; a later identity change must not drop the socket again.

    With several accounts the primary can move, and each close costs every in-flight action.
    """
    c = client(3905548034)
    socket = FakeSocket()
    await asyncio.wait_for(c._reconnect_when_identity_known(socket, 3905548034), timeout=2)
    c.runtime.self_id = 111111
    await asyncio.sleep(1.2)
    c.runtime.self_id = 222222
    await asyncio.sleep(1.2)
    assert socket.closed == 0


async def test_the_header_carries_the_account():
    c = client(3905548034)
    headers = c._headers()
    assert headers["X-Self-ID"] == "3905548034"

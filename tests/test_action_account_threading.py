"""The account that must execute a request survives all the way to the session port.

The chain is: network config receives an action -> `Runtime.invoke` -> an action handler -> `actions.base.call`
-> `transport.send` -> `Transport._pipe_session_port(account)` -> `POST 127.0.0.1:<port>/action`.

Every handler builds its own payload -- `send_message` assembles `{chat, elements}`, a mute builds
`{group_id, ...}` -- and none of them carried the account. So the transport received no account and had to pick
a session by guesswork. With one session running the guess was right; with two it was not:

  * a private image reply was addressed to a port that was not the account's, and failed
  * a group reply was executed by whichever session came first, so 2208948865 answered a command sent to
    3905548034

These tests pin the account at both ends: it is stamped by `call`, and it decides the port.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from qsick.onebot.actions import base  # noqa: E402
from qsick.transport.base import SendResult  # noqa: E402

ACCOUNT = "3905548034"
OTHER = "2208948865"


class _Ctx:
    def __init__(self, account: str) -> None:
        self.account = account
        self.calls: list[tuple[str, dict]] = []

    async def call(self, op, params=None, *, timeout=None):  # noqa: ANN001, ANN003
        self.calls.append((op, dict(params or {})))
        return SendResult(ok=True, data={"message_id": 1, "message_id_str": "1"})


class TestEveryPayloadCarriesTheAccount:
    async def test_a_private_send_carries_it(self):
        ctx = _Ctx(ACCOUNT)
        await base.send_message(ctx, {"type": "private", "id": 2208948865},
                                [{"type": "text", "data": {"text": "hi"}}], False)
        assert ctx.calls[-1][1]["account"] == ACCOUNT

    async def test_a_media_send_carries_it(self):
        # This is the one that failed: the picture upload was addressed to a session that was not this account's.
        ctx = _Ctx(ACCOUNT)
        await base.send_message(ctx, {"type": "private", "id": 2208948865},
                                [{"type": "image", "data": {"file": "base64://x"}}], False)
        assert ctx.calls[-1][1]["account"] == ACCOUNT

    async def test_a_group_send_carries_it(self):
        ctx = _Ctx(ACCOUNT)
        await base.send_message(ctx, {"type": "group", "id": 2169010946},
                                [{"type": "text", "data": {"text": "hi"}}], False)
        assert ctx.calls[-1][1]["account"] == ACCOUNT

    async def test_the_chat_target_is_not_overwritten(self):
        # The account says who executes; it must not be confused with the conversation.
        ctx = _Ctx(ACCOUNT)
        await base.send_message(ctx, {"type": "private", "id": 2208948865},
                                [{"type": "text", "data": {"text": "hi"}}], False)
        payload = ctx.calls[-1][1]
        assert payload["chat"] == {"type": "private", "id": 2208948865}
        assert payload["account"] == ACCOUNT

    async def test_a_payload_that_already_names_an_account_is_left_alone(self):
        # `invoke_for` sets it explicitly for an instance; that must win over the context.
        ctx = _Ctx(ACCOUNT)
        await base.call(ctx, "group.mute", {"group_id": 1, "account": OTHER}, "mute")
        assert ctx.calls[-1][1]["account"] == OTHER

    async def test_a_context_without_an_account_adds_nothing(self):
        ctx = _Ctx("")
        await base.send_message(ctx, {"type": "group", "id": 2169010946},
                                [{"type": "text", "data": {"text": "hi"}}], False)
        assert "account" not in ctx.calls[-1][1]

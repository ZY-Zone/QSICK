"""The history actions must forward every ordering flag the OneBot contract declares.

`reverse_order` was accepted by the action signature and then dropped on the way to the transport: the
handler called `history.group` with only `group_id`/`message_seq`/`count`. Measured against a live client,
both orderings returned the identical page -- `[1309, 1310, 1311]` -- and nothing reported a problem,
because a wrong order is still a valid answer.

These pin the forwarding rather than the client-side reversal (which the dispatcher does, and which was
verified live: reverse returned `[1311, 1310, 1309]`).
"""
from __future__ import annotations

import pytest

from qsick.onebot.actions.message import get_friend_msg_history, get_group_msg_history


class Recorder:
    """Minimal ActionContext stand-in that records what the action asked the transport for."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def call(self, op: str, params: dict, *_args, **_kwargs):
        from qsick.transport.base import SendResult

        self.calls.append((op, dict(params)))
        # `base.call` reads `.ok`/`.data` off the result, so this has to be a SendResult, not a dict.
        return SendResult(ok=True, status=0, message="", body=b"", data={"messages": []})

    # the mapper touches these when it runs; returning nothing keeps the row list empty
    store = None
    protocol = "onebotv11"


@pytest.mark.asyncio
async def test_group_history_forwards_reverse_order():
    ctx = Recorder()
    await get_group_msg_history._handle(ctx, group_id=2169010946, count=3, reverse_order=True)
    op, params = ctx.calls[0]
    assert op == "history.group"
    assert params["reverse_order"] is True


@pytest.mark.asyncio
async def test_group_history_accepts_the_older_spelling():
    # NapCat takes both `reverse_order` and `reverseOrder`; a client may send either.
    ctx = Recorder()
    await get_group_msg_history._handle(ctx, group_id=2169010946, count=3, reverseOrder=True)
    assert ctx.calls[0][1]["reverse_order"] is True


@pytest.mark.asyncio
async def test_group_history_defaults_to_forward_order():
    ctx = Recorder()
    await get_group_msg_history._handle(ctx, group_id=2169010946, count=3)
    assert ctx.calls[0][1]["reverse_order"] is False


@pytest.mark.asyncio
async def test_friend_history_forwards_reverse_order():
    ctx = Recorder()
    await get_friend_msg_history._handle(ctx, user_id=2055683427, count=3, reverse_order=True)
    op, params = ctx.calls[0]
    assert op == "history.private"
    assert params["reverse_order"] is True

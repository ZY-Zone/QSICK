"""`PipeTransport.push` must feed the same pipeline headless events use.

The injected DLL now reports received messages by appending to `<Pipe>/owndata/events.jsonl`, and the runtime
polls that file and calls `transport.push` with `{kind, instance, payload}`. `HeadlessTransport` already had
this method, because its host POSTs to `/api/headless/event`; `PipeTransport` did not, so under
`start.bat real` (which runs `--pipe`) the poller found nothing to push to and every message was silently
dropped -- the listener worked, the file grew, and nothing arrived.

What is pinned here:

    * a message becomes the same `message.group` / `message.private` / `message.sent` packet
    * attribution lands on the **account**, since all pipe sessions share one event stream
    * malformed events are rejected loudly rather than queued
"""
from __future__ import annotations

import asyncio
import json

import pytest

from qsick.transport.pipe import PipeTransport


GROUP_MESSAGE = {
    "message_id": 7710274593888217657,
    "chat": {"type": "group", "id": "2169010946"},
    "sender": {"uin": "2208948865"},
    "elements": [{"type": "text", "text": "hello"}],
}


@pytest.fixture()
async def transport():
    """A pipe transport with the fields `push` touches, and its event loop bound.

    `push` enqueues through `_publish`, which needs a loop, and the login branch updates `_login` and sets
    `_login_frame_seen`. Constructing the real object would start a client, so only its state is built here.
    """
    from qsick.transport.base import LoginState

    item = PipeTransport.__new__(PipeTransport)
    item._events = asyncio.Queue(maxsize=64)
    item._loop = asyncio.get_running_loop()
    item._login = LoginState(logged_in=False, uin="0", nickname="")
    item._login_frame_seen = asyncio.Event()
    yield item


async def drain(item: PipeTransport) -> list:
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    out = []
    while not item._events.empty():
        out.append(item._events.get_nowait())
    return out


async def test_a_group_message_becomes_message_group(transport):
    transport.push({"kind": "message", "instance": "3905548034", "payload": GROUP_MESSAGE})
    packets = await drain(transport)

    assert len(packets) == 1
    assert packets[0].cmd == "message.group"
    assert packets[0].kind == "message"
    payload = json.loads(packets[0].body)
    assert payload["message_id"] == GROUP_MESSAGE["message_id"]


async def test_attribution_is_the_account_not_the_client_directory(transport):
    """Every pipe session writes into one shared `events.jsonl`.

    So `Pipe` as the instance would put several accounts' messages in one context, and network profiles --
    which are owned per account -- would never match.
    """
    transport.push({"kind": "message", "instance": "3905548034", "payload": GROUP_MESSAGE})
    packets = await drain(transport)

    assert packets[0].instance == "3905548034"
    assert packets[0].account == "3905548034"


async def test_a_sent_message_is_message_sent(transport):
    transport.push({"kind": "message_sent", "instance": "3905548034", "payload": GROUP_MESSAGE})
    packets = await drain(transport)

    assert packets[0].cmd == "message.sent"


async def test_a_private_message_becomes_message_private(transport):
    payload = {**GROUP_MESSAGE, "chat": {"type": "private", "id": "2208948865"}}
    transport.push({"kind": "message", "instance": "3905548034", "payload": payload})
    packets = await drain(transport)

    assert packets[0].cmd == "message.private"


async def test_an_unsupported_chat_type_is_rejected(transport):
    """Better a visible error than a packet the store cannot index."""
    payload = {**GROUP_MESSAGE, "chat": {"type": "channel", "id": "1"}}
    with pytest.raises(ValueError):
        transport.push({"kind": "message", "instance": "3905548034", "payload": payload})


async def test_a_message_without_a_numeric_id_is_rejected(transport):
    payload = {**GROUP_MESSAGE, "message_id": "abc"}
    with pytest.raises(ValueError):
        transport.push({"kind": "message", "instance": "3905548034", "payload": payload})


async def test_an_unknown_kind_is_rejected(transport):
    with pytest.raises(ValueError):
        transport.push({"kind": "mystery", "instance": "3905548034", "payload": {}})


async def test_a_login_event_updates_the_transport_state(transport):
    transport.push({"kind": "login", "instance": "3905548034",
                    "payload": {"logged_in": True, "uin": "3905548034", "nickname": "号主"}})
    packets = await drain(transport)

    # `login` is a property on this transport, not a method.
    assert transport.login.logged_in is True
    assert transport.login.uin == "3905548034"
    assert packets[0].cmd == "login"
    assert transport._login_frame_seen.is_set()

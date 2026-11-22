"""An rkey belongs to one account, so the cache is keyed by account.

The manager used to keep a single set of scopes. With one account that is invisible; with two, the token fetched
for one is pasted into the other's image URL -- and the server rejects it, or worse, it resolves media the caller
should not reach.

It only appeared to work because an action naming no account used to be routed to "the first live session", so
`rkey` quietly asked an arbitrary account. Now that such a request is refused, the holes have to be real holes:
the account is part of the key and part of the request.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from qsick.protocol.media import MediaResolver  # noqa: E402
from qsick.protocol.rkey import RKeyManager  # noqa: E402
from qsick.transport.base import SendResult  # noqa: E402

ACCOUNT = "3905548034"
OTHER = "2208948865"


class _Transport:
    """Records what was asked and answers with a token derived from the account it was asked about."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def send(self, op, params=None, *, timeout=None):  # noqa: ANN001, ANN003
        self.calls.append({"op": op, "params": dict(params or {})})
        account = str((params or {}).get("account") or "none")
        return SendResult(ok=True, data={"rkeys": [
            {"rkey": f"key-{account}", "type": 20, "ttl": 3600, "create_time": int(time.time())},
            {"rkey": f"key-{account}-p", "type": 10, "ttl": 3600, "create_time": int(time.time())},
        ]})


class TestTheAccountTravels:
    async def test_refresh_sends_the_account(self):
        transport = _Transport()
        manager = RKeyManager(transport)
        await manager.refresh(ACCOUNT)
        assert transport.calls[-1]["params"] == {"account": ACCOUNT}

    async def test_refresh_without_an_account_sends_nothing_extra(self):
        # A caller that genuinely has no account (startup) must not invent one.
        transport = _Transport()
        manager = RKeyManager(transport)
        await manager.refresh()
        assert transport.calls[-1]["params"] == {}


class TestTheCacheIsPerAccount:
    async def test_two_accounts_get_their_own_tokens(self):
        transport = _Transport()
        manager = RKeyManager(transport)
        await manager.refresh(ACCOUNT)
        await manager.refresh(OTHER)
        assert manager.cached("group", ACCOUNT) == f"key-{ACCOUNT}"
        assert manager.cached("group", OTHER) == f"key-{OTHER}"

    async def test_a_miss_on_one_account_does_not_use_the_others(self):
        transport = _Transport()
        manager = RKeyManager(transport)
        await manager.refresh(ACCOUNT)
        assert manager.cached("group", OTHER) is None

    async def test_get_refreshes_only_for_the_account_asked_about(self):
        transport = _Transport()
        manager = RKeyManager(transport)
        token = await manager.get("group", OTHER)
        assert token == f"key-{OTHER}"
        assert transport.calls[-1]["params"] == {"account": OTHER}

    async def test_snapshot_separates_the_accounts(self):
        transport = _Transport()
        manager = RKeyManager(transport)
        await manager.refresh(ACCOUNT)
        await manager.refresh(OTHER)
        snapshot = manager.snapshot()
        assert set(snapshot) == {ACCOUNT, OTHER}
        assert "group" in snapshot[ACCOUNT]


class TestMediaUsesTheMessagesAccount:
    async def test_an_image_url_is_built_from_that_accounts_token(self):
        transport = _Transport()
        manager = RKeyManager(transport)
        resolver = MediaResolver(transport, manager)

        element = type("Element", (), {"type": "image", "data": {"file_id": "abc"}})()
        # No payload-side URL, so the rkey path is what runs.
        async def no_payload(self, element, scope):  # noqa: ANN001
            return None

        resolver._payload_url = no_payload.__get__(resolver)  # type: ignore[assignment]
        url = await resolver.url_for(element, scope="group", account=OTHER)

        assert url is not None and f"key-{OTHER}" in url
        assert f"key-{ACCOUNT}" not in url

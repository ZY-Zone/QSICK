"""rkeys come from the HTTP provider, and its two quirks are handled.

The OIDB request (`command=381`) is refused by the server on this client with `errorCode=253`, so asking it first
would only earn a warning per refresh. The provider answers over plain HTTP:

    { "private_rkey": "&rkey=CAESMG...", "group_rkey": "&rkey=CAISMP...", "expired_time": 1795354123 }

Two things that shape would otherwise break:

  * the values carry a `&rkey=` prefix, and the URL builder appends `&rkey=` itself -- used as-is the URL reads
    `&rkey=&rkey=CAES...`, which the server sees as an empty token
  * `expired_time` is an absolute epoch second, while the cache works in `create_time` + `ttl`
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from qsick.protocol.rkey import RKeyManager, _strip_rkey_prefix  # noqa: E402
from qsick.transport.base import SendResult  # noqa: E402

ACCOUNT = "3905548034"
PROVIDER = "http://example.invalid/rkey"


class _Transport:
    """Fails loudly if the OIDB path is taken: with a provider configured it must not be."""

    def __init__(self) -> None:
        self.calls = 0

    async def send(self, op, params=None, *, timeout=None):  # noqa: ANN001, ANN003
        self.calls += 1
        return SendResult(ok=False, message="OIDB returned errorCode=253")


def manager(payload: dict | None = None, *, url: str = PROVIDER) -> RKeyManager:
    instance = RKeyManager(_Transport(), provider_url=url)
    if payload is not None:
        instance._fetch_provider = lambda: payload  # type: ignore[assignment]
    return instance


class TestPrefix:
    def test_the_query_fragment_is_stripped(self):
        assert _strip_rkey_prefix("&rkey=CAESMG") == "CAESMG"

    def test_a_bare_value_is_left_alone(self):
        assert _strip_rkey_prefix("CAESMG") == "CAESMG"

    def test_a_non_string_is_not_a_token(self):
        assert _strip_rkey_prefix(None) == ""
        assert _strip_rkey_prefix(123) == ""

    def test_the_url_builder_sees_one_rkey_parameter(self):
        # This is the failure the stripping exists to prevent.
        assert "&rkey=&rkey=" not in f"host/download?appid=1&fileid=x&rkey={_strip_rkey_prefix('&rkey=CAES')}"


class TestProviderRefresh:
    async def test_both_tokens_are_taken(self):
        now = int(time.time())
        instance = manager({
            "private_rkey": "&rkey=PRIV", "group_rkey": "&rkey=GRP",
            "expired_time": now + 3600, "name": "rkeys",
        })
        fetched = await instance.refresh(ACCOUNT)
        assert fetched == {"private": "PRIV", "group": "GRP"}
        assert instance.cached("group", ACCOUNT) == "GRP"
        assert instance.cached("private", ACCOUNT) == "PRIV"

    async def test_the_oidb_path_is_not_used(self):
        instance = manager({"private_rkey": "&rkey=P", "group_rkey": "&rkey=G",
                            "expired_time": int(time.time()) + 60})
        await instance.refresh(ACCOUNT)
        assert instance._transport.calls == 0, "配了来源就不该再去问已被拒绝的 OIDB"

    async def test_an_expired_provider_answer_is_refused(self):
        # Handing out a token the server already rejects is worse than having none.
        instance = manager({"private_rkey": "&rkey=P", "group_rkey": "&rkey=G",
                            "expired_time": int(time.time()) - 10})
        assert await instance.refresh(ACCOUNT) == {}
        assert instance.cached("group", ACCOUNT) is None

    async def test_a_missing_expiry_is_treated_as_no_expiry(self):
        instance = manager({"private_rkey": "&rkey=P", "group_rkey": "&rkey=G"})
        assert await instance.refresh(ACCOUNT) == {"private": "P", "group": "G"}
        assert instance.cached("group", ACCOUNT) == "G"

    async def test_a_payload_without_tokens_is_a_failure_not_a_crash(self):
        instance = manager({"name": "rkeys"})
        assert await instance.refresh(ACCOUNT) == {}

    async def test_a_failing_fetch_does_not_raise(self):
        instance = manager(url=PROVIDER)

        def boom() -> dict:
            raise OSError("connection refused")

        instance._fetch_provider = boom  # type: ignore[assignment]
        assert await instance.refresh(ACCOUNT) == {}

    async def test_get_still_falls_back_to_static_when_the_provider_fails(self):
        instance = manager(url=PROVIDER)

        def boom() -> dict:
            raise OSError("down")

        instance._fetch_provider = boom  # type: ignore[assignment]
        instance._static["group"] = "STATIC"
        assert await instance.get("group", ACCOUNT) == "STATIC"


class TestPerAccountStillHolds:
    async def test_each_account_has_its_own_slot(self):
        now = int(time.time())
        instance = manager({"private_rkey": "&rkey=P", "group_rkey": "&rkey=G",
                            "expired_time": now + 3600})
        await instance.refresh(ACCOUNT)
        assert instance.cached("group", "999") is None, "别的账号不该沾到这份 token"


class TestForgetting:
    async def test_forget_makes_the_next_get_fetch_again(self):
        now = int(time.time())
        instance = manager({"private_rkey": "&rkey=P", "group_rkey": "&rkey=G",
                            "expired_time": now + 3600})
        await instance.refresh(ACCOUNT)
        assert instance.cached("group", ACCOUNT) == "G"

        instance.forget(ACCOUNT)
        assert instance.cached("group", ACCOUNT) is None, "被拒绝过的 token 不能继续留在缓存里"

        fetched = await instance.get("group", ACCOUNT)
        assert fetched == "G"

    async def test_forget_only_touches_the_account_named(self):
        now = int(time.time())
        instance = manager({"private_rkey": "&rkey=P", "group_rkey": "&rkey=G",
                            "expired_time": now + 3600})
        await instance.refresh(ACCOUNT)
        await instance.refresh("999")
        instance.forget(ACCOUNT)
        assert instance.cached("group", "999") == "G"

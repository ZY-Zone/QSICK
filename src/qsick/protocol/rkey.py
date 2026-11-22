"""Download rkeys: fetch, cache and expire the tokens behind media URLs.

The token is *not* a secret sauce — it is a normal authenticated request. What
costs engineering is the lifecycle: per-scope tokens, short TTLs, and refresh on
demand without stampeding the payload.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from ..log import get_logger
from ..transport.base import Transport, TransportError

log = get_logger("Protocol.RKey")

TYPE_FALLBACK = 2
TYPE_PRIVATE = 10
TYPE_GROUP = 20

SCOPE_BY_TYPE = {TYPE_PRIVATE: "private", TYPE_GROUP: "group", TYPE_FALLBACK: "fallback"}


@dataclass(slots=True)
class RKeyEntry:
    rkey: str
    type: int
    ttl: int
    create_time: int

    def expired(self, *, now: int | None = None, skew: int = 0) -> bool:
        now = int(time.time()) if now is None else now
        if self.ttl <= 0:
            return False  # server said "no expiry"
        return now >= self.create_time + max(0, self.ttl - skew)


def _strip_rkey_prefix(value: object) -> str:
    """`&rkey=CAES...` -> `CAES...`.

    The provider hands back a ready-made query fragment, while our URL builder appends `&rkey=` itself. Passing the
    value through unchanged produced `&rkey=&rkey=CAES...`, which the server reads as an empty token.
    """
    if not isinstance(value, str):
        return ""
    token = value.strip()
    for prefix in ("&rkey=", "rkey=", "&", "?"):
        if token.startswith(prefix):
            token = token[len(prefix):]
    return token.strip()


class RKeyManager:
    """Caches one token per (account, scope) and refreshes lazily.

    The account is part of the key, not a detail of the request: a token belongs to whoever asked for it, so a
    cache shared between accounts hands out tokens that the server rejects -- or worse, that work against a
    different account's media.
    """

    def __init__(
        self,
        transport: Transport,
        *,
        skew: int = 300,
        static: dict[str, str] | None = None,
        provider_url: str = "",
    ) -> None:
        self._transport = transport
        #: When set, tokens come from here instead of the OIDB request. The request is what the server refuses
        #: with `errorCode=253` on this client, so asking it first would only earn a warning per refresh.
        self._provider_url = str(provider_url or "").strip()
        self._skew = skew
        #: account -> scope -> entry. Keyed by account because a token is issued to one.
        self._cache: dict[str, dict[str, RKeyEntry]] = {}
        self._static = dict(static or {})
        for scope, env in (("private", "QSICK_RKEY_PRIVATE"), ("group", "QSICK_RKEY_GROUP")):
            value = os.environ.get(env)
            if value:
                self._static[scope] = value.strip()
        self._last_error = ""

    def cached(self, scope: str, account: str = "") -> str | None:
        entry = self._cache.get(str(account or ""), {}).get(scope)
        if entry and not entry.expired(skew=self._skew):
            return entry.rkey
        return None

    async def get(self, scope: str, account: str = "") -> str | None:
        scope = scope if scope in ("private", "group") else "group"
        cached = self.cached(scope, account)
        if cached:
            return cached
        await self.refresh(account)
        cached = self.cached(scope, account)
        if cached:
            return cached
        # Static fallback is intentionally last: it is stale by definition.
        return self._static.get(scope)

    async def refresh(self, account: str = "") -> dict[str, str]:
        # The account travels with the request. Without it the transport cannot tell which session must answer,
        # and with more than one running it refuses rather than guessing -- correctly, because a guessed token is
        # another account's.
        if self._provider_url:
            return await self._refresh_from_provider(account)

        params: dict[str, str] = {"account": str(account)} if account else {}
        try:
            result = await self._transport.send("rkey", params)
        except TransportError as exc:
            self._log_failure(f"transport: {exc}")
            return {}
        if not result.ok:
            self._log_failure(result.message or "payload refused rkey request")
            return {}

        entries = result.data.get("rkeys") if isinstance(result.data, dict) else None
        if entries is None:
            entries = result.data if isinstance(result.data, list) else []
        fetched: dict[str, str] = {}
        for raw in entries or []:
            try:
                entry = RKeyEntry(
                    rkey=str(raw["rkey"]),
                    type=int(raw.get("type", 0)),
                    ttl=int(raw.get("ttl", raw.get("ttl_seconds", 0))),
                    create_time=int(raw.get("create_time", time.time())),
                )
            except (KeyError, TypeError, ValueError):
                continue
            scope = SCOPE_BY_TYPE.get(entry.type)
            if not scope or not entry.rkey:
                continue
            self._cache.setdefault(str(account or ""), {})[scope] = entry
            fetched[scope] = entry.rkey
        if fetched:
            log.debug("rkey refreshed", scopes=",".join(sorted(fetched)), account=str(account or ""))
        return fetched

    async def _refresh_from_provider(self, account: str = "") -> dict[str, str]:
        """Fetch both tokens over HTTP and store them under the asking account."""
        try:
            payload = await asyncio.to_thread(self._fetch_provider)
        except Exception as exc:  # noqa: BLE001 - a provider that is down is worth one warning, not a crash
            self._log_failure(f"provider: {type(exc).__name__}: {exc}")
            return {}

        now = int(time.time())
        # `expired_time` is absolute. A provider that reports a past instant is treated as useless rather than as
        # "no expiry", because the alternative is handing out a token that is already rejected.
        try:
            expires_at = int(payload.get("expired_time") or 0)
        except (TypeError, ValueError):
            expires_at = 0
        ttl = max(0, expires_at - now) if expires_at else 0
        if expires_at and ttl <= 0:
            self._log_failure("provider returned an expired rkey")
            return {}

        fetched: dict[str, str] = {}
        scope_by_key = {"private_rkey": "private", "group_rkey": "group"}
        for key, scope in scope_by_key.items():
            token = _strip_rkey_prefix(payload.get(key))
            if not token:
                continue
            entry = RKeyEntry(rkey=token, type=TYPE_PRIVATE if scope == "private" else TYPE_GROUP,
                              ttl=ttl, create_time=now)
            self._cache.setdefault(str(account or ""), {})[scope] = entry
            fetched[scope] = token
        if fetched:
            log.debug("rkey refreshed from the provider", scopes=",".join(sorted(fetched)),
                      account=str(account or ""))
        else:
            self._log_failure("provider returned no usable rkey")
        return fetched

    def _fetch_provider(self) -> dict:
        """One blocking GET. Kept separate so tests can replace it without a socket."""
        request = urllib.request.Request(self._provider_url, headers={"User-Agent": "QSICK/1"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=10) as response:  # noqa: S310 - configured endpoint
            body = response.read(64 * 1024).decode("utf-8", "replace")
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise ValueError("provider payload is not an object")
        return payload

    def _log_failure(self, reason: str) -> None:
        if reason != self._last_error:
            self._last_error = reason
            log.warn("rkey refresh failed; falling back to static/empty", reason=reason)

    def snapshot(self) -> dict[str, dict[str, dict[str, object]]]:
        """What is cached, per account. Kept nested so two accounts cannot be confused in a status view."""
        return {
            account: {
                scope: {"ttl": entry.ttl, "create_time": entry.create_time, "expired": entry.expired()}
                for scope, entry in scopes.items()
            }
            for account, scopes in self._cache.items()
        }

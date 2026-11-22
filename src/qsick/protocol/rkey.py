"""Download rkeys: fetch, cache and expire the tokens behind media URLs.

The token is *not* a secret sauce — it is a normal authenticated request. What
costs engineering is the lifecycle: per-scope tokens, short TTLs, and refresh on
demand without stampeding the payload.
"""

from __future__ import annotations

import os
import time
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
    ) -> None:
        self._transport = transport
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

"""Stateful Milky identity bridging; native operations always use Runtime.invoke.

Sequence numbers are conversation-local, never internal message IDs. Unknown or
ambiguous identities fail closed rather than falling back to the primary account.
"""
from __future__ import annotations

import asyncio
from typing import Any


async def run_milky(runtime: Any, pipelines: Any, api: str, body: dict[str, Any], echo: Any = None) -> dict[str, Any]:
    """Shared HTTP/WS/reverse entry; callers enforce authentication and bound account."""
    def failure(code: int, message: str) -> dict[str, Any]:
        reply = {"status": "failed", "retcode": code, "message": f"{api}: {message}", "data": None}
        if echo is not None:
            reply["echo"] = echo
        return reply
    if pipelines is None:
        return failure(-500, "no pipeline")
    try:
        adapter = pipelines.protocol("milky")
        local = getattr(adapter, "local_reply", None)
        answered = local(api, body) if callable(local) else None
        if answered is not None:
            return {**answered, **({"echo": echo} if echo is not None else {})}
    except Exception as exc:
        return failure(-500, str(exc) or type(exc).__name__)
    bridge = MilkyBridge(runtime)
    try:
        body = {**body, "account": bridge.account(body)}
        prepared = await bridge.prepare(api, body)
        name, params = prepared if prepared is not None else pipelines.decode_action("milky", api, body)
        params = {**params, "account": body["account"]}
    except BridgeError as exc:
        return failure(exc.code, str(exc))
    except Exception as exc:
        return failure(-400, str(exc) or type(exc).__name__)
    try:
        result = await runtime.invoke(name, params)
        try:
            result = await bridge.enrich(api, params, result)
        except Exception:
            # The formatter rejects missing receipt fields, without repeating the send.
            pass
        formatter = getattr(adapter, "format_api_reply", None)
        if callable(formatter):
            return formatter(api, result, echo=echo) if echo is not None else formatter(api, result)
        return pipelines.format_reply("milky", result, echo=echo)
    except Exception as exc:
        return failure(-500, str(exc) or type(exc).__name__)


class BridgeError(ValueError):
    def __init__(self, message: str, code: int = -400) -> None:
        super().__init__(message)
        self.code = code


def _positive(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise BridgeError(f"{field} must be a positive integer")
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise BridgeError(f"{field} must be a positive integer") from None
    if number <= 0 or str(number) != str(value):
        raise BridgeError(f"{field} must be a positive integer")
    return number


class MilkyBridge:
    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime

    def account(self, body: dict[str, Any]) -> str:
        active = getattr(self.runtime, "_active_accounts", None)
        accounts = set(map(str, active())) if callable(active) else set()
        if not accounts:
            accounts = {str(s.uin) for s in getattr(self.runtime, "_logins", {}).values()
                        if getattr(s, "logged_in", False) and str(s.uin) not in ("", "0")}
        requested = str(body.get("account") or "").strip()
        if requested:
            if requested not in accounts:
                raise BridgeError("selected account is not active", -409)
            return requested
        if len(accounts) == 1:
            return next(iter(accounts))
        profiles_for = getattr(self.runtime, "_network_profiles_for", None)
        if callable(profiles_for):
            owners = set()
            for profile in profiles_for(sorted(accounts)):
                if not profile.get("enabled") or profile.get("protocol") != "milky" or profile.get("kind") not in ("http", "ws_server"):
                    continue
                owner = str(profile.get("instance") or profile.get("account") or "")
                state = getattr(self.runtime, "_logins", {}).get(owner)
                owner = str(state.uin) if state is not None else owner
                if owner in accounts:
                    owners.add(owner)
            if len(owners) == 1:
                return owners.pop()
        raise BridgeError("Milky account is ambiguous or offline; select an active account", -409)

    def _stored(self, account: str) -> list[Any]:
        found = {}
        stores = [getattr(self.runtime, name, None) for name in ("store", "cache_store")]
        hub = getattr(self.runtime, "instances", None)
        if hub is not None:
            for name in hub.names():
                instance = hub.get(name)
                if instance is not None and str(instance.account) == account:
                    stores.append(instance.store)
        visited = set()
        for store in stores:
            if store is not None and id(store) not in visited:
                visited.add(id(store))
                for m in store.recent(2000):
                    if str(getattr(m, "account", "")) == account:
                        found[(m.message_id, m.chat_type, m.chat_id, m.seq)] = m
        return list(found.values())

    @staticmethod
    def _scope(api: str, body: dict[str, Any]) -> tuple[str, int]:
        if api in ("recall_group_message", "set_group_essence_message"):
            return "group", _positive(body.get("group_id"), "group_id")
        if api == "recall_private_message":
            return "private", _positive(body.get("user_id"), "user_id")
        scene = body.get("message_scene")
        if scene not in ("friend", "group"):
            raise BridgeError("message_scene must be friend or group; temp is unsupported")
        return ("private" if scene == "friend" else "group"), _positive(body.get("peer_id"), "peer_id")

    async def _resolve(self, account: str, kind: str, peer: int, seq: int) -> tuple[int, Any]:
        matches = [m for m in await asyncio.to_thread(self._stored, account)
                   if m.chat_type == kind and m.chat_id == peer and m.seq == seq]
        if len(matches) > 1:
            raise BridgeError("ambiguous message sequence", -409)
        if matches:
            match = matches[0]
            primary = getattr(self.runtime, "store", None)
            cached = primary.get(match.message_id) if primary is not None else None
            if cached is not None and (str(cached.account) != account or cached.chat_type != kind or cached.chat_id != peer or cached.seq != seq):
                raise BridgeError("message ID cache belongs to a different conversation/account", -409)
            return match.message_id, match
        key = "group_id" if kind == "group" else "user_id"
        result = await self.runtime.invoke(
            "get_group_msg_history" if kind == "group" else "get_friend_msg_history",
            {"account": account, key: peer, "message_seq": seq, "count": 20})
        if result.get("status") == "failed":
            raise BridgeError("message history lookup failed: " + str(result.get("message") or result.get("wording") or "unavailable"), -404)
        rows = (result.get("data") or {}).get("messages", [])
        matches = [r for r in rows if isinstance(r, dict)
                   and str(r.get("message_seq")) == str(seq)
                   and str(r.get(key)) == str(peer)
                   and r.get("message_type") == kind
                   and str(r.get("account", account)) == account]
        if len(matches) != 1:
            raise BridgeError("message sequence not found unambiguously", -404)
        mid = _positive(matches[0].get("message_id"), "resolved message_id")
        store = getattr(self.runtime, "store", None)
        cached = store.get(mid) if store is not None else None
        if cached is not None and (str(cached.account) != account or cached.chat_type != kind or cached.chat_id != peer or cached.seq != seq):
            raise BridgeError("message ID cache belongs to a different conversation/account", -409)
        return mid, cached

    async def prepare(self, api: str, body: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
        account = body["account"]
        if api == "get_forwarded_messages":
            fid = str(body.get("forward_id") or "")
            if not fid:
                raise BridgeError("forward_id is required")
            contexts = set()
            for m in await asyncio.to_thread(self._stored, account):
                for element in m.elements:
                    data = element.data
                    if element.type == "forward" and fid in {str(data.get(k) or "") for k in ("id", "res_id", "forward_id")}:
                        contexts.add((m.chat_type, m.chat_id))
            if len(contexts) != 1:
                raise BridgeError("forward_id has no unambiguous cached conversation context", -404)
            kind, peer = contexts.pop()
            return "get_forward_msg", {"account": account, "id": fid, "chat_type": kind, "chat_id": peer}
        if api not in {"recall_private_message", "recall_group_message", "get_message", "set_group_essence_message", "mark_message_as_read"}:
            return None
        kind, peer = self._scope(api, body)
        seq = _positive(body.get("message_seq"), "message_seq")
        mid, stored = await self._resolve(account, kind, peer, seq)
        params = {"account": account, "message_id": mid}
        if api == "set_group_essence_message":
            native = getattr(stored, "native", {}) or {}
            raw = getattr(stored, "raw", {}) or {}
            random = next((d[k] for d in (native, raw) for k in ("msgRandom", "msg_random", "random") if k in d), None)
            if random is None:
                raise BridgeError("essence requires the real msg_random; not present in message cache", -404)
            params.update(group_id=peer, msg_seq=seq, msg_random=random)
            enabled = body.get("is_set", True)
            if not isinstance(enabled, bool):
                raise BridgeError("is_set must be a boolean")
            return ("set_essence_msg" if enabled else "delete_essence_msg"), params
        return ({"get_message": "get_msg", "mark_message_as_read": "mark_msg_as_read"}.get(api, "delete_msg"), params)

    async def enrich(self, api: str, params: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        if api not in ("send_private_message", "send_group_message") or result.get("status") == "failed":
            return result
        data = result.get("data")
        if not isinstance(data, dict) or (data.get("message_seq") and data.get("time")):
            return result
        mid = data.get("message_id")
        if not mid:
            return result
        kind = "group" if api == "send_group_message" else "private"
        peer = params.get("group_id" if kind == "group" else "user_id")
        store = getattr(self.runtime, "store", None)
        cached = store.get(int(mid)) if store is not None else None
        if cached is not None and (str(cached.account) != params["account"] or cached.chat_type != kind or str(cached.chat_id) != str(peer)):
            return result
        reply = await self.runtime.invoke("get_msg", {"account": params["account"], "message_id": mid})
        record = reply.get("data") or {}
        key = "group_id" if kind == "group" else "user_id"
        if reply.get("status") != "failed" and record.get("message_type") == kind and str(record.get(key)) == str(peer) and str(record.get("message_id")) == str(mid):
            if record.get("message_seq") and record.get("time"):
                return {**result, "data": {**data, "message_seq": record["message_seq"], "time": record["time"]}}
        return result

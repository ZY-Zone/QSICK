"""A loopback HTTP endpoint that feeds the message cache.

This is the cache's own ingress, separate from the OneBot listener and from the WebUI, so a producer can hand it a
message without impersonating a bot or holding a WebSocket open. It is deliberately **not** wired into the WebUI
chat feature: it only writes to the store.

The body is the normalized message shape (`protocol.message.Message.to_dict()`), not a OneBot event. QSICK already
normalises on the way in, so accepting the normalized form means one conversion on the producing side instead of
two, and nothing here has to re-derive a chat from `message_type`/`group_id`/`target_id`.

Writes are idempotent: `SqliteStore.put` upserts on `message_id`, so QSICK forwarding a copy of a message it has
already stored does not create a second row. That is what makes "forward a copy to the cache port" safe.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import log as logmod
from .protocol.message import Message

if TYPE_CHECKING:  # pragma: no cover
    from .runtime import Runtime

log = logmod.get_logger("MessageCache")


def create_app(runtime: "Runtime") -> FastAPI:
    """Serve the message cache over HTTP.

    A bare `FastAPI` with three routes: it is a cache ingress, not an application.
    """
    app = FastAPI(title="QSICK message cache", docs_url=None, redoc_url=None, openapi_url=None)

    def store_for(account: str):
        """The store that owns `account`, falling back to the runtime's own.

        An unknown account is not an error: an external producer may only know the uin, and putting the message in
        the default cache is more useful than rejecting it. Existing contexts are consulted only -- nothing is
        created, because a message for an account that is not running should not leave an empty database behind.
        """
        if account:
            try:
                context = runtime.instances.get(account)
                if context is None:
                    # Instances are named by account for pipe sessions, but a headless instance is `C0`/`C1`, so
                    # match on the reported account as well.
                    for name in runtime.instances.names():
                        candidate = runtime.instances.get(name)
                        if candidate is not None and str(candidate.account or "") == account:
                            context = candidate
                            break
                if context is not None:
                    return context.store
            except Exception as exc:  # noqa: BLE001 - a lookup failure must not lose the message
                log.debug("could not resolve the account's store", account=account, error=str(exc))
        return runtime.store

    @app.get("/health")
    async def health() -> JSONResponse:
        return JSONResponse({"status": "ok", "cache": "messages"})

    @app.post("/message")
    async def put_message(request: Request) -> JSONResponse:
        try:
            body: Any = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": "body is not JSON"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"ok": False, "error": "body must be an object"}, status_code=400)
        try:
            message = Message.from_payload(body)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": f"cannot read the message: {exc}"}, status_code=400)
        if not message.message_id:
            return JSONResponse({"ok": False, "error": "message_id is required"}, status_code=400)
        store = store_for(str(body.get("account") or ""))
        try:
            store.put(message)
        except Exception as exc:  # noqa: BLE001
            log.warn("message cache write failed", message_id=message.message_id, error=str(exc))
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
        return JSONResponse({"ok": True, "message_id": message.message_id})

    @app.get("/messages")
    async def recent(limit: int = 50, account: str = "") -> JSONResponse:
        """The newest entries, so the endpoint can be checked without opening the WebUI.

        Merged across the instance stores, because ingest routes each message to the store that owns its account.
        Reading only the default store would answer empty right after a successful write to another account --
        which looks exactly like a broken cache.
        """
        wanted = max(1, min(limit, 500))
        if account:
            stores = [store_for(account)]
        else:
            stores = [runtime.store]
            try:
                for name in runtime.instances.names():
                    context = runtime.instances.get(name)
                    if context is not None:
                        stores.append(context.store)
            except Exception as exc:  # noqa: BLE001 - listing is a convenience, not a contract
                log.debug("could not list instance stores", error=str(exc))

        seen: dict[int, Any] = {}
        for store in stores:
            for item in store.recent(limit=wanted):
                seen.setdefault(item.message_id, item)
        ordered = sorted(seen.values(), key=lambda item: item.time, reverse=True)[:wanted]
        return JSONResponse({"ok": True, "messages": [item.to_dict() for item in ordered]})

    return app

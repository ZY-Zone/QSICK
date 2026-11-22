"""OneBot v11 transports: HTTP action endpoint, WebSocket, SSE, event fan-out."""

from __future__ import annotations

import asyncio
import json
import secrets
from typing import Any, TYPE_CHECKING

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.websockets import WebSocketState

from .. import log as logmod
from . import models

if TYPE_CHECKING:  # pragma: no cover
    from ..runtime import Runtime

log = logmod.get_logger("OneBot.Server")


class EventHub:
    """Fan-out for OneBot clients (WebSocket + SSE)."""

    def __init__(self) -> None:
        self._websockets: set[WebSocket] = set()
        self._sse: set[asyncio.Queue[str]] = set()
        self._lock = asyncio.Lock()
        self.dropped_sse = 0
        self.slow_ws = 0

    async def add_ws(self, ws: WebSocket) -> None:
        async with self._lock:
            self._websockets.add(ws)

    async def remove_ws(self, ws: WebSocket) -> None:
        async with self._lock:
            self._websockets.discard(ws)

    def add_sse(self) -> asyncio.Queue[str]:
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=256)
        self._sse.add(queue)
        return queue

    def remove_sse(self, queue: asyncio.Queue[str]) -> None:
        self._sse.discard(queue)

    def stats(self) -> dict[str, int]:
        return {
            "websockets": len(self._websockets),
            "sse": len(self._sse),
            "dropped_sse": self.dropped_sse,
            "slow_ws": self.slow_ws,
        }

    async def broadcast(self, payload: dict[str, Any]) -> None:
        text = json.dumps(payload, ensure_ascii=False)
        dead: list[WebSocket] = []
        for ws in list(self._websockets):
            try:
                if ws.client_state is WebSocketState.CONNECTED:
                    await asyncio.wait_for(ws.send_text(text), timeout=5.0)
                else:
                    dead.append(ws)
            except Exception:  # noqa: BLE001 - one broken client must not stop others
                dead.append(ws)
        self.slow_ws += len(dead)
        for ws in dead:
            await self.remove_ws(ws)
        for queue in list(self._sse):
            try:
                queue.put_nowait(text)
            except asyncio.QueueFull:
                self.dropped_sse += 1
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    queue.put_nowait(text)
                except asyncio.QueueFull:
                    pass


def _token_of(request: Request | WebSocket) -> str:
    if isinstance(request, WebSocket):
        token = request.query_params.get("access_token", "")
        return token or request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    token = request.query_params.get("access_token", "")
    if token:
        return token
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return request.headers.get("x-access-token", "")


def create_app(runtime: "Runtime") -> FastAPI:
    app = FastAPI(title="QSICK OneBot", docs_url=None, redoc_url=None)
    config = runtime.config.onebot
    hub = runtime.hub
    app.state.runtime = runtime

    def authorized(request: Request | WebSocket) -> bool:
        expected = config.access_token
        if not expected:
            return True
        return secrets.compare_digest(_token_of(request), expected)

    async def handle_action(name: str, params: dict[str, Any]) -> dict[str, Any]:
        return await runtime.invoke(name, params)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "self_id": runtime.self_id}

    async def _run_body(body: Any) -> JSONResponse:
        if not isinstance(body, dict):
            return JSONResponse(models.fail("body must be an object"), status_code=400)
        name = str(body.get("action", ""))
        params = body.get("params") or {}
        if not isinstance(params, dict):
            return JSONResponse(models.fail("params must be an object"), status_code=400)
        result = await handle_action(name, {**params, "echo": body.get("echo")})
        return JSONResponse(result)

    @app.post(config.http_path)
    async def action_post(request: Request) -> JSONResponse:
        if not authorized(request):
            return JSONResponse(models.fail("unauthorized", models.UNAUTHORIZED), status_code=401)
        content_type = request.headers.get("content-type", "").lower()
        if content_type.startswith("application/x-www-form-urlencoded"):
            # OneBot v11 also permits form-encoded action bodies.
            form = await request.form()
            params: dict[str, Any] = {}
            for key, value in form.items():
                if key in ("action", "echo"):
                    continue
                params[key] = value
            return await _run_body(
                {"action": form.get("action", ""), "params": params, "echo": form.get("echo")}
            )
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JSONResponse(models.fail("body is not valid JSON"), status_code=400)
        return await _run_body(body)

    @app.post("/action")
    async def action_post_standard(request: Request) -> JSONResponse:
        return await action_post(request)

    @app.get("/action")
    async def action_get_standard(request: Request) -> JSONResponse:
        return await action_get(request)

    @app.get(config.http_path)
    async def action_get(request: Request) -> JSONResponse:
        if not authorized(request):
            return JSONResponse(models.fail("unauthorized", models.UNAUTHORIZED), status_code=401)
        name = request.query_params.get("action", "")
        # access_token authenticates the request; it is never an action parameter.
        params: dict[str, Any] = {
            key: value
            for key, value in request.query_params.items()
            if key not in ("action", "access_token")
        }
        result = await handle_action(name, {**params, "echo": request.query_params.get("echo")})
        return JSONResponse(result)

    @app.websocket(config.ws_path)
    async def websocket_endpoint(ws: WebSocket) -> None:
        if not authorized(ws):
            await ws.close(code=1008, reason="unauthorized")
            return
        await ws.accept()
        await hub.add_ws(ws)
        log.info("onebot websocket connected", clients=len(hub._websockets))
        try:
            await ws.send_text(
                json.dumps(
                    {
                        "post_type": "meta_event",
                        "meta_event_type": "lifecycle",
                        "sub_type": "connect",
                        "time": runtime.now(),
                        "self_id": runtime.self_id,
                    }
                )
            )
            while True:
                raw = await ws.receive_text()
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    await ws.send_text(json.dumps(models.fail("invalid JSON", echo=None)))
                    continue
                if not isinstance(msg, dict):
                    await ws.send_text(json.dumps(models.fail("expected object")))
                    continue
                action = str(msg.get("action", ""))
                params = msg.get("params") or {}
                if not isinstance(params, dict):
                    params = {}
                result = await runtime.invoke(action, {**params, "echo": msg.get("echo")})
                await ws.send_text(json.dumps(result, ensure_ascii=False))
        except WebSocketDisconnect:
            pass
        except Exception as exc:  # noqa: BLE001
            log.warn("websocket error", error=f"{type(exc).__name__}: {exc}")
        finally:
            await hub.remove_ws(ws)
            log.info("onebot websocket disconnected", clients=len(hub._websockets))

    if config.enable_sse:

        @app.get(config.sse_path)
        async def sse(request: Request) -> StreamingResponse:
            if not authorized(request):
                return JSONResponse(models.fail("unauthorized", models.UNAUTHORIZED), status_code=401)

            queue = hub.add_sse()

            async def stream() -> Any:
                try:
                    yield "retry: 3000\n\n"
                    while True:
                        try:
                            item = await asyncio.wait_for(queue.get(), timeout=15)
                            yield f"data: {item}\n\n"
                        except asyncio.TimeoutError:
                            yield ": keep-alive\n\n"
                except asyncio.CancelledError:
                    raise
                finally:
                    hub.remove_sse(queue)

            return StreamingResponse(stream(), media_type="text/event-stream")

    return app

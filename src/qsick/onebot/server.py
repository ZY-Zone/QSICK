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

#: 每条协议在自己的路径前缀下，互不遮蔽。一条连接落在哪个前缀里，它的协议就已经确定了 ——
#: 这是比分流点里的 profile 反查更可靠的一层：前缀是请求自己带的信息。
#:
#: 大小写按用户的写法保留：`/ONEBOTV11/` 与 `/MILKY/`。云端客户端通常原样使用配置里的 URL，
#: 不会去猜大小写，所以这里不额外做小写别名 —— 多一个别名就多一条会被误用的路径。
ONEBOTV11_PREFIX = "/ONEBOTV11"
MILKY_PREFIX = "/MILKY"


def prefixed(prefix: str, path: str) -> str:
    """`("/ONEBOTV11", "/ws")` -> `"/ONEBOTV11/ws"`；`("/ONEBOTV11", "/")` -> `"/ONEBOTV11/"`。"""
    inner = str(path or "/")
    if not inner.startswith("/"):
        inner = "/" + inner
    return prefix + inner


log = logmod.get_logger("OneBot.Server")
# Connection-class categories, separate from the general server logger.
#
# "the websocket dropped" and "the http endpoint answered 403" were both just `OneBot.Server`, so watching one kind
# of connection meant reading past the other. These two exist so the log filter can be narrowed to a transport.
ws_log = logmod.get_logger("OneBot.WS")
http_log = logmod.get_logger("OneBot.HTTP")


class EventHub:
    """Fan-out for the server-side transports (WebSocket + SSE).

    Each connected client is registered together with a :class:`Connection`, so the hub knows **which protocol
    that client speaks**. It used to store bare `WebSocket` objects, which made per-protocol encoding impossible
    to implement even in principle -- there was nothing to look the protocol up by.

    The hub itself knows no protocol: it asks `pipelines` to encode once per protocol and then writes the same
    serialized frame to every client of that protocol. One encode per protocol, not per client, so N clients of
    the same protocol cannot end up with different JSON.
    """

    def __init__(self, pipelines: Any = None) -> None:
        self._websockets: dict[WebSocket, Any] = {}
        self._sse: dict[asyncio.Queue[str], Any] = {}
        self._lock = asyncio.Lock()
        self.dropped_sse = 0
        self.slow_ws = 0
        #: Set by the runtime once it exists. Optional so the hub can be built standalone in tests.
        self.pipelines = pipelines
        #: `(kind, path) -> protocol id`. Injected rather than read from config here: the mapping lives in the
        #: profile store, and the hub has no business importing it.
        self.resolve_protocol = None

    def protocol_for(self, kind: str, path: str = "") -> str:
        """Which protocol a server-side connection speaks, per the enabled profiles."""
        resolver = self.resolve_protocol
        if callable(resolver):
            try:
                return str(resolver(kind, path) or "")
            except Exception:  # noqa: BLE001 - a broken resolver must not refuse the connection
                pass
        if self.pipelines is not None:
            return self.pipelines.default()
        return ""

    def connection(self, kind: str, path: str = "", account: str = "",
                   protocol: str | None = None) -> Any:
        """Build a `Connection` for an incoming server-side client.

        `protocol` overrides the profile lookup. Some paths fix their protocol intrinsically --
        Milky's `/event` is Milky whatever the profiles say -- and a lookup there could only ever get it wrong.
        """
        from .pipeline import Connection

        chosen = self.protocol_for(kind, path) if protocol is None else str(protocol)
        key = f"{kind}:{path}:{account}" if account else f"{kind}:{path}"
        return Connection(key=key, kind=kind, protocol=chosen, account=account,
                          profile_id=kind, instance=account)

    async def add_ws(self, ws: WebSocket, connection: Any = None) -> None:
        async with self._lock:
            self._websockets[ws] = connection or self.connection("ws_server")

    async def remove_ws(self, ws: WebSocket) -> None:
        async with self._lock:
            self._websockets.pop(ws, None)

    def add_sse(self, connection: Any = None) -> asyncio.Queue[str]:
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=256)
        self._sse[queue] = connection or self.connection("sse")
        return queue

    def remove_sse(self, queue: asyncio.Queue[str]) -> None:
        self._sse.pop(queue, None)

    def protocols(self) -> list[str]:
        """Protocols with at least one live server-side client."""
        names = {getattr(item, "protocol", "") for item in self._websockets.values()}
        names |= {getattr(item, "protocol", "") for item in self._sse.values()}
        return sorted(name for name in names if name)

    #: HTTP is stateless, so there is no connection to count. These two are what can honestly be shown instead:
    #: how many requests are being served right now, and how many have been served in total. A panel that claimed
    #: "0 connections" for a working HTTP endpoint would be telling the operator nothing is using it.
    http_active = 0
    http_requests = 0

    def note_http(self, delta: int) -> None:
        """Call with +1 when a request starts and -1 when it ends."""
        self.http_active = max(0, self.http_active + delta)
        if delta > 0:
            self.http_requests += 1

    def stats(self) -> dict[str, Any]:
        return {
            "websockets": len(self._websockets),
            "sse": len(self._sse),
            "dropped_sse": self.dropped_sse,
            "slow_ws": self.slow_ws,
            "http_active": self.http_active,
            "http_requests": self.http_requests,
            "protocols": self.protocols(),
        }

    async def broadcast(self, payload: dict[str, Any]) -> None:
        """Write one event to every client, encoded for that client's own protocol."""
        if not self._websockets and not self._sse:
            return
        frames = self._encode_by_protocol(payload)
        if not frames:
            return

        dead: list[WebSocket] = []
        for ws, connection in list(self._websockets.items()):
            texts = frames.get(getattr(connection, "protocol", ""))
            if not texts:
                continue          # this protocol failed to encode; skip its clients only
            try:
                if ws.client_state is WebSocketState.CONNECTED:
                    for text in texts:
                        await asyncio.wait_for(ws.send_text(text), timeout=5.0)
                else:
                    dead.append(ws)
            except Exception:  # noqa: BLE001 - one broken client must not stop others
                dead.append(ws)
        self.slow_ws += len(dead)
        for ws in dead:
            await self.remove_ws(ws)

        for queue, connection in list(self._sse.items()):
            texts = frames.get(getattr(connection, "protocol", ""))
            if not texts:
                continue
            for text in texts:
                try:
                    queue.put_nowait(text)
                except asyncio.QueueFull:
                    self.dropped_sse += 1
                    try:
                        queue.get_nowait()
                        queue.put_nowait(text)
                    except (asyncio.QueueEmpty, asyncio.QueueFull):
                        pass

    def _encode_by_protocol(self, payload: dict[str, Any]) -> dict[str, list[str]]:
        """`protocol -> serialized frames`, encoded once per protocol.

        A protocol whose encoder raises is simply absent from the result: its clients get nothing for this event,
        every other protocol still gets it. A list rather than a single string because one internal event can
        legitimately become several wire events (Milky: a private file message is `message_receive` plus
        `friend_file_upload`).
        """
        if self.pipelines is None:
            return {"": [json.dumps(payload, ensure_ascii=False)]}

        wanted: set[str] = set()
        for connection in list(self._websockets.values()) + list(self._sse.values()):
            wanted.add(getattr(connection, "protocol", "") or "")

        frames: dict[str, list[str]] = {}
        for protocol in wanted:
            encoded = self.pipelines.encode_events(protocol, payload)
            texts: list[str] = []
            for item in encoded:
                try:
                    texts.append(json.dumps(item, ensure_ascii=False))
                except (TypeError, ValueError):
                    continue
            if texts:
                frames[protocol] = texts
        return frames


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

    # ---------------------------------------------------------------- 分流点（出）
    #
    # 动作从哪条连接进来，就用那条连接的协议翻译名字与参数，然后交给 invoke。
    #
    # 为什么一定要经过 invoke：插件过滤、账号级出入限制、只读缓存、发送串行化全在它的入口。
    # 绕开它直接把调用转给宿主，core 插件的封禁就形同虚设 —— 这是这一层唯一不能让步的东西。

    def _pipelines() -> Any:
        return hub.pipelines

    def _encode_for(protocol: str, event: dict[str, Any]) -> dict[str, Any]:
        pipelines = _pipelines()
        if pipelines is None:
            return event
        encoded = pipelines.encode_event(protocol, event)
        # 编码失败时退回内部形状：一条首帧没发出去，比发一个结构对不上的帧要好查。
        return encoded if encoded is not None else event

    async def _run_action(protocol: str, name: str, params: dict[str, Any],
                          echo: Any = None) -> dict[str, Any]:
        pipelines = _pipelines()
        if pipelines is None:
            return await runtime.invoke(name, {**params, "echo": echo})
        try:
            decoded_name, decoded_params = pipelines.decode_action(protocol, name, params)
        except Exception as exc:  # noqa: BLE001 - 不认识的调用名要回给对方，而不是让它等到超时
            return models.fail(f"{protocol}: cannot decode action {name!r}: {exc}",
                               models.NOT_FOUND, echo=echo)
        result = await runtime.invoke(decoded_name, {**decoded_params, "echo": echo})
        return pipelines.format_reply(protocol, result, echo)

    async def handle_action(name: str, params: dict[str, Any],
                            protocol: str | None = None) -> dict[str, Any]:
        """Run one action. `protocol` is the calling connection's protocol; None means the HTTP default."""
        # HTTP 的动作入口现在挂在 `/ONEBOTV11/` 下，协议由路径决定。
        chosen = protocol if protocol is not None else "onebotv11"
        echo = params.get("echo")
        return await _run_action(chosen, name, params, echo)

    @app.get(prefixed(ONEBOTV11_PREFIX, "/health"))
    async def health() -> dict[str, Any]:
        return {"status": "ok", "self_id": runtime.self_id, "protocol": "onebotv11"}

    async def _run_body(body: Any) -> JSONResponse:
        if body is not None:
            hub.note_http(-1)     # paired with the +1 the caller took before parsing
        if not isinstance(body, dict):
            return JSONResponse(models.fail("body must be an object"), status_code=400)
        name = str(body.get("action", ""))
        params = body.get("params") or {}
        if not isinstance(params, dict):
            return JSONResponse(models.fail("params must be an object"), status_code=400)
        result = await handle_action(name, {**params, "echo": body.get("echo")})
        return JSONResponse(result)

    @app.post(prefixed(ONEBOTV11_PREFIX, config.http_path))
    async def action_post(request: Request) -> JSONResponse:
        hub.note_http(1)
        if not authorized(request):
            hub.note_http(-1)
            http_log.warn("onebot http rejected", reason="bad access_token", verb="POST",
                          path=str(request.url.path))
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

    @app.post(prefixed(ONEBOTV11_PREFIX, "/action"))
    async def action_post_standard(request: Request) -> JSONResponse:
        return await action_post(request)

    @app.get(prefixed(ONEBOTV11_PREFIX, "/action"))
    async def action_get_standard(request: Request) -> JSONResponse:
        return await action_get(request)

    @app.get(prefixed(ONEBOTV11_PREFIX, config.http_path))
    async def action_get(request: Request) -> JSONResponse:
        hub.note_http(1)
        if not authorized(request):
            hub.note_http(-1)
            http_log.warn("onebot http rejected", reason="bad access_token", verb="GET",
                          path=str(request.url.path))
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

    @app.websocket(prefixed(ONEBOTV11_PREFIX, config.ws_path))
    async def websocket_endpoint(ws: WebSocket) -> None:
        if not authorized(ws):
            ws_log.warn("onebot websocket rejected", reason="bad access_token",
                        client=str(getattr(ws, "client", "") or ""))
            await ws.close(code=1008, reason="unauthorized")
            return
        await ws.accept()
        # 前缀已经说明这是 OneBot v11；反查配置反而可能查错（同一 kind 有多条配置时只有第一条生效）。
        connection = hub.connection("ws_server", config.ws_path, protocol="onebotv11")
        await hub.add_ws(ws, connection)
        ws_log.info("onebot websocket connected", clients=len(hub._websockets),
                    protocol=connection.protocol, client=str(getattr(ws, "client", "") or ""))
        try:
            lifecycle = {
                "post_type": "meta_event",
                "meta_event_type": "lifecycle",
                "sub_type": "connect",
                "time": runtime.now(),
                "self_id": runtime.self_id,
            }
            await ws.send_text(json.dumps(
                _encode_for(connection.protocol, lifecycle), ensure_ascii=False))
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
                result = await _run_action(connection.protocol, action, params, msg.get("echo"))
                await ws.send_text(json.dumps(result, ensure_ascii=False))
        except WebSocketDisconnect:
            pass
        except Exception as exc:  # noqa: BLE001
            ws_log.warn("onebot websocket error", error=f"{type(exc).__name__}: {exc}")
        finally:
            await hub.remove_ws(ws)
            ws_log.info("onebot websocket disconnected", clients=len(hub._websockets))

    if config.enable_sse:

        @app.get(prefixed(ONEBOTV11_PREFIX, config.sse_path))
        async def sse(request: Request) -> StreamingResponse:
            if not authorized(request):
                return JSONResponse(models.fail("unauthorized", models.UNAUTHORIZED), status_code=401)

            queue = hub.add_sse(hub.connection("sse", config.sse_path, protocol="onebotv11"))

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

    # Milky 的端点挂在同一个应用上：两种协议的路径不重叠（v11 是 `/` 与 `/ws`，
    # Milky 是 `/api/{api}` 与 `/event`），方向也一致（协议端都是服务器）。
    #
    # 只在注册表里真的登记了 milky 时才挂：没有它的部署不该多出两个 404 路径。
    if "milky" in set(_registered_protocols()):
        from .milky_server import register_milky_routes

        register_milky_routes(app, runtime, hub, authorized)

    return app


def _registered_protocols() -> list[str]:
    from ..protocol_bridge import protocol_names

    return protocol_names()

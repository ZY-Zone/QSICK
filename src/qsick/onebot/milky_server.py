"""Milky 的传输端点：`POST /MILKY/api/{api}` 与 `GET /MILKY/event`。

## 拓扑与 v11 相反

OneBot v11 里 QSICK 是**服务器**（客户端连上来发动作、收事件），Milky 里协议端也是服务器——
方向一致，所以两种协议可以挂在同一个端口的不同路径上，不需要第二条监听。

差别在形状：

| | OneBot v11 | Milky |
| --- | --- | --- |
| 调用路径 | `POST /ONEBOTV11/`（动作名在 body 里） | `POST /MILKY/api/{api}`（动作名在路径里） |
| 调用体 | `{action, params, echo}` | 参数直接是 body，至少要 `{}` |
| 应答 | `{status, retcode, data, echo}` | `{status, retcode, message, data}` |
| 推送 | `GET /ONEBOTV11/`（WS/SSE） | `GET /MILKY/event`（WS/SSE），SSE 帧是 `event: milky_event` |
| 鉴权失败 | 401 | 401 |

信封与事件形状的转换**不在这里**——这里只做路由、鉴权与装帧，转换交给适配器
（`protocols/milky/adapter.py`）。这条分界和 v11 那边一致：路由不认识协议内容。

## 为什么 `Upgrade` 头决定通道

Milky 规范要求先看 `Upgrade: websocket`，否则按 SSE 处理。反过来做（先假定 SSE）会让每一个
WebSocket 客户端拿到一个不是握手的响应，然后双方各自超时——一个只在真实客户端连上来时才
出现的故障。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, StreamingResponse

from .. import log as logmod
from .server import MILKY_PREFIX, prefixed

log = logmod.get_logger("Milky.Server")

#: Milky 的 SSE 事件名。规范固定为这个字符串，不是可选项。
SSE_EVENT_NAME = "milky_event"


def register_milky_routes(app: FastAPI, runtime: Any, hub: Any, authorized: Any) -> None:
    """把 Milky 的两个端点挂到已有应用上。

    `authorized` 复用 v11 的鉴权（`access_token` 双向复用是 Milky 的明确要求：`/api` 与
    `/event` 都由协议端校验 `Authorization: Bearer`，`/event` 还允许 `?access_token=`）。
    """

    def unauthorized() -> JSONResponse:
        return JSONResponse(
            {"status": "failed", "retcode": -401, "message": "unauthorized", "data": None},
            status_code=401,
        )

    async def run(api: str, body: dict[str, Any]) -> dict[str, Any]:
        """一次 Milky 调用：解码 -> invoke -> 包信封。"""
        pipelines = hub.pipelines
        protocol = "milky"
        if pipelines is None:
            return {"status": "failed", "retcode": -500, "message": "no pipeline", "data": None}

        # 协议端自身的元信息不经过动作层：答案在本进程里，去问 QQ 反而错了。
        try:
            adapter = pipelines.protocol(protocol)
            local = getattr(adapter, "local_reply", None)
            answered = local(api, body) if callable(local) else None
        except Exception:  # noqa: BLE001 - 本地回答失败不该挡住别的调用
            answered = None
        if answered is not None:
            return answered

        try:
            name, params = pipelines.decode_action(protocol, api, body)
        except Exception as exc:  # noqa: BLE001 - 不认识的 API 要回 -400，不是 500
            return {"status": "failed", "retcode": -400,
                    "message": f"{api}: {exc}", "data": None}
        # 一定要经过 invoke：插件过滤、账号级出入限制、缓存、发送锁都在它的入口。
        try:
            result = await runtime.invoke(name, params)
        except Exception as exc:  # noqa: BLE001 - 裸 500 到客户端只剩一行 HTML
            return {"status": "failed", "retcode": -500,
                    "message": f"{api}: {str(exc) or type(exc).__name__}", "data": None}
        return pipelines.format_reply(protocol, result)

    @app.post(prefixed(MILKY_PREFIX, "/api/{api}"))
    async def milky_api(api: str, request: Request) -> JSONResponse:
        # v11 的 HTTP 动作入口也是 POST `/`，路径不冲突；这里只服务 `/api/*`。
        if not authorized(request):
            log.warn("milky api rejected", reason="bad access_token", api=api)
            return unauthorized()
        body: Any = {}
        raw = await request.body()
        if raw:
            try:
                body = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                return JSONResponse(
                    {"status": "failed", "retcode": -400, "message": "body is not valid JSON",
                     "data": None}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse(
                {"status": "failed", "retcode": -400, "message": "body must be an object",
                 "data": None}, status_code=400)
        return JSONResponse(await run(api, body))

    @app.get(prefixed(MILKY_PREFIX, "/event"))
    async def milky_event(request: Request) -> Any:
        """推送通道。`Upgrade: websocket` 交给下面的 websocket 路由，其余走 SSE。"""
        if not authorized(request):
            return unauthorized()
        # 协议显式写死：`/MILKY/event` 是 Milky 的固定路径，它的协议由路径本身决定，
        # 按 profile 反查只可能查错。
        queue = hub.add_sse(hub.connection("sse", "/event", protocol="milky"))

        async def stream() -> Any:
            try:
                yield "retry: 3000\n\n"
                while True:
                    try:
                        item = await asyncio.wait_for(queue.get(), timeout=15)
                        # 装帧：`event: milky_event` + `data:` + 空行。payload 本身与 WS 帧完全一致。
                        yield f"event: {SSE_EVENT_NAME}\ndata: {item}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keep-alive\n\n"
            except asyncio.CancelledError:
                raise
            finally:
                hub.remove_sse(queue)

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.websocket(prefixed(MILKY_PREFIX, "/event"))
    async def milky_event_ws(ws: WebSocket) -> None:
        if not authorized(ws):
            await ws.close(code=1008, reason="unauthorized")
            return
        await ws.accept()
        await hub.add_ws(ws, hub.connection("ws_server", "/event", protocol="milky"))
        log.info("milky websocket connected", clients=len(hub._websockets))
        try:
            while True:
                raw = await ws.receive_text()
                try:
                    payload = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    await ws.send_text(json.dumps(
                        {"status": "failed", "retcode": -400, "message": "invalid JSON", "data": None}))
                    continue
                if not isinstance(payload, dict):
                    continue
                # WebSocket 上也能发调用：Milky 允许客户端在事件通道上直接调 API，
                # 形状与 HTTP 一致（`{api, params}`）。规范没要求，但客户端普遍这么做。
                api = str(payload.get("api") or payload.get("action") or "")
                params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
                if not api:
                    continue
                await ws.send_text(json.dumps(await run(api, params), ensure_ascii=False))
        except WebSocketDisconnect:
            pass
        except Exception as exc:  # noqa: BLE001
            log.warn("milky websocket error", error=f"{type(exc).__name__}: {exc}")
        finally:
            await hub.remove_ws(ws)
            log.info("milky websocket disconnected", clients=len(hub._websockets))

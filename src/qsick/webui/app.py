"""WebUI: server-rendered pages, a live log stream and an action console.

HTMX is used through a tiny vendored subset (``static/htmx-lite.js``) so the
console works offline; the markup keeps standard ``hx-*`` attributes, so dropping
in real htmx later is a one-line change.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
import os
import re
import secrets
import time
from urllib.parse import quote
from pathlib import Path
from typing import Any, TYPE_CHECKING

from fastapi import FastAPI, Form, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from .. import log as logmod
from .auth import COOKIE_NAME as AUTH_COOKIE, SESSION_SECONDS, AuthStore
from . import caches as cachmod
from .cursor_types import CURSOR_STATES

log = logmod.get_logger("WebUI")
from .. import downloads
from ..headless import REPO_ROOT as _QSICK_ROOT
from ..headless import HeadlessManager
from .. import network_profiles as netprofiles
from ..market import MarketClient, MarketError, download_archive, install_archive

if TYPE_CHECKING:  # pragma: no cover
    from ..runtime import Runtime

BASE = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE / "templates"))


def _asset_version(static_dir: Path) -> str:
    """A short token that changes whenever a bundled asset changes.

    Built from the newest mtime plus the total size, which is enough to tell "the same bytes" from "not", and does
    not need hashing three files on every render -- it is computed once, at startup.
    """
    newest = 0.0
    total = 0
    for name in ("app.css", "app.js", "cache.css", "cursor.css", "cursor.js", "htmx-lite.js"):
        try:
            stat = (static_dir / name).stat()
        except OSError:
            continue
        newest = max(newest, stat.st_mtime)
        total += stat.st_size
    return f"{int(newest):x}{total:x}"


#: Injected into every template so `base.html` can stamp the real version onto its <link>/<script> URLs.
ASSET_VERSION = _asset_version(BASE / "static")

#: The settings page's sub-sections. Validated on the way in so an unknown ?tab= falls back instead of rendering a
#: page with no panels at all.
SETTINGS_TABS = ("general", "display", "security", "cache", "advanced")


def _human_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days}天 {hours}小时"
    if hours:
        return f"{hours}小时 {minutes}分"
    if minutes:
        return f"{minutes}分 {secs}秒"
    return f"{secs}秒"


def _relative(ts: float | int | None) -> str:
    if not ts:
        return "—"
    delta = int(time.time() - float(ts))
    if delta < 0:
        return "刚刚"
    if delta < 60:
        return f"{delta}秒前"
    if delta < 3600:
        return f"{delta // 60}分钟前"
    if delta < 86400:
        return f"{delta // 3600}小时前"
    return f"{delta // 86400}天前"


templates.env.filters["duration"] = _human_duration
templates.env.filters["reltime"] = _relative


def _relogin_delays() -> list[tuple[int, str]]:
    """自动重登时延的下拉选项。

    选项来自 ``headless.RELOGIN_DELAY_CHOICES``，界面与校验共用同一份定义——
    否则后端拒收某档位时，下拉里还会留着它。
    """
    from ..headless import RELOGIN_DELAY_CHOICES, RELOGIN_DELAY_LABELS

    return [(seconds, RELOGIN_DELAY_LABELS.get(seconds, f"{seconds} 秒"))
            for seconds in RELOGIN_DELAY_CHOICES]


def _auth(app: "FastAPI") -> AuthStore:
    """The live auth store.

    Read through `app.state` rather than captured in a closure: a store only the builder can reach cannot be swapped
    for a test, and cannot be reloaded later without rebuilding the whole app.
    """
    return app.state.auth


def create_app(runtime: "Runtime") -> FastAPI:
    app = FastAPI(title="QSICK WebUI", docs_url=None, redoc_url=None)
    app.state.runtime = runtime
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")

    #: Cache-busting token for app.css / app.js, derived from the files themselves.
    #:
    #: It used to be a hand-written string in base.html, last touched on 2026-11-14. Every UI change since then
    #: shipped behind a URL the browser had already cached, so the running page silently kept the old JavaScript --
    #: which is exactly what "the dropdown is still broken" looked like from the outside: the fix was on disk and in
    #: the HTML, and the browser was still executing last week's file. A version nobody has to remember to bump is
    #: the only kind that stays correct.
    templates.env.globals["asset_version"] = ASSET_VERSION

    #: Password authentication, alongside the access token. Loaded once per app so a restart drops every session --
    #: a stale cookie that outlives the process it was issued by is the last thing a console wants.
    app.state.auth = AuthStore(runtime.config.path("webui", "auth.json"))
    app.state.auth.load()
    # The cache panel clears the login history through this, so it has to be reachable from the runtime too.
    runtime.auth_store = app.state.auth

    def template_context(request: Request) -> dict[str, Any]:
        """Values every page needs, computed per render.

        A context processor rather than a Jinja global: a global would be shared by concurrent requests, so two
        tabs served at the same moment could overwrite each other's answer. These are cheap predicates, but
        "cheap and wrong" is still wrong.
        """
        return {
            "needs_password_setup": (
                not _auth(app).has_password
                and request.cookies.get("qsick_setup_dismissed", "") != "1"
                and not request.url.path.startswith("/login")
            ),
            "has_password": _auth(app).has_password,
        }

    templates.context_processors.insert(0, template_context)

    def client_ip(request: Request) -> str:
        """The address to record in the login history.

        X-Forwarded-For is honoured only when it is the first hop's single value, and the direct peer otherwise: a
        header a client can set freely is a note from the caller, not evidence.
        """
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            first = forwarded.split(",")[0].strip()
            if first:
                return first
        return request.client.host if request.client else ""

    def wants_password_setup(request: Request) -> bool:
        """True on the very first visit of a password-less install.

        The console has been open to anyone who could reach the port until now; the prompt is a nudge to close that,
        shown once per browser session rather than blocked on, because refusing to render the dashboard until a
        password exists would lock out the person who just installed it.
        """
        if _auth(app).has_password:
            return False
        return request.cookies.get("qsick_setup_dismissed", "") != "1"

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Response:
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        if request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "public, max-age=3600"
        return response

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
        if exc.status_code == 404 and not request.url.path.startswith("/api/"):
            return templates.TemplateResponse(
                request, "404.html", {"page": "404", "info": runtime.info()}, status_code=404
            )
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    def current_token() -> str:
        # Read live so a token change on /network applies without recreating the app.
        return runtime.config.webui.access_token

    def authorized(request: Request) -> bool:
        """Whether this request may in.

        Three ways through, in order of how much they get used:

          * a valid access token, in the query, a cookie or a header -- the path scripts and API clients take;
          * a live password session, which is what a person browsing uses;
          * neither configured at all, which is the out-of-the-box state on a local machine.

        A password does not disable the token: an operator who sets a password for the browser must not thereby
        break the `curl` calls that carried the token, and the token is still the shorter-lived, rotatable option
        of the two.
        """
        token = current_token()
        supplied = request.query_params.get("token") or request.cookies.get("qsick_token")
        supplied = supplied or request.headers.get("x-access-token", "")
        if token and supplied and secrets.compare_digest(str(supplied), str(token)):
            return True
        if _auth(app).has_password:
            return _auth(app).touch(request.cookies.get(AUTH_COOKIE, "")) is not None
        # No password set: fall back to the token rule, and with no token either, the console is open.
        return not token

    def guard(request: Request) -> Response | None:
        if authorized(request):
            return None
        if request.url.path.startswith("/api/"):
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        target = quote(request.url.path + (("?" + request.url.query) if request.url.query else ""), safe="/")
        return RedirectResponse(f"/login?next={target}", status_code=303)

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
            '<rect x="1" y="1" width="30" height="30" rx="7" fill="#ffffff" stroke="#0ea5e9" stroke-width="2"/>'
            '<text x="16" y="21" font-family="Segoe UI,sans-serif" font-size="12" '
            'font-weight="700" text-anchor="middle" fill="#0284c7">QS</text></svg>'
        )
        return Response(svg, media_type="image/svg+xml")

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request, next: str = "/") -> HTMLResponse:
        if authorized(request):
            return RedirectResponse(next or "/", status_code=303)
        # Nothing to ask for when neither a password nor a token is configured -- the console is simply open.
        if not _auth(app).has_password and not current_token():
            return RedirectResponse("/", status_code=303)
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "page": "login",
                "info": runtime.info(),
                "next": next,
                "error": "",
                "needs_password": _auth(app).has_password,
                "token_login": bool(current_token()),
                "session_minutes": SESSION_SECONDS // 60,
            },
        )

    @app.post("/login", response_class=HTMLResponse)
    async def login_submit(
        request: Request,
        password: str = Form(""),
        token_value: str = Form("", alias="token"),
        next: str = Form("/"),
    ):
        ip = client_ip(request)
        agent = request.headers.get("user-agent", "")

        if _auth(app).has_password and password:
            if _auth(app).check(password):
                session = _auth(app).start_session(ip)
                _auth(app).note_login(ip, True, agent)
                response = RedirectResponse(next or "/", status_code=303)
                response.set_cookie(
                    AUTH_COOKIE, session.token, httponly=True, samesite="lax",
                    max_age=SESSION_SECONDS,
                )
                return response
            _auth(app).note_login(ip, False, agent)
            return templates.TemplateResponse(
                request,
                "login.html",
                {"page": "login", "info": runtime.info(), "next": next, "error": "密码不正确",
                 "needs_password": True, "token_login": bool(current_token()),
                 "session_minutes": SESSION_SECONDS // 60},
                status_code=401,
            )

        expected = current_token()
        if expected and token_value and secrets.compare_digest(token_value, expected):
            _auth(app).note_login(ip, True, agent + " (token)")
            response = RedirectResponse(next or "/", status_code=303)
            response.set_cookie("qsick_token", token_value, httponly=True, samesite="lax")
            return response

        _auth(app).note_login(ip, False, agent)
        message = "密码不正确" if _auth(app).has_password else "token 不正确"
        return templates.TemplateResponse(
            request,
            "login.html",
            {"page": "login", "info": runtime.info(), "next": next, "error": message,
             "needs_password": _auth(app).has_password, "token_login": bool(current_token()),
             "session_minutes": SESSION_SECONDS // 60},
            status_code=401,
        )

    @app.post("/logout")
    async def logout(request: Request) -> RedirectResponse:
        _auth(app).end_session(request.cookies.get(AUTH_COOKIE, ""))
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie("qsick_token")
        response.delete_cookie(AUTH_COOKIE)
        return response

    # ---------------------------------------------------------------- 安全与隐私

    @app.post("/api/security/password")
    async def set_password(request: Request) -> JSONResponse:
        """Set or change the WebUI password.

        Changing an existing password requires the current one. Setting the first one does not -- there is nothing to
        confirm against, and demanding a password in order to create the first password is a lock with no key.
        """
        denied = guard(request)
        if denied:
            return denied
        try:
            payload = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JSONResponse({"ok": False, "error": "请求体不是 JSON"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"ok": False, "error": "请求体必须是对象"}, status_code=400)

        current = str(payload.get("current") or "")
        fresh = str(payload.get("password") or "")
        again = str(payload.get("confirm") or "")

        if _auth(app).has_password and not _auth(app).check(current):
            return JSONResponse({"ok": False, "error": "当前密码不正确"}, status_code=403)
        if len(fresh) < 6:
            return JSONResponse({"ok": False, "error": "新密码至少 6 位"}, status_code=400)
        if fresh != again:
            return JSONResponse({"ok": False, "error": "两次输入不一致"}, status_code=400)
        if fresh == current:
            return JSONResponse({"ok": False, "error": "新密码与当前密码相同"}, status_code=400)

        _auth(app).set_password(fresh)
        _auth(app).note_login(client_ip(request), True, "password changed")
        # The password change invalidated every session, including this one; hand back a fresh cookie so the caller
        # is not logged out by its own success.
        session = _auth(app).start_session(client_ip(request))
        response = JSONResponse({"ok": True, "message": "密码已更新，其它会话已失效"})
        response.set_cookie(AUTH_COOKIE, session.token, httponly=True, samesite="lax", max_age=SESSION_SECONDS)
        return response

    @app.post("/api/security/password/clear")
    async def clear_password(request: Request) -> JSONResponse:
        denied = guard(request)
        if denied:
            return denied
        if not _auth(app).has_password:
            return JSONResponse({"ok": True, "message": "本来就没有设置密码"})
        try:
            payload = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = {}
        current = str((payload or {}).get("current") or "")
        if not _auth(app).check(current):
            return JSONResponse({"ok": False, "error": "当前密码不正确"}, status_code=403)
        _auth(app).clear_password()
        _auth(app).note_login(client_ip(request), True, "password cleared")
        response = JSONResponse({"ok": True, "message": "密码已移除，WebUI 重新开放访问"})
        response.delete_cookie(AUTH_COOKIE)
        return response

    @app.post("/api/security/setup-dismissed")
    async def dismiss_setup(request: Request) -> JSONResponse:
        """Remember that this browser said "not now" to the first-run password prompt."""
        response = JSONResponse({"ok": True})
        response.set_cookie("qsick_setup_dismissed", "1", max_age=7 * 24 * 3600, samesite="lax")
        return response

    # ---------------------------------------------------------------- 缓存清理

    @app.get("/api/cache")
    async def cache_status(request: Request) -> JSONResponse:
        denied = guard(request)
        if denied:
            return denied
        return JSONResponse({"items": cachmod.measure(cachmod.collect(runtime))})

    @app.post("/api/cache/clear")
    async def cache_clear(request: Request) -> JSONResponse:
        """Clear one cache, or all of them with `{"id": "all"}`."""
        denied = guard(request)
        if denied:
            return denied
        try:
            payload = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JSONResponse({"ok": False, "error": "请求体不是 JSON"}, status_code=400)
        item_id = str((payload or {}).get("id") or "").strip()
        if not item_id:
            return JSONResponse({"ok": False, "error": "缺少 id"}, status_code=400)

        items = cachmod.collect(runtime)
        result = cachmod.clear(items, item_id)
        if result.get("ok"):
            logmod.get_logger("WebUI.Cache").info(
                "cleared caches", id=item_id, freed=result.get("freed_human"))
            # Hand back the fresh sizes so the page does not have to guess what is left.
            result["items"] = cachmod.measure(cachmod.collect(runtime))
        return JSONResponse(result, status_code=200 if result.get("ok") else 400)

    @app.get("/api/security/overview")
    async def security_overview(request: Request) -> JSONResponse:
        denied = guard(request)
        if denied:
            return denied
        return JSONResponse({
            "has_password": _auth(app).has_password,
            "token_enabled": bool(current_token()),
            "session_seconds": SESSION_SECONDS,
            "sessions": _auth(app).active_sessions(),
            "logins": _auth(app).recent_logins(30),
        })

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request) -> HTMLResponse:
        denied = guard(request)
        if denied:
            return denied
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "page": "dashboard",
                "info": runtime.info(),
                "messages": runtime.store.recent(12),
            },
        )

    @app.get("/partials/status", response_class=HTMLResponse)
    async def status_partial(request: Request) -> HTMLResponse:
        denied = guard(request)
        if denied:
            return denied
        return templates.TemplateResponse(
            request, "partials/status.html", {"info": runtime.info()}
        )

    @app.get("/logs", response_class=HTMLResponse)
    async def logs_page(request: Request) -> HTMLResponse:
        denied = guard(request)
        if denied:
            return denied
        return templates.TemplateResponse(
            request,
            "logs.html",
            {
                "page": "logs",
                "info": runtime.info(),
                "records": [r.to_dict() for r in logmod.BUFFER.tail(300)],
                # Declared categories, so the filter offers "Plugins" and "OneBot.WS" before anything has written to
                # them. Populating from observed records alone meant the categories that matter exactly when they are
                # empty were the ones that could not be selected.
                #
                # Grouped, not flat: eighteen entries is a list to read, not a filter to use. Four groups answer the
                # four questions someone actually has. Plugins are the exception and stay individual, because "which
                # plugin said this" is the whole reason to filter there.
                "groups": logmod.grouped_categories(),
                "category_groups": {entry["category"]: entry["group"]
                                    for entry in logmod.known_categories()},
            },
        )

    @app.get("/api/logs")
    async def logs_api(request: Request, cursor: int = 0, limit: int = 500) -> JSONResponse:
        denied = guard(request)
        if denied:
            return denied
        records = logmod.BUFFER.since(cursor)[-limit:]
        return JSONResponse({"records": [record.to_dict() for record in records]})

    @app.get("/api/logs/stream")
    async def logs_stream(request: Request) -> StreamingResponse:
        denied = guard(request)
        if denied:
            return denied
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=512)

        def on_record(record: logmod.Record) -> None:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, record.to_dict())
            except RuntimeError:
                pass

        unsubscribe = logmod.BUFFER.subscribe(on_record)

        async def stream() -> Any:
            try:
                while True:
                    try:
                        item = await asyncio.wait_for(queue.get(), timeout=15)
                        yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keep-alive\n\n"
            except asyncio.CancelledError:
                raise
            finally:
                unsubscribe()

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.get("/api/status")
    async def status_api(request: Request) -> JSONResponse:
        denied = guard(request)
        if denied:
            return denied
        return JSONResponse(runtime.info())

    @app.get("/api/charts")
    async def charts_api(request: Request) -> JSONResponse:
        """The overview's three charts, polled separately from the status tiles.

        Separate because of the cadence: the tiles want two seconds, a thirty-minute trend does not, and rebuilding
        a few hundred SVG points every two seconds to redraw an identical line is work nobody asked for.
        """
        denied = guard(request)
        if denied:
            return denied
        # Take a sample on demand as well as on the timer: right after a restart the ring is empty, and a page
        # opened in that window should draw from its first refresh rather than from five seconds later.
        runtime.sample_metrics()
        return JSONResponse(runtime.chart_data())

    @app.get("/metrics")
    async def metrics(request: Request) -> PlainTextResponse:
        denied = guard(request)
        if denied:
            return denied
        info = runtime.info()
        lines = [
            "# HELP qsick_up Whether the runtime is online.",
            "# TYPE qsick_up gauge",
            f"qsick_up {1 if info['online'] else 0}",
            f"qsick_logged_in {1 if info['logged_in'] else 0}",
            f"qsick_uptime_seconds {info['uptime']}",
            f"qsick_events_total {info['events_seen']}",
            f"qsick_events_dropped_total {info['events_dropped']}",
            f"qsick_messages_stored {info['messages_stored']}",
            f"qsick_actions {info['actions']}",
            f"qsick_plugins {info['plugins']}",
            f"qsick_events_duplicate_total {info['events_duplicate']}",
        ]
        hub = runtime.hub.stats()
        lines.append(f"qsick_ws_clients {hub['websockets']}")
        lines.append(f"qsick_sse_clients {hub['sse']}")
        lines.append(f"qsick_sse_dropped_total {hub['dropped_sse']}")
        lines.append(f"qsick_ws_slow_total {hub['slow_ws']}")
        cache = runtime.cache.stats()
        lines.append(f"qsick_cache_entries {cache['entries']}")
        lines.append(f"qsick_cache_hits_total {cache['hits']}")
        lines.append(f"qsick_cache_misses_total {cache['misses']}")
        for name, stat in sorted(runtime.action_stats().items()):
            if not stat["calls"]:
                continue
            lines.append(f'qsick_action_calls_total{{action="{name}"}} {int(stat["calls"])}')
            lines.append(f'qsick_action_errors_total{{action="{name}"}} {int(stat["errors"])}')
            lines.append(f'qsick_action_latency_ms_sum{{action="{name}"}} {stat["total_ms"]:.3f}')
        return PlainTextResponse("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")

    @app.get("/api/actions")
    async def actions_api(request: Request, q: str = "", category: str = "") -> JSONResponse:
        denied = guard(request)
        if denied:
            return denied
        return JSONResponse({"actions": _filter_specs(runtime.registry.specs(), q, category)})

    @app.get("/api/actions/{name}")
    async def action_detail(name: str, request: Request) -> JSONResponse:
        denied = guard(request)
        if denied:
            return denied
        for spec in runtime.registry.specs():
            if spec["name"] == name:
                return JSONResponse(spec)
        return JSONResponse({"ok": False, "error": f"没有名为 {name!r} 的动作"}, status_code=404)

    @app.post("/api/actions/{name}")
    async def invoke_api(name: str, request: Request) -> JSONResponse:
        denied = guard(request)
        if denied:
            return denied
        try:
            params = await request.json()
        except json.JSONDecodeError:
            params = {}
        if not isinstance(params, dict):
            params = {}
        try:
            result = await runtime.invoke(name, params)
        except Exception as exc:  # noqa: BLE001 - 传输层/宿主抛的异常类型很杂
            # 不兜底就会变成裸 500，调用方只能看到 "Internal Server Error"，
            # 真正的失败原因（宿主报了什么）被吞掉。
            return JSONResponse({"status": "failed", "retcode": 1200,
                                 "message": f"{name} 执行失败：{exc}", "data": None},
                                status_code=502)
        return JSONResponse(result)

    @app.get("/plugins", response_class=HTMLResponse)
    async def plugins_page(request: Request) -> HTMLResponse:
        denied = guard(request)
        if denied:
            return denied
        return templates.TemplateResponse(
            request, "plugins.html", {"page": "plugins", "info": runtime.info(), "plugins": runtime.plugins.view()}
        )

    def _market_client() -> MarketClient:
        return MarketClient(runtime.config.plugins.market_url, runtime.config.plugins.market_timeout)

    @app.get("/plugins/market", response_class=HTMLResponse)
    async def market_page(
        request: Request,
        q: str = "",
        sort: str = "published",
        order: str = "desc",
        client_version: str = "",
        page: int = 1,
        msg: str = "",
    ) -> HTMLResponse:
        denied = guard(request)
        if denied:
            return denied
        data: dict[str, Any] = {"items": [], "total": 0, "pages": 1, "page": 1}
        error = ""
        try:
            data = await _market_client().list_plugins(
                q=q, sort=sort, order=order, client=client_version, page=page
            )
        except MarketError as exc:
            error = str(exc)
        return templates.TemplateResponse(
            request,
            "market.html",
            {
                "page": "market",
                "info": runtime.info(),
                "market_url": runtime.config.plugins.market_url,
                "plugins": data.get("items", []),
                "total": data.get("total", 0),
                "page_no": data.get("page", 1),
                "pages": data.get("pages", 1),
                "q": q,
                "sort": sort,
                "order": order,
                "client_version": client_version,
                "error": error,
                "msg": msg,
                "allow_install": runtime.config.plugins.allow_remote_install,
            },
        )

    @app.post("/plugins/market/{plugin_id}/like")
    async def market_like(plugin_id: str, request: Request) -> RedirectResponse:
        voter = hashlib.sha256(
            f"{request.client.host if request.client else ''}|{request.headers.get('user-agent', '')}".encode()
        ).hexdigest()[:24]
        try:
            await _market_client().like(plugin_id, voter)
        except MarketError as exc:
            log.warn("market like failed", id=plugin_id, error=str(exc))
        return RedirectResponse("/plugins/market", status_code=303)

    @app.post("/plugins/market/{plugin_id}/install")
    async def market_install(plugin_id: str) -> RedirectResponse:
        if not runtime.config.plugins.allow_remote_install:
            return RedirectResponse("/plugins/market?msg=远程安装未启用", status_code=303)
        try:
            info = await _market_client().install_info(plugin_id)
            archive_url = str(info.get("archive_url") or "")
            if not archive_url:
                raise MarketError("market returned no archive URL")
            payload = await download_archive(archive_url)
            root = Path(runtime.config.plugins.dirs[0] if runtime.config.plugins.dirs else "plugins")
            root.mkdir(parents=True, exist_ok=True)
            installed = install_archive(payload, root, expected_id=plugin_id)
            manifest = next((item for item in runtime.plugins.discover() if item.id == installed), None)
            if installed in runtime.plugins._loaded:
                await runtime.plugins.reload(installed)
            elif manifest is not None:
                await runtime.plugins.load(manifest)
            return RedirectResponse(f"/plugins/market?msg=已安装 {installed}", status_code=303)
        except (MarketError, OSError) as exc:
            log.warn("market install failed", id=plugin_id, error=str(exc))
            return RedirectResponse(f"/plugins/market?msg=安装失败：{exc}", status_code=303)

    def _manifest(plugin_id: str):
        return next((item for item in runtime.plugins.discover() if item.id == plugin_id), None)

    @app.get("/plugins/{plugin_id}/config", response_class=HTMLResponse)
    async def plugin_config_page(request: Request, plugin_id: str, saved: int = 0) -> HTMLResponse:
        denied = guard(request)
        if denied:
            return denied
        manifest = _manifest(plugin_id)
        if manifest is None:
            return HTMLResponse("plugin not found", status_code=404)
        current = runtime.plugins.plugin_config(plugin_id)
        enabled = plugin_id not in runtime.plugins._disabled and plugin_id in runtime.plugins._loaded
        return templates.TemplateResponse(
            request,
            "plugin_config.html",
            {
                "page": "plugins",
                "info": runtime.info(),
                "plugin": manifest.to_dict(),
                "fields": _config_fields(manifest, current),
                "enabled": enabled,
                "saved": bool(saved),
                "config_path": str(runtime.plugins.config_path(plugin_id)),
            },
        )

    @app.post("/plugins/{plugin_id}/config")
    async def plugin_config_save(plugin_id: str, request: Request) -> RedirectResponse:
        form = await request.form()
        manifest = _manifest(plugin_id)
        if manifest is None:
            return RedirectResponse("/plugins", status_code=303)
        types = {str(item["key"]): str(item.get("type", "str")) for item in manifest.config_fields}
        meta: dict[str, tuple[str, bool]] = {}
        for item in manifest.config_fields:
            key = str(item.get("key", ""))
            if not key:
                continue
            raw_enable = item.get("configEnable", item.get("config_enable"))
            enable = raw_enable if isinstance(raw_enable, dict) else {}
            widget = str(enable.get("type", "text")).lower()
            if widget in ("switch", "toggle", "bool", "boolean"):
                widget = "switch"
            elif widget in ("select", "dropdown", "choice"):
                widget = "select"
            else:
                widget = "text"
            visible = bool(enable.get("ifShow", enable.get("if_show", False))) if isinstance(raw_enable, dict) else True
            meta[key] = (widget, visible)

        # Start from the stored values so hidden fields are not erased on save.
        values: dict[str, Any] = dict(runtime.plugins.plugin_config(plugin_id))
        submitted: set[str] = set()
        for key, raw in form.items():
            if not key.startswith("cfg_"):
                continue
            field = key[4:]
            submitted.add(field)
            if meta.get(field, ("text", True))[0] == "switch":
                values[field] = str(raw).lower() in ("1", "true", "yes", "on")
            else:
                values[field] = _coerce_field(str(raw), types.get(field, "str"))
        # An unchecked switch is absent from the form; persist it as false.
        for field, (widget, visible) in meta.items():
            if widget == "switch" and visible and field not in submitted:
                values[field] = False
        runtime.plugins.save_plugin_config(plugin_id, values)
        await runtime.plugins.set_enabled(plugin_id, form.get("enabled") == "on")
        return RedirectResponse(f"/plugins/{plugin_id}/config?saved=1", status_code=303)

    @app.get("/plugins/{plugin_id}/config/page")
    async def plugin_custom_page(plugin_id: str) -> Any:
        manifest = _manifest(plugin_id)
        if manifest is None or manifest.config_ui != "custom" or not manifest.config_page:
            return HTMLResponse("no custom page", status_code=404)
        base = manifest.path.parent.resolve()
        target = (base / manifest.config_page).resolve()
        if base not in target.parents or not target.is_file():
            return HTMLResponse("invalid config page path", status_code=400)
        return FileResponse(target)

    @app.get("/api/plugins/{plugin_id}/config")
    async def plugin_config_api(plugin_id: str, request: Request) -> JSONResponse:
        denied = guard(request)
        if denied:
            return denied
        return JSONResponse(runtime.plugins.plugin_config(plugin_id))

    @app.post("/api/plugins/{plugin_id}/config")
    async def plugin_config_api_save(plugin_id: str, request: Request) -> JSONResponse:
        denied = guard(request)
        if denied:
            return denied
        try:
            values = await request.json()
        except json.JSONDecodeError:
            return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)
        if not isinstance(values, dict):
            return JSONResponse({"ok": False, "error": "expected object"}, status_code=400)
        runtime.plugins.save_plugin_config(plugin_id, values)
        return JSONResponse({"ok": True})

    @app.post("/plugins/{plugin_id}/config/reset")
    async def reset_plugin_config(plugin_id: str) -> RedirectResponse:
        path = runtime.plugins.config_path(plugin_id)
        if path.exists():
            path.unlink()
        await runtime.plugins.reload(plugin_id)
        return RedirectResponse(f"/plugins/{plugin_id}/config?saved=1", status_code=303)

    @app.post("/plugins/{plugin_id}/uninstall")
    async def uninstall_plugin(plugin_id: str) -> RedirectResponse:
        await runtime.plugins.uninstall(plugin_id)
        return RedirectResponse("/plugins", status_code=303)

    @app.post("/plugins/{plugin_id}/update")
    async def update_plugin(plugin_id: str) -> RedirectResponse:
        await runtime.plugins.update(plugin_id)
        return RedirectResponse("/plugins", status_code=303)

    @app.post("/plugins/{plugin_id}/toggle")
    async def toggle_plugin(plugin_id: str, enabled: str = Form("1")) -> RedirectResponse:
        await runtime.plugins.set_enabled(plugin_id, enabled == "1")
        return RedirectResponse("/plugins", status_code=303)

    @app.post("/plugins/{plugin_id}/reload")
    async def reload_plugin(plugin_id: str) -> RedirectResponse:
        await runtime.plugins.reload(plugin_id)
        return RedirectResponse("/plugins", status_code=303)

    @app.get("/api/plugins")
    async def plugins_api(request: Request) -> JSONResponse:
        denied = guard(request)
        if denied:
            return denied
        return JSONResponse({"plugins": runtime.plugins.view()})

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request, saved: int = 0, tab: str = "general") -> HTMLResponse:
        denied = guard(request)
        if denied:
            return denied
        # The tab lives in the query string rather than in client state: a reload has to land on the same panel, and
        # a link like /settings?tab=security has to be shareable with the person you are asking to set a password.
        chosen = tab if tab in SETTINGS_TABS else "general"
        info = runtime.info()
        info["has_password"] = _auth(app).has_password
        info["token_enabled"] = bool(current_token())
        return templates.TemplateResponse(
            request,
            "settings.html",
            {
                "page": "settings",
                "info": info,
                "config": runtime.config,
                "config_json": _config_json(runtime),
                "saved": bool(saved),
                "tab": chosen,
                "cursor_types": CURSOR_STATES,
                "caches": cachmod.measure(cachmod.collect(runtime)) if chosen == "cache" else [],
            },
        )

    @app.post("/settings/log")
    async def save_log_level(level: str = Form("INFO")) -> RedirectResponse:
        name = level.strip().upper()
        if name not in ("TRACE", "DEBUG", "INFO", "WARN", "ERROR"):
            return RedirectResponse("/settings?saved=0", status_code=303)
        logmod.configure(level=name)
        runtime.config.log.level = name
        _write_setting(runtime, {"log": {"level": name}})
        return RedirectResponse("/settings?saved=1", status_code=303)

    @app.post("/settings/storage")
    async def save_storage(max_messages: int = Form(5000), max_days: int = Form(7)) -> RedirectResponse:
        max_messages = max(0, min(max_messages, 1_000_000))
        max_days = max(0, min(max_days, 3650))
        runtime.config.rotation.max_messages = max_messages
        runtime.config.rotation.max_days = max_days
        # The cache is what these limits are for -- it is the store the 消息缓存 page reads and the one the
        # overview counts. Setting only `runtime.store` used to leave the cache's limits untouched, so the form
        # wrote a number that nothing read.
        for store in (runtime.cache_store, runtime.store):
            for attribute, value in (("max_messages", max_messages), ("max_days", max_days)):
                if hasattr(store, attribute):
                    setattr(store, attribute, value)
        _write_setting(runtime, {"rotation": {"max_messages": max_messages, "max_days": max_days}})
        return RedirectResponse("/settings?saved=1", status_code=303)

    @app.post("/settings/runtime")
    async def save_runtime(watch: str = Form("0")) -> RedirectResponse:
        enabled = watch == "1"
        runtime.config.plugins.watch = enabled
        _write_setting(runtime, {"plugins": {"watch": enabled}})
        return RedirectResponse("/settings?saved=1", status_code=303)

    @app.get("/settings/config.json")
    async def download_config(request: Request) -> Response:
        """The sanitized config as a download.

        之前设置页上挂的是 `/config`，而那个路由从来不存在 —— 一个 404 的"下载配置"。
        这里用真实端点：内容和 `/api/status` 里的脱敏版本同源（`_config_json`），
        所以不会出现"页面看到的和下载到的不是一份东西"。
        """
        denied = guard(request)
        if denied:
            return denied
        # `_config_json` already returns encoded JSON text; dumping it again would hand back a quoted string.
        return Response(
            _config_json(runtime),
            media_type="application/json; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="qsick-config.json"'},
        )

    @app.post("/settings/transport")
    async def save_transport(
        pipe_auto_start: str = Form("0"),
        network_activation_delay: float = Form(15.0),
        forward_noise: str = Form("0"),
    ) -> RedirectResponse:
        """启动与转发行为。

        延迟限制在 0-600 秒：负数没有意义，而大到一个小时以上只会让"网络怎么还没起来"变成
        一个查不明白的问题。
        """
        delay = max(0.0, min(float(network_activation_delay or 0), 600.0))
        pipe = pipe_auto_start == "1"
        noise = forward_noise == "1"
        runtime.config.pipe_auto_start = pipe
        runtime.config.network_activation_delay = delay
        runtime.config.forward_noise = noise
        _write_setting(runtime, {
            "pipe_auto_start": pipe,
            "network_activation_delay": delay,
            "forward_noise": noise,
        })
        return RedirectResponse("/settings?saved=1&tab=general", status_code=303)

    @app.post("/settings/plugins")
    async def save_plugins(
        allow_remote_install: str = Form("0"),
        max_tier: str = Form("core"),
    ) -> RedirectResponse:
        tier = max_tier.strip().lower()
        if tier not in ("core", "verified", "any"):
            return RedirectResponse("/settings?saved=0&tab=advanced", status_code=303)
        runtime.config.plugins.allow_remote_install = allow_remote_install == "1"
        runtime.config.plugins.max_tier = tier
        _write_setting(runtime, {"plugins": {
            "allow_remote_install": runtime.config.plugins.allow_remote_install,
            "max_tier": tier,
        }})
        return RedirectResponse("/settings?saved=1&tab=advanced", status_code=303)

    @app.post("/settings/log-buffer")
    async def save_log_buffer(log_buffer: int = Form(2000)) -> RedirectResponse:
        """日志缓冲区行数。上限 20000：再多就只是把内存变成日志仓库，而不是便于排查的窗口。"""
        size = max(200, min(int(log_buffer or 2000), 20000))
        runtime.config.webui.log_buffer = size
        _write_setting(runtime, {"webui": {"log_buffer": size}})
        logmod.configure(capacity=size)
        return RedirectResponse("/settings?saved=1&tab=display", status_code=303)

    @app.post("/settings/pipe-auth")
    async def save_pipe_auth(
        pipe_auth: str = Form("random"), pipe_token: str = Form("")
    ) -> RedirectResponse:
        mode = pipe_auth.strip().lower()
        if mode not in ("random", "fixed", "none"):
            return RedirectResponse("/settings?saved=0", status_code=303)
        if mode == "fixed" and not pipe_token.strip():
            return RedirectResponse("/settings?saved=0", status_code=303)
        _write_setting(runtime, {"transport": {"pipe_auth": mode, "pipe_token": pipe_token.strip()}})
        runtime.config.transport.pipe_auth = mode
        runtime.config.transport.pipe_token = pipe_token.strip()
        return RedirectResponse("/settings?saved=1", status_code=303)

    def _parse_urls(raw: str) -> list[str]:
        items: list[str] = []
        for chunk in raw.replace(",", "\n").splitlines():
            value = chunk.strip()
            if value:
                items.append(value)
        return items

    def _apply_network(runtime: "Runtime", data: dict[str, Any]) -> list[str]:
        errors: list[str] = []
        onebot = runtime.config.onebot
        webui = runtime.config.webui
        host = str(data.get("host", onebot.host)).strip()
        try:
            port = int(data.get("port", onebot.port))
        except (TypeError, ValueError):
            port = -1
        if not host or " " in host:
            errors.append("监听地址不能为空或包含空格")
        if not 1 <= port <= 65535:
            errors.append("OneBot 端口必须在 1..65535 之间")
        # Only enabled transports can collide. The three paths live on one listener, so a duplicate
        # among them is a real ambiguity -- but a *disabled* transport contributes nothing, and its
        # path is only whatever stale value the config happens to hold. Comparing it anyway is what
        # rejected a lone ws_server at "/": the unused http_path was still "/" from the defaults, so
        # the page reported a duplicate for a path the user had typed exactly once.
        paths = {}
        if bool(data.get("enable_http", onebot.enable_http)):
            paths["http_path"] = str(data.get("http_path", onebot.http_path))
        if bool(data.get("enable_ws", onebot.enable_ws)):
            paths["ws_path"] = str(data.get("ws_path", onebot.ws_path))
        if bool(data.get("enable_sse", onebot.enable_sse)):
            paths["sse_path"] = str(data.get("sse_path", onebot.sse_path))
        for name, value in paths.items():
            if not value.startswith("/") or " " in value:
                errors.append(f"{name} 必须以 / 开头且不含空格")
        # SSE shares the HTTP path by design (OneBot v11 puts actions and the event stream on one
        # URL, split by method), so that pair is not a collision. The only choice left to the
        # operator is the WS path, and a WS endpoint at the same URL as HTTP is genuinely
        # ambiguous to anyone reading the address.
        chosen = {name: value for name, value in paths.items() if name != "sse_path"}
        if len(set(chosen.values())) != len(chosen):
            duplicated = sorted({value for value in chosen.values()
                                 if list(chosen.values()).count(value) > 1})
            errors.append("已启用的监听路径不能重复：" + "、".join(duplicated))
        urls = data.get("reverse_ws_urls", onebot.reverse_ws_urls)
        if isinstance(urls, str):
            urls = _parse_urls(urls)
        for url in urls:
            if not url.startswith(("ws://", "wss://")):
                errors.append(f"Reverse WS 地址必须以 ws:// 或 wss:// 开头：{url}")
        try:
            reconnect = float(data.get("reverse_reconnect_seconds", onebot.reverse_reconnect_seconds))
        except (TypeError, ValueError):
            reconnect = -1
        if not 0 <= reconnect <= 300:
            errors.append("重连间隔必须在 0..300 秒之间")
        webui_host = str(data.get("webui_host", webui.host)).strip()
        try:
            webui_port = int(data.get("webui_port", webui.port))
        except (TypeError, ValueError):
            webui_port = -1
        if not webui_host or " " in webui_host:
            errors.append("WebUI 地址不能为空或包含空格")
        if not 1 <= webui_port <= 65535:
            errors.append("WebUI 端口必须在 1..65535 之间")
        if errors:
            return errors

        onebot.host = host
        onebot.port = port
        onebot.enable_http = bool(data.get("enable_http", onebot.enable_http))
        onebot.enable_ws = bool(data.get("enable_ws", onebot.enable_ws))
        onebot.enable_sse = bool(data.get("enable_sse", onebot.enable_sse))
        for name, value in paths.items():
            setattr(onebot, name, value)
        if data.get("clear_onebot_token"):
            onebot.access_token = ""
        elif data.get("access_token"):
            onebot.access_token = str(data["access_token"])
        onebot.reverse_ws_urls = list(urls)
        onebot.reverse_reconnect_seconds = reconnect
        webui.host = webui_host
        webui.port = webui_port
        if data.get("clear_webui_token"):
            webui.access_token = ""
        elif data.get("webui_token"):
            webui.access_token = str(data["webui_token"])
        _write_setting(
            runtime,
            {
                "onebot": {
                    "host": onebot.host,
                    "port": onebot.port,
                    "enable_http": onebot.enable_http,
                    "enable_ws": onebot.enable_ws,
                    "enable_sse": onebot.enable_sse,
                    "http_path": onebot.http_path,
                    "ws_path": onebot.ws_path,
                    "sse_path": onebot.sse_path,
                    "access_token": onebot.access_token,
                    "reverse_ws_urls": onebot.reverse_ws_urls,
                    "reverse_reconnect_seconds": onebot.reverse_reconnect_seconds,
                },
                "webui": {
                    "host": webui.host,
                    "port": webui.port,
                    "access_token": webui.access_token,
                },
            },
        )
        return []

    def _network_view(runtime: "Runtime", account: str = "", kind: str = "") -> dict[str, Any]:
        profiles = netprofiles.load(runtime.config)
        chat_protocols = netprofiles.chat_protocols()

        # Accounts that may own network configs: only ones that are actually logged in.
        #
        # The list is built from live state rather than from the instance directory listing, because the
        # question the operator is answering here is "which account's connections am I editing". Showing an
        # instance that exists but has never logged in offers a page with nothing behind it -- no messages
        # to route, no account to attribute them to.
        #
        # Two sources, both keyed by account number so that a pipe session and a headless instance running
        # the same account are one entry rather than two:
        #
        #   pipe sessions     each has its own process and port; the account is whatever login-boot.json
        #                     reports for that session
        #   headless instances  the manager's own login state
        #
        # An account with no login is simply absent. An empty list is a valid state, not an error.
        accounts: list[dict[str, Any]] = []
        seen_accounts: set[str] = set()

        try:
            # Pipe sessions: the same source the accounts page reads.
            #
            # `session_rows()` is the state the three-second poll last observed -- no probing here, so rendering
            # this page cannot stall on a dead session the way one HTTP round trip per session would. It used to
            # call `manager.live_sessions()`, which probes `login.info` for every session on every render: slow,
            # and a session whose DLL was busy enough to miss the timeout silently dropped out of the column.
            for row in runtime.session_rows():
                info = row.get("account") or {}
                uin = str(info.get("uin") or "").strip()
                session_id = str(row.get("tmp_id") or "")
                if not uin or uin in seen_accounts:
                    continue
                seen_accounts.add(uin)
                accounts.append({
                    "id": uin,
                    "qq": uin,
                    "label": f"{uin} · pipe {session_id}" if session_id else uin,
                    "kind": "pipe",
                    "session": session_id,
                    "logged_in": bool(row.get("logged_in")),
                    "running": bool(row.get("running")),
                    "nickname": str(info.get("nickname") or ""),
                    "phase": str(row.get("phase") or ""),
                })

            # Headless instances: their login state lives in the runtime's own login registry, keyed by instance
            # id. `instances()` only knows what `instance.json` says, so asking it for a login state always
            # answered "not logged in" -- that is the half that was missing, not a stray condition.
            for instance in _headless().instances():
                instance_id = str(instance.get("id") or "")
                state = runtime._logins.get(instance_id)
                uin = str((state.uin if state else "") or instance.get("qq") or "").strip()
                logged_in = bool(getattr(state, "logged_in", False))
                if not uin or uin == "0" or not logged_in or uin in seen_accounts:
                    continue
                seen_accounts.add(uin)
                accounts.append({
                    "id": uin,
                    "qq": uin,
                    "label": f"{uin} · headless {instance_id}",
                    "kind": "headless",
                    "session": instance_id,
                    "logged_in": True,
                    "running": True,
                    "nickname": str(getattr(state, "nickname", "") or ""),
                    "phase": "",
                })
        except Exception:  # noqa: BLE001 - the page must render even if state cannot be read
            accounts = []

        # An account with profiles but no live login still belongs in the column. Without it, the only way to
        # reach that account's settings was to be signed in as it -- so a misconfigured profile could become
        # unreachable precisely when it needed fixing.
        try:
            for owner in netprofiles.accounts_in_store(runtime.config):
                if owner in seen_accounts:
                    continue
                seen_accounts.add(owner)
                accounts.append({
                    "id": owner,
                    "qq": owner,
                    "label": f"{owner} · 已配置",
                    "kind": "",
                    "session": "",
                    "logged_in": False,
                    "running": False,
                    "nickname": "",
                })
        except Exception:  # noqa: BLE001 - a broken store must not blank the page
            pass

        accounts.sort(key=lambda item: (not item["logged_in"], item["kind"] != "pipe", item["id"]))

        selected = str(account or "").strip()
        known = {entry["id"] for entry in accounts}
        if selected and selected not in known:
            selected = ""
        if not selected and accounts:
            # Prefer an account that actually has profiles. Defaulting to "the first logged-in account" landed
            # on one with nothing configured whenever a second account was also signed in, and the page then
            # looked empty even though the operator's configuration was right there under another account.
            configured = {netprofiles.owner_of(item) for item in profiles}
            selected = next((entry["id"] for entry in accounts if entry["id"] in configured), accounts[0]["id"])

        # Only this account's profiles. The `instance` field is the ownership key and already existed;
        # the change is that the page now filters on it instead of showing everything at once.
        scoped = [item for item in profiles if netprofiles.owner_of(item) == selected]
        groups = {kind: [item for item in scoped if item.get("kind") == kind]
                  for kind in netprofiles.KINDS}

        # Which tab to open. Explicit choice wins; otherwise the first kind this account actually configures, so
        # landing on an empty HTTP tab when the account only has WebSocket clients no longer looks like "my
        # configuration is gone".
        if kind not in netprofiles.KINDS:
            kind = next((name for name in netprofiles.KINDS if groups[name]), netprofiles.KINDS[0])

        return {
            "page": "network",
            "info": runtime.info(),
            "config": runtime.config,
            "onebot": runtime.config.onebot,
            "webui": runtime.config.webui,
            "applied": runtime.last_network_apply,
            "profiles": scoped,
            "all_profiles": profiles,
            "groups": groups,
            "accounts": accounts,
            "selected_account": selected,
            # Which kind tab was open. Every action reloads the page, and without this the reload landed on the
            # first tab -- editing a WS client on the second account put the operator back on HTTP for account
            # one, which reads as "my change was thrown away".
            "selected_kind": kind,
            # 下拉候选项来自 protocols/enabled_protocol.json；maxStore 是存储协议，
            # 不在其中，因此永远不会被选成聊天协议。
            "chat_protocols": chat_protocols,
            "protocol_names": {item["id"]: item["name"] for item in chat_protocols},
            # 账号下拉的候选实例（C0、C1 …），与账户管理页同源。
            "instance_choices": netprofiles.instance_choices(runtime.config),
        }

    @app.get("/network", response_class=HTMLResponse)
    async def network_page(request: Request, saved: int = 0, account: str = "", kind: str = "") -> HTMLResponse:
        denied = guard(request)
        if denied:
            return denied
        return templates.TemplateResponse(
            request, "network.html",
            {**_network_view(runtime, account, kind), "saved": bool(saved), "errors": []}
        )

    @app.post("/api/network/profiles")
    async def network_profiles_api(request: Request) -> JSONResponse:
        denied = guard(request)
        if denied:
            return denied
        try:
            payload = await request.json()
        except json.JSONDecodeError:
            return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"ok": False, "error": "expected object"}, status_code=400)
        profiles = netprofiles.load(runtime.config)
        action = str(payload.get("action") or "upsert")
        try:
            if action == "delete":
                netprofiles.remove(profiles, str(payload.get("id") or ""))
            elif action == "toggle":
                target = next((item for item in profiles if item.get("id") == payload.get("id")), None)
                if target is None:
                    raise KeyError("profile not found")
                target["enabled"] = bool(payload.get("enabled"))
            else:
                if not str(payload.get("instance") or payload.get("account") or "").strip():
                    # A profile created without an explicit owner belongs to the account whose page it came from.
                    # Leaving it unset put it on "C0" -- an owner nobody has -- and an ownerless profile is
                    # active for whichever account happens to be serving, which is the leak this page must not
                    # have. Only an unambiguous single logged-in account is used as the default.
                    active = runtime._active_accounts()
                    if len(active) == 1:
                        payload["instance"] = active[0]
                # Passed the config so the owner is validated: without it an unknown owner was stored verbatim
                # and the profile then matched no account at all.
                netprofiles.upsert(profiles, payload, runtime.config)
        except (ValueError, KeyError) as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

        # Validate *before* anything is written.
        #
        # This used to save the profiles first and validate afterwards, so a save the page then
        # rejected ("HTTP / WS / SSE 路径不能重复") had already been persisted: the dialog said it
        # failed while a page refresh showed the new profile. Worse, `apply_to_config` mutates
        # `config.onebot` as it goes, so a rejected save also left the running config changed.
        # Both are fixed by applying to a copy, validating that, and committing only if it passes.
        previous_onebot = copy.deepcopy(runtime.config.onebot)
        active_profiles = runtime._network_profiles_for(runtime._active_accounts(), profiles)
        data, warnings = netprofiles.apply_to_config(runtime.config, active_profiles)
        data["clear_onebot_token"] = False
        errors = _apply_network(runtime, data)
        if errors:
            # Put the in-memory config back: nothing of this request should survive a rejection.
            # `OneBotConfig` is a slots dataclass, so this copies field by field rather than via `vars`.
            for slot in previous_onebot.__slots__:
                setattr(runtime.config.onebot, slot, getattr(previous_onebot, slot))
            return JSONResponse({"ok": False, "error": "; ".join(errors)}, status_code=400)

        netprofiles.save(runtime.config, profiles)
        _write_setting(runtime, {"onebot": {key: value for key, value in data.items() if key != "clear_onebot_token"}})
        # Hot reload still happens immediately -- but through the account gate, so a profile for an account
        # that is not logged in saves without opening a listener for it. `force` is what makes this a reload:
        # without it a save that does not change the logged-in account set would be a no-op.
        #
        # `settle=False`: the login delay exists so a session can finish coming up before a listener binds to it.
        # Here the operator has clicked save on an account that is already serving, so delaying the effect would
        # only look like the save did nothing.
        applied = await runtime.sync_account_network(force=True, reason="settings saved", settle=False)
        return JSONResponse({"ok": True, "profiles": profiles, "warnings": warnings,
                             "applied": applied})

    @app.post("/network", response_class=HTMLResponse)
    async def save_network(request: Request) -> Response:
        denied = guard(request)
        if denied:
            return denied
        form = await request.form()
        data: dict[str, Any] = {key: form.get(key) for key in form.keys()}
        data["enable_http"] = form.get("enable_http") == "on"
        data["enable_ws"] = form.get("enable_ws") == "on"
        data["enable_sse"] = form.get("enable_sse") == "on"
        data["clear_onebot_token"] = form.get("clear_onebot_token") == "on"
        data["clear_webui_token"] = form.get("clear_webui_token") == "on"
        errors = _apply_network(runtime, data)
        if errors:
            return templates.TemplateResponse(
                request,
                "network.html",
                {**_network_view(runtime), "saved": False, "errors": errors},
                status_code=400,
            )
        applied = await runtime.apply_network()
        runtime.last_network_apply = applied
        return RedirectResponse("/network?saved=1", status_code=303)

    @app.post("/api/headless/event")
    async def headless_event(request: Request) -> JSONResponse:
        client = request.client.host if request.client else ""
        if client not in ("127.0.0.1", "::1", "localhost"):
            return JSONResponse({"ok": False, "error": "loopback only"}, status_code=403)
        push = getattr(runtime.transport, "push", None)
        if push is None:
            return JSONResponse({"ok": False, "error": "headless transport is not active"}, status_code=409)
        # 允许多个实例上报：网络配置可以限定"只处理某个实例的事件"，那要求
        # 该实例的事件真能进来。安全性由两点保证——回环地址 + 该实例自己的
        # bridgeToken（每个实例启动时随机生成，互不相同）。
        instance = str(request.headers.get("X-QSICK-Instance") or "").strip()
        if not re.fullmatch(r"C(?:0|[1-9]\d*)", instance):
            return JSONResponse({"ok": False, "error": "unsupported host instance"}, status_code=409)
        try:
            host_config = json.loads((Path(runtime.config.headless_root) / instance / "config.json").read_text(encoding="utf-8"))
            token = str(host_config.get("bridgeToken") or "")
        except (OSError, json.JSONDecodeError):
            token = ""
        if not token or not secrets.compare_digest(request.headers.get("Authorization", ""), "Bearer " + token):
            return JSONResponse({"ok": False, "error": "bridge authorization required"}, status_code=401)
        try:
            payload = await request.json()
        except json.JSONDecodeError:
            return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"ok": False, "error": "expected object"}, status_code=400)
        try:
            push({**payload, "instance": instance})
        except (ValueError, TypeError) as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return JSONResponse({"ok": True})

    @app.get("/api/headless/capabilities")
    async def headless_capabilities(request: Request) -> JSONResponse:
        denied = guard(request)
        if denied:
            return denied
        if runtime.transport.name != "headless":
            return JSONResponse({"ok": False, "error": "headless transport is not active"}, status_code=409)
        result = await runtime.transport.send("runtime.capabilities", {}, timeout=5)
        return JSONResponse({"ok": result.ok, "data": result.data, "error": result.message}, status_code=200 if result.ok else 503)

    @app.get("/api/network")
    async def network_api(request: Request) -> JSONResponse:
        denied = guard(request)
        if denied:
            return denied
        onebot = runtime.config.onebot
        webui = runtime.config.webui
        return JSONResponse(
            {
                "transport": runtime.config.transport.mode,
                "onebot": {
                    "host": onebot.host,
                    "port": onebot.port,
                    "enable_http": onebot.enable_http,
                    "enable_ws": onebot.enable_ws,
                    "enable_sse": onebot.enable_sse,
                    "http_path": onebot.http_path,
                    "ws_path": onebot.ws_path,
                    "sse_path": onebot.sse_path,
                    "access_token": "***" if onebot.access_token else "",
                    "reverse_ws_urls": onebot.reverse_ws_urls,
                    "reverse_reconnect_seconds": onebot.reverse_reconnect_seconds,
                },
                "webui": {
                    "host": webui.host,
                    "port": webui.port,
                    "access_token": "***" if webui.access_token else "",
                },
                "reverse_ws": runtime.info()["reverse_ws"],
            }
        )

    @app.post("/api/network")
    async def network_api_save(request: Request) -> JSONResponse:
        denied = guard(request)
        if denied:
            return denied
        try:
            data = await request.json()
        except json.JSONDecodeError:
            return JSONResponse({"ok": False, "errors": ["invalid JSON"]}, status_code=400)
        if not isinstance(data, dict):
            return JSONResponse({"ok": False, "errors": ["expected object"]}, status_code=400)
        errors = _apply_network(runtime, data)
        if errors:
            return JSONResponse({"ok": False, "errors": errors}, status_code=400)
        applied = await runtime.apply_network()
        return JSONResponse({"ok": True, "applied": applied, "webui_port_restart_required": True})

    def _fmt_time(value: Any) -> str:
        """Epoch seconds as a UTC+8 wall clock.

        Raw epoch was shown before, which is unreadable at a glance and ambiguous about which zone it is in. The
        operator is in UTC+8, and the messages arrive from QQ with epoch seconds, so the conversion belongs here
        rather than in the reader's head.
        """
        try:
            moment = datetime.fromtimestamp(int(value), tz=timezone(timedelta(hours=8)))
        except (TypeError, ValueError, OSError, OverflowError):
            return str(value or "")
        return moment.strftime("%Y-%m-%d %H:%M:%S")

    #: Element types whose content can be fetched on demand. `face` is QQ's sticker element.
    # `face` is deliberately absent: it is QQ's built-in emoji, with no media behind it, so a button for it
    # could only answer "nothing to download". `mface` is an animated sticker and does carry a file id.
    _MEDIA_ELEMENT_TYPES = ("image", "file", "video", "mface", "record")

    async def _message_page(request: Request, q: str, page: int, limit: int, account: str = "") -> dict[str, Any]:
        """The cache, straight from the store the cache writes to.

        It used to read `runtime.store` -- the default instance's database -- and later every instance store. Both
        tie the view to instance state: with no session running there are no instance contexts, so the page showed
        a fallback store holding leftover mock rows. The cache records forwarded messages itself now, so this reads
        that one database and cannot be affected by whether anything is logged in.
        """
        page = max(1, page)
        wanted = str(account or "").strip()
        items_all = []
        try:
            # Unfiltered: the search runs below over the whole record, because the operator asked for one box
            # that matches any of its fields rather than five separate filters.
            items_all = list(runtime.cache_store.search("", 0, 100_000))
        except Exception:  # noqa: BLE001 - an unreadable cache must not blank the page
            items_all = []

        if wanted:
            items_all = [item for item in items_all if str(getattr(item, "account", "") or "") == wanted]

        needle = q.strip().lower()
        if needle:
            def haystack(item: Any) -> str:
                sender = str(getattr(item, "sender_name", "") or getattr(item, "sender_uin", "") or "")
                parts = [
                    str(item.message_id),
                    str(getattr(item, "account", "") or ""),
                    str(getattr(item, "chat_id", "") or ""),
                    "群" if getattr(item, "is_group", False) else "私",
                    str(getattr(item, "sender_uin", "") or ""),
                    sender,
                    item.text,
                ]
                return " ".join(parts).lower()

            items_all = [item for item in items_all if needle in haystack(item)]

        ordered = sorted(items_all, key=lambda item: (item.time, item.message_id), reverse=True)
        total = len(ordered)
        pages = max(1, math.ceil(total / limit))
        page = min(page, pages)
        items = ordered[(page - 1) * limit: (page - 1) * limit + limit]

        # What can be downloaded, as the URL the content actually lives at.
        #
        # The button used to point at `/messages/media/<id>/<n>` -- a route on this machine, which is not a download
        # link anyone else can use. The real address is minted here instead, and it has to be minted *now*: the
        # token in it expires, so storing it with the record would hand out dead links.
        attachments: dict[Any, list[dict[str, Any]]] = {}
        for item in items:
            links = []
            for index, the_element in enumerate(getattr(item, "elements", []) or []):
                kind = str(getattr(the_element, "type", "") or "")
                if kind not in _MEDIA_ELEMENT_TYPES:
                    continue
                data = getattr(the_element, "data", {}) or {}
                name = str(data.get("file_name") or data.get("name") or "")
                # The route does the fetching, so nothing has to be resolved while the page renders. A record whose
                # token cannot be had at this moment therefore still gets a button, and the click reports the real
                # reason instead of the link having quietly disappeared.
                if kind == "file" and not item.is_group:
                    continue
                links.append({
                    "href": f"/messages/media/{item.message_id}/{index}",
                    "label": f"下载{'图片' if kind == 'image' else '文件' if kind == 'file' else kind}",
                    "title": name or kind,
                })
            if links:
                attachments[item.message_id] = links
        return {
            "page": "messages",
            "info": runtime.info(),
            "messages": items,
            "q": q,
            "account": wanted,
            "accounts": sorted({str(getattr(item, "account", "") or "") for item in ordered} - {""}),
            "page_no": page,
            "pages": pages,
            "total": total,
            "limit": limit,
            "preview": _preview,
            "fmt_time": _fmt_time,
            "attachments": attachments,
        }

    def _headless() -> HeadlessManager:
        return HeadlessManager(runtime.config.headless_root)

    @app.get("/accounts/{instance_id}/login", response_class=HTMLResponse)
    async def accounts_login(instance_id: str, request: Request, embedded: int = 0) -> HTMLResponse:
        denied = guard(request)
        if denied:
            return denied
        account = next((item for item in _headless().instances() if item["id"] == instance_id), None)
        if account is None:
            return HTMLResponse(f"<h1>404</h1><p>找不到实例 {instance_id}</p>", status_code=404)
        return templates.TemplateResponse(
            request, "login_qr.html", {"page": "accounts", "account": account, "embedded": bool(embedded)}
        )

    def _headless_only() -> list[dict[str, Any]]:
        """The instance table's rows.

        One function for both the initial render and the 5-second refresh. They previously disagreed:
        the poll filtered pipe out, the first render did not, so a Pipe row appeared on load and vanished
        on the first refresh. Anything that decides what belongs in this table belongs here.

        `instances()` already excludes anything that is not a `C<n>` directory holding a `wrapper.node`,
        so no kind filter is needed on top -- only headless instances exist in that list by construction.
        """
        return _headless().instances()

    @app.get("/accounts", response_class=HTMLResponse)
    async def accounts_page(request: Request) -> HTMLResponse:
        denied = guard(request)
        if denied:
            return denied
        return templates.TemplateResponse(
            request,
            "accounts.html",
            {"page": "accounts", "info": runtime.info(), "accounts": _headless_only(),
             "relogin_delays": _relogin_delays(), "config": runtime.config},
        )

    @app.get("/api/accounts")
    async def accounts_api(request: Request) -> JSONResponse:
        denied = guard(request)
        if denied:
            return denied
        # Headless instances only, from the same function the page uses. Pipe sessions have their own
        # endpoint and their own section: they are started and closed per session, which is a different
        # lifecycle from "delete this instance directory".
        return JSONResponse({"accounts": _headless_only()})

    @app.post("/api/accounts")
    async def accounts_create(request: Request) -> JSONResponse:
        denied = guard(request)
        if denied:
            return denied
        # 创建参数可选：老前端（以及脚本）不带 body 时沿用原来的"下一个 C<n>"。
        try:
            payload = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        name = str(payload.get("name") or "").strip()
        kind = str(payload.get("kind") or "headless").strip().lower()
        if kind not in ("headless", "pipe"):
            return JSONResponse({"ok": False, "error": "kind must be headless or pipe"},
                                status_code=400)
        try:
            # pipe 实例要铺一份完整客户端（模板用硬链接），几秒钟起步：别放在
            # 事件循环上，否则这段时间整个 WebUI 都不响应。
            account = await asyncio.to_thread(_headless().create, name, kind)
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        except Exception as exc:  # noqa: BLE001 - 模板/磁盘问题都要如实回报
            return JSONResponse({"ok": False, "error": f"创建失败：{exc}"}, status_code=500)
        return JSONResponse({"ok": True, "account": account})

    @app.get("/partials/accounts", response_class=HTMLResponse)
    async def partial_accounts(request: Request) -> HTMLResponse:
        denied = guard(request)
        if denied:
            return denied
        return templates.TemplateResponse(
            request, "partials/accounts_table.html",
                {"page": "accounts",
                 # Same source as the initial render, so a refresh cannot change which rows exist.
                 "accounts": _headless_only(),
                 "relogin_delays": _relogin_delays()},
        )

    @app.get("/api/accounts/{instance_id}/qrcode")
    async def accounts_qrcode(instance_id: str, request: Request) -> JSONResponse:
        denied = guard(request)
        if denied:
            return denied
        try:
            instance = next((item for item in _headless().instances() if item["id"] == instance_id), None)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
        if instance is None:
            return JSONResponse({"ok": False, "error": f"找不到实例 {instance_id!r}"}, status_code=404)
        return JSONResponse({"ok": True, "account": instance})

    @app.post("/api/accounts/{instance_id}/refresh")
    async def accounts_refresh(instance_id: str, request: Request) -> JSONResponse:
        denied = guard(request)
        if denied:
            return denied
        try:
            account = await asyncio.to_thread(_headless().refresh, instance_id)
            return JSONResponse({"ok": True, "account": account})
        except KeyError:
            return JSONResponse({"ok": False, "error": f"找不到实例 {instance_id!r}"}, status_code=404)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    @app.post("/api/accounts/{instance_id}/port")
    async def accounts_set_port(instance_id: str, request: Request) -> JSONResponse:
        """Set or clear a session's DLL port.

        The port is where the injected DLL serves its action API, so a wrong value makes the session
        unreachable rather than merely misconfigured. Validation is therefore strict about the range and
        about collisions: two sessions on one port means the second cannot bind, and the failure would
        only show up later as "this session does not respond".

        An empty or zero value means "go back to automatic": the override is removed and the next start
        allocates from 7540 again.
        """
        denied = guard(request)
        if denied:
            return denied
        try:
            payload = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"ok": False, "error": "body must be an object"}, status_code=400)

        raw = payload.get("port")
        try:
            port = int(raw or 0)
        except (TypeError, ValueError):
            return JSONResponse({"ok": False, "error": "port must be a number"}, status_code=400)

        from .. import headless as headless_mod

        if port:
            if not (headless_mod.PIPE_PORT_BASE <= port <= headless_mod.PIPE_PORT_MAX):
                return JSONResponse(
                    {"ok": False,
                     "error": f"端口必须落在 {headless_mod.PIPE_PORT_BASE}-"
                              f"{headless_mod.PIPE_PORT_MAX} 之间"},
                    status_code=400)
            # Refuse a port another session already claims: the collision is silent at bind time and
            # only surfaces as an unreachable session.
            for other in _headless().instances():
                if other["id"] == instance_id:
                    continue
                if int(other.get("port") or 0) == port and other.get("running"):
                    return JSONResponse(
                        {"ok": False, "error": f"端口 {port} 已被运行中的 session "
                                               f"「{other.get('name') or other['id']}」占用"},
                        status_code=409)

        try:
            overrides = headless_mod.read_port_override()
            if port:
                overrides[instance_id] = port
            else:
                overrides.pop(instance_id, None)
            headless_mod.write_port_override(overrides)
            account = await asyncio.to_thread(_headless().set_port, instance_id, port)
            return JSONResponse({"ok": True, "account": account, "port": port})
        except KeyError:
            return JSONResponse({"ok": False, "error": f"找不到实例 {instance_id!r}"},
                                status_code=404)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    @app.get("/api/sessions")
    async def sessions_api(request: Request) -> JSONResponse:
        """The pipe sessions and whether each is actually running.

        The list comes from `Pipe/dll/`: one directory per session, holding that session's port. That is
        the durable half -- a session directory outlives its process, so sessions survive a QSICK restart.

        Liveness is not read from any file. Each session's status is asked of its own DLL, over
        `GET /health` on the port the session was given:

            reply with status "ok"   -> running
            connection refused       -> not started
            timeout / other error    -> not started

        That is the authoritative answer because the DLL answers only while its process is alive and bound.
        A status file would report whatever was true when it was written -- a killed session keeps its last
        heartbeat, which is exactly how a dead session used to keep showing as running.

        Probes run concurrently: sequentially, one unreachable session would add its full timeout to the
        list before the next was even tried, and a page polling every 5 s cannot afford that.
        """
        denied = guard(request)
        if denied:
            return denied
        from .. import headless as headless_mod

        try:
            sessions = await asyncio.to_thread(headless_mod.read_session_dirs)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

        if sessions:
            # Read the state the three-second poll left behind; do not probe here.
            #
            # The poll owns the cadence, and the retry ladder lives in it: one probe every three seconds, then
            # one per second for the next two while a session goes unanswered, then the session is judged dead
            # and stops being probed until the operator starts it again. Probing from this endpoint would
            # bypass all of that -- a page refresh would revive a stopped session and the answers would race
            # the poll's own.
            live = {str(row.get("tmp_id") or ""): row for row in runtime.session_rows()}
            for item in sessions:
                row = live.get(str(item.get("tmp_id") or ""))
                if row is not None:
                    item.update({key: row[key] for key in
                                 ("running", "logged_in", "closed", "detail", "account", "probed",
                                  "stage", "ready", "phase")
                                 if key in row})
                else:
                    # Never polled yet: say so instead of implying 已停止.
                    item.setdefault("running", False)
                    item["logged_in"] = False
                    item["probed"] = False
                # A session whose answer came from `nowport` but whose requested port differs has an edit
                # waiting for a restart. Surfaced so the row can say so instead of looking settled.
                item["port_changed"] = bool(
                    item["running"] and item.get("port") and item.get("nowport")
                    and int(item["port"]) != int(item["nowport"]))
        return JSONResponse({"ok": True, "sessions": sessions})

    @app.post("/api/sessions")
    async def sessions_start(request: Request) -> JSONResponse:
        """Start one more pipe session.

        This does *not* create an instance. Only one client copy exists under `Pipe/`, and a session is
        that client being launched again -- a new process with its own temporary id and port. Copying the
        client per session was the earlier behaviour and it cost 1.3 GB each time, which is neither what
        "repeat-launch QQ.exe" means nor something the disk can absorb.

        The launch path is the existing pipe start: a fresh 4-character id, a DLL copy under `dll/<id>/`, and
        a port allocated from 7540.
        """
        denied = guard(request)
        if denied:
            return denied
        from .. import headless as headless_mod

        manager = _headless()
        try:
            # The port comes from the hot-reloaded override map or the next free one.
            port = headless_mod.read_port_override().get(manager.pipe_instance_id(), 0)
            # The configured port, not the literal. A session told to report to a port the WebUI does not
            # listen on connects and then delivers nothing.
            result = await asyncio.to_thread(
                manager.start_session, port, runtime.config.webui.port)
            # A brand-new session has never been polled; make sure it is, whatever the previous run left in
            # the "given up" set for an id that happens to collide.
            if isinstance(result, dict) and result.get("id"):
                runtime.resume_session_polling(str(result.get("id")))
            return JSONResponse({"ok": True, "session": result})
        except FileNotFoundError:
            return JSONResponse(
                {"ok": False, "error": "还没有 Pipe 客户端。先在账户管理里创建一个 pipe 实例。"},
                status_code=404)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    @app.post("/api/sessions/{tmp_id}/port")
    async def sessions_set_port(tmp_id: str, request: Request) -> JSONResponse:
        """Change one session's DLL port, in that session's own config.

        The port is written to `dll/<tmp_id>/qsick_pipe.json` and takes effect the next time this session is
        started.

        It cannot take effect immediately, and that is a property of the DLL rather than of this code: the
        injected side exposes `QsickStartHttp(port)` guarded by an `InterlockedCompareExchange` one-shot, so
        the listener is created once per process and there is no stop or rebind. A running session therefore
        keeps the port it bound. The response says so, and `needs_restart` lets the UI state it instead of
        leaving the operator to discover it.

        Everything else about the edit is live: the new number is on disk at once, the list shows it on the
        next poll, and an invalid or conflicting value is refused here rather than at the next launch.
        """
        denied = guard(request)
        if denied:
            return denied
        try:
            payload = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"ok": False, "error": "body must be an object"}, status_code=400)
        try:
            port = int(payload.get("port") or 0)
        except (TypeError, ValueError):
            return JSONResponse({"ok": False, "error": "port must be a number"}, status_code=400)

        from .. import headless as headless_mod

        if port and not (headless_mod.PIPE_PORT_BASE <= port <= headless_mod.PIPE_PORT_MAX):
            return JSONResponse(
                {"ok": False,
                 "error": f"端口必须落在 {headless_mod.PIPE_PORT_BASE}-"
                          f"{headless_mod.PIPE_PORT_MAX} 之间"},
                status_code=400)
        # Refuse a port another *running* session actually answers on. Checking liveness rather than the
        # recorded number, because a stopped session's port is free to take.
        #
        # `login.info` is the liveness test: it is a plain round trip to the other session's DLL, and a
        # refused connection means that session is closed, so the number is free.
        if port:
            for other in headless_mod.read_session_dirs():
                if other["tmp_id"] == tmp_id or int(other.get("port") or 0) != port:
                    continue
                if headless_mod.probe_session_login(port).get("running"):
                    return JSONResponse(
                        {"ok": False,
                         "error": f"端口 {port} 已被运行中的 session {other['tmp_id']} 占用"},
                        status_code=409)

        directory = headless_mod.session_dll_dir() / tmp_id
        config_path = directory / headless_mod.PIPE_DLL_CONFIG
        if not config_path.is_file():
            return JSONResponse({"ok": False, "error": f"找不到 session {tmp_id!r} 的配置"},
                                status_code=404)

        # Only `port` changes here. `nowport` deliberately does not: it records what the process bound, and
        # the process is still listening on the old number. Leaving it alone is what lets a caller tell
        # "edited but not applied" from "applied", and what keeps probes pointed at a live socket.
        old_port, old_nowport = headless_mod.session_ports(headless_mod.read_session_config(tmp_id))
        effective = port or old_port
        headless_mod.write_session_ports(tmp_id, port=effective)
        was_running = bool(
            (old_nowport or old_port)
            and headless_mod.probe_session_login(int(old_nowport or old_port)).get("running"))
        return JSONResponse({
            "ok": True,
            "tmp_id": tmp_id,
            "port": effective,
            "nowport": old_nowport,
            # A running session keeps its old port until relaunched; saying so is the whole point.
            "needs_restart": was_running and effective != (old_nowport or old_port),
        })

    @app.post("/api/sessions/autostart")
    async def sessions_autostart(request: Request) -> JSONResponse:
        """Read or set which pipe session starts with QSICK.

        Two values, because one is not enough: a switch alone cannot say *which* session, and a choice alone
        cannot say whether to act on it. Both live in the config file so they survive a restart -- that is the
        whole point of an auto-start setting.

        An empty session id with the switch on is accepted and simply starts nothing. Refusing it would force
        the operator to pick a session before they can turn the switch off, which is backwards.
        """
        denied = guard(request)
        if denied:
            return denied
        try:
            payload = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        config = runtime.config
        if "enabled" in payload:
            config.pipe_auto_start = bool(payload.get("enabled"))
        if "session" in payload:
            config.pipe_auto_start_session = str(payload.get("session") or "").strip()
        _write_setting(runtime, {
            "pipe_auto_start": config.pipe_auto_start,
            "pipe_auto_start_session": config.pipe_auto_start_session,
        })
        return JSONResponse({
            "ok": True,
            "enabled": config.pipe_auto_start,
            "session": config.pipe_auto_start_session,
        })

    @app.post("/api/sessions/{tmp_id}/start")
    async def sessions_start_one(tmp_id: str, request: Request) -> JSONResponse:
        """Relaunch one existing session on its requested port.

        Starting is the point where `nowport` is aligned to `port`: the process is launched on the number the
        operator asked for, and `nowport` records that it is now bound. Until then the two may differ, which
        is how a pending edit is represented.

        A port that the launch cannot bind fails loudly rather than being silently swapped for another: the
        operator picked that number, and moving it would undo their choice without saying so.
        """
        denied = guard(request)
        if denied:
            return denied
        from .. import headless as headless_mod
        from ..transport import session_link

        directory = headless_mod.session_dll_dir() / tmp_id
        if not directory.is_dir():
            return JSONResponse({"ok": False, "error": f"找不到 session {tmp_id!r}"},
                                status_code=404)
        port, _ = headless_mod.session_ports(headless_mod.read_session_config(tmp_id))
        try:
            # `tmp_id` is passed so the launch reuses this session's directory. Without it a fresh id is
            # generated, which is how pressing 启动 used to add a second row for the same process.
            result = await asyncio.to_thread(
                _headless().start_session, port, runtime.config.webui.port, tmp_id)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
        headless_mod.write_session_ports(tmp_id, nowport=int(port or 0))
        # A session that was judged dead is not being polled. Pressing 启动 is what brings it back, which is the
        # operator's rule and also the only moment its state can meaningfully change.
        runtime.resume_session_polling(tmp_id)
        return JSONResponse({"ok": True, "session": result})

    @app.post("/api/sessions/{tmp_id}/close")
    async def sessions_close(tmp_id: str, request: Request) -> JSONResponse:
        """Close one session: kill its process, keep its directory.

        Closing is about the process. The directory holds the port and the DLL copy, so deleting it would
        lose the operator's port assignment and make "close then start" impossible -- they would be
        configuring a new session each time. Use `/delete` to remove the session entirely.
        """
        denied = guard(request)
        if denied:
            return denied
        from .. import headless as headless_mod

        directory = headless_mod.session_dll_dir() / tmp_id
        if not directory.is_dir():
            return JSONResponse({"ok": False, "error": f"找不到 session {tmp_id!r}"},
                                status_code=404)
        # Learn the account before the process dies: after the kill nothing can answer for it, and the
        # account is what owns a network config.
        account = ""
        config = {}
        port = 0
        try:
            config = await asyncio.to_thread(headless_mod.read_session_config, tmp_id)
            # `session_ports` knows both config shapes: `nowport` is nested inside `session` in current
            # launches and top-level in older ones. Reading only one shape gives 0 and silently skips every
            # step below that needs the port.
            requested, bound = headless_mod.session_ports(config or {})
            port = int(bound or requested or 0)
            if port:
                probe = await asyncio.to_thread(headless_mod.probe_session_login, port)
                if probe.get("logged_in"):
                    account = str(probe.get("uin") or "")
        except Exception as exc:  # noqa: BLE001 - a missing account only costs the immediate close
            log.debug("could not read the session account before closing", error=str(exc))

        # Close, in the operator's order: end the session first, then wait for it to say it is going.
        #
        # `/quit` is the normal kill on Windows: there is no SIGTERM, so the DLL is asked and announces itself
        # over the state link (`closing`) before exiting. A hard `TerminateProcess` first would give it no
        # chance to say anything, which is exactly what made the disconnect packet unobservable.
        graceful = False
        announced = False
        try:
            token = str((config or {}).get("ws_token") or "")
            if port:
                graceful = await asyncio.to_thread(session_link.request_shutdown, port, token, 5.0)
        except Exception as exc:  # noqa: BLE001 - fall through to the kill
            log.debug("graceful shutdown request failed", error=str(exc))

        # Either the state link reports the disconnect, or ten seconds pass and we insist.
        deadline = 10.0
        waited = 0.0
        while waited < deadline:
            link = runtime._links.get(tmp_id)
            if link is not None and link.state.closing:
                announced = True
                break
            if port:
                still_up = await asyncio.to_thread(headless_mod.probe_session_login, port, 0.5)
                if not still_up.get("running"):
                    # The port stopped answering: whatever it managed to send, it is gone.
                    announced = True
                    break
            await asyncio.sleep(0.5)
            waited += 0.5

        forced = False
        try:
            killed = await asyncio.to_thread(headless_mod.kill_session_process,
                                             headless_mod.ROOT_NAME, tmp_id)
            # Ten seconds without a goodbye means it is still running: terminate it for real.
            if not announced:
                forced = True
                await asyncio.to_thread(headless_mod.kill_session_process,
                                        headless_mod.ROOT_NAME, tmp_id)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

        # The account is gone, so its network config must not stay open.
        closed_network = runtime.mark_account_offline(account, reason="session closed") if account else False
        # Closing is final until 启动: the link stops, and the row says 已关闭.
        runtime.stop_session_polling(tmp_id, detail="已关闭")
        # Only now is the port settled: `nowport` is what a running session bound, and a stopped one binds
        # nothing -- aligning it with `port` is what makes the row read "stopped on this port" instead of
        # showing a pending restart. Deliberately after the disconnect, never before: clearing it early is
        # what made the close path lose the port it needed to ask the session to shut down.
        try:
            headless_mod.write_session_ports(tmp_id, nowport=int(port or 0))
        except Exception as exc:  # noqa: BLE001 - a stale row is better than a failed close
            log.debug("could not align the session port", error=str(exc))

        return JSONResponse({"ok": True, "closed": tmp_id, "killed": killed,
                             "graceful": graceful, "forced": forced,
                             "account": account, "network_closed": closed_network})

    @app.post("/api/sessions/{tmp_id}/delete")
    async def sessions_delete(tmp_id: str, request: Request) -> JSONResponse:
        """Delete one session: kill its process and remove its directory.

        The row disappears from the list because the list is read from `dll/`. This is the destructive one
        -- the port assignment goes with it.

        Deleting a running session waits for the DLL to be released before removing anything, so a single
        click is enough. If the files are still held after that wait the request reports it as a conflict
        rather than a success, which is what stops the UI from showing a half-deleted row that only a second
        click clears.
        """
        denied = guard(request)
        if denied:
            return denied
        from .. import headless as headless_mod

        directory = headless_mod.session_dll_dir() / tmp_id
        if not directory.is_dir():
            return JSONResponse({"ok": False, "error": f"找不到 session {tmp_id!r}"},
                                status_code=404)
        try:
            deleted = await asyncio.to_thread(headless_mod.close_session,
                                              headless_mod.ROOT_NAME, tmp_id)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
        if not deleted:
            return JSONResponse(
                {"ok": False,
                 "error": "session 的 dll 仍被占用，目录没删掉。稍等几秒再点一次删除。"},
                status_code=409)
        return JSONResponse({"ok": True, "deleted": tmp_id})

    @app.post("/api/accounts/{instance_id}/auto-start")
    async def accounts_auto_start(instance_id: str, request: Request) -> JSONResponse:
        """开关单个实例的"随 QSICK 启动"。

        只写配置，不立刻启动或停止实例——开关管的是**下次**启动的行为。想马上生效
        请点"启动"。这样两者职责分明，也不会让一次误触把 QQ 拉起来。
        """
        denied = guard(request)
        if denied:
            return denied
        try:
            payload = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)
        if not isinstance(payload, dict) or not isinstance(payload.get("enabled"), bool):
            return JSONResponse({"ok": False, "error": "enabled must be a boolean"},
                                status_code=400)
        try:
            account = await asyncio.to_thread(_headless().set_auto_start,
                                              instance_id, payload["enabled"])
            return JSONResponse({"ok": True, "account": account})
        except KeyError:
            return JSONResponse({"ok": False, "error": f"找不到实例 {instance_id!r}"},
                                status_code=404)
        except Exception as exc:  # noqa: BLE001 - 交给界面显示
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    @app.post("/api/accounts/{instance_id}/auto-relogin")
    async def accounts_auto_relogin(instance_id: str, request: Request) -> JSONResponse:
        denied = guard(request)
        if denied:
            return denied
        try:
            payload = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)
        if not isinstance(payload, dict) or not isinstance(payload.get("enabled"), bool):
            return JSONResponse({"ok": False, "error": "enabled must be a boolean"}, status_code=400)
        delay = payload.get("delay")
        if delay is not None:
            if not isinstance(delay, int) or isinstance(delay, bool):
                return JSONResponse({"ok": False, "error": "delay 必须是整数秒"}, status_code=400)
            # 在路由层就挡掉非法档位，而不是只依赖 manager 里的校验：那个方法在
            # 测试里会被替换掉，而"任意数字"会让重登节拍失控，这是策略而不是
            # 实现细节，应该在入口处守住。
            from ..headless import RELOGIN_DELAY_CHOICES

            if delay not in RELOGIN_DELAY_CHOICES:
                allowed = "、".join(str(item) for item in RELOGIN_DELAY_CHOICES)
                return JSONResponse({"ok": False, "error": f"不支持的时延 {delay}；可选：{allowed}"},
                                    status_code=400)
        try:
            account = await asyncio.to_thread(_headless().set_auto_relogin,
                                              instance_id, payload["enabled"], delay)
            # 刚被踢的实例一旦打开开关，计时器要从现在开始算；关掉则清空，
            # 下次被踢重新等满整个档位。
            from ..backoff import KICK_CLOCK

            if payload["enabled"]:
                KICK_CLOCK.start(instance_id)
            else:
                KICK_CLOCK.clear(instance_id)
            return JSONResponse({"ok": True, "account": account})
        except KeyError:
            return JSONResponse({"ok": False, "error": f"找不到实例 {instance_id!r}"}, status_code=404)
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    @app.post("/api/accounts/{instance_id}/recover")
    async def accounts_recover(instance_id: str, request: Request) -> JSONResponse:
        denied = guard(request)
        if denied:
            return denied
        try:
            # 手动点重登 = 用户明确决定"再试一次"，先清掉退避账本；否则熔断之后
            # 这个按钮永远只会回一句"已停止自动重试"，等于坏了。
            from ..backoff import BOOK

            BOOK.reset(instance_id)
            result = await asyncio.to_thread(_headless().recover_kicked, instance_id, runtime.config.webui.port)
            return JSONResponse({"ok": True, **result})
        except KeyError:
            return JSONResponse({"ok": False, "error": f"找不到实例 {instance_id!r}"}, status_code=404)
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    @app.post("/api/accounts/{instance_id}/quick-login")
    async def accounts_quick_login(instance_id: str, request: Request) -> JSONResponse:
        denied = guard(request)
        if denied:
            return denied
        try:
            result = await asyncio.to_thread(_headless().quick_login, instance_id, runtime.config.webui.port)
            return JSONResponse({"ok": True, **result})
        except KeyError:
            return JSONResponse({"ok": False, "error": f"找不到实例 {instance_id!r}"}, status_code=404)
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        except RuntimeError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)

    @app.post("/api/accounts/{instance_id}/start")
    async def accounts_start(instance_id: str, request: Request) -> JSONResponse:
        denied = guard(request)
        if denied:
            return denied
        try:
            return JSONResponse({"ok": True, "account": _headless().start(instance_id, runtime.config.webui.port)})
        except KeyError:
            return JSONResponse({"ok": False, "error": f"找不到实例 {instance_id!r}"}, status_code=404)
        except Exception as exc:  # noqa: BLE001 - host startup failures are reported to the UI
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    @app.post("/api/accounts/{instance_id}/stop")
    async def accounts_stop(instance_id: str, request: Request) -> JSONResponse:
        denied = guard(request)
        if denied:
            return denied
        try:
            return JSONResponse({"ok": True, "account": _headless().stop(instance_id)})
        except KeyError:
            return JSONResponse({"ok": False, "error": f"找不到实例 {instance_id!r}"}, status_code=404)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    @app.delete("/api/accounts/{instance_id}")
    async def accounts_delete(instance_id: str, request: Request) -> JSONResponse:
        denied = guard(request)
        if denied:
            return denied
        try:
            return JSONResponse({"ok": _headless().delete(instance_id)})
        except KeyError:
            return JSONResponse({"ok": False, "error": f"找不到实例 {instance_id!r}"}, status_code=404)
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    # 聊天栏（/chat 页面与 /api/chat/* 的账户级接口）已移除。
    #
    # 下面这三个 group-only 端点是留给外部调用方的，不服务任何页面，因此跟着留下;
    # 它们原先借用 chat_api 的 _store_for，聊天栏删掉后这个函数搬到这里。
    def _store_for(target: "Runtime", account: Any) -> Any:
        """按账号找到它所属实例的数据库。

        请求里的 ``account`` 是 QQ 号；这里把它映射到实例，再取那个实例自己的
        maxStore 库。每个实例各有一个库，所以这一步不是优化而是正确性：
        拿错库就会看到另一个号的消息。

        找不到对应实例时退回 ``target.store``（主实例的库），保持旧行为而不是
        报错——升级前收到的消息都在那里。
        """
        instances = getattr(target, "instances", None)
        if instances is None:
            return target.store
        wanted = str(account or "").strip()
        if wanted:
            for name in instances.names():
                context = instances.get(name)
                if context is not None and str(context.account) == wanted:
                    return context.store
        return target.store

    @app.get("/api/chat/groups")
    async def chat_groups(request: Request, account: str = "") -> JSONResponse:
        denied = guard(request)
        if denied:
            return denied
        groups: list[dict[str, Any]] = []
        try:
            result = await runtime.invoke("get_group_list", {})
            raw = result.get("data") if isinstance(result, dict) else []
            if isinstance(raw, list):
                groups = [
                    {"group_id": int(group.get("group_id", 0)), "group_name": str(group.get("group_name", ""))}
                    for group in raw
                    if isinstance(group, dict)
                ]
        except Exception:  # noqa: BLE001 - headless transports may not expose group list yet
            groups = []
        if not groups:
            # Fall back to groups observed in message history.
            # 按账号取它所属实例的库：实例各有自己的数据库，读主库会漏掉别的号。
    
            seen: dict[int, dict[str, Any]] = {}
            for message in _store_for(runtime, account).recent(400):
                if not message.is_group:
                    continue
                if account and str(message.account or "") != account:
                    continue
                group_id = int(message.chat_id)
                if group_id <= 0:
                    continue
                seen.setdefault(group_id, {"group_id": group_id, "group_name": f"群 {group_id}"})
            groups = list(seen.values())
        return JSONResponse({"groups": groups})

    @app.get("/api/chat/messages")
    async def chat_messages(request: Request, group_id: int = 0, limit: int = 100, account: str = "") -> JSONResponse:
        denied = guard(request)
        if denied:
            return denied

        items = _store_for(runtime, account).recent(min(400, max(1, limit * 3)))
        messages = []
        for message in items:
            if not message.is_group:
                continue
            if group_id and int(message.chat_id) != int(group_id):
                continue
            if account and str(message.account or "") != account:
                continue
            messages.append(
                {
                    "message_id": message.message_id,
                    "group_id": int(message.chat_id),
                    "sender": message.sender_label,
                    "text": message.text,
                    "time": message.time,
                }
            )
        return JSONResponse({"messages": messages[-limit:]})

    @app.post("/api/chat/send")
    async def chat_send(request: Request) -> JSONResponse:
        denied = guard(request)
        if denied:
            return denied
        if not runtime.config.webui.chat_enabled:
            return JSONResponse({"ok": False, "error": "chat disabled"}, status_code=403)
        try:
            payload = await request.json()
        except json.JSONDecodeError:
            return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)
        group_id = int(payload.get("group_id") or 0) if isinstance(payload, dict) else 0
        text = str(payload.get("text") or "").strip() if isinstance(payload, dict) else ""
        if group_id <= 0 or not text:
            return JSONResponse({"ok": False, "error": "group_id and text are required"}, status_code=400)
        params: dict[str, Any] = {"group_id": group_id,
                                  "message": [{"type": "text", "data": {"text": text}}]}
        # 这是一个保留的旧端点，调用方可能不带 account；带了就传给动作层，
        # 多实例下才能落到正确的号上（不带则仍按主实例处理，行为与以前一致）。
        account = str(payload.get("account") or "").strip() if isinstance(payload, dict) else ""
        if account:
            params["account"] = account
        try:
            result = await runtime.invoke("send_group_msg", params)
        except Exception as exc:  # noqa: BLE001 - 传输层异常很杂，裸 500 到浏览器只剩一行 HTML
            return JSONResponse(
                {"status": "failed", "retcode": 1200,
                 "message": f"发送失败：{str(exc) or type(exc).__name__}", "data": None},
                status_code=502,
            )
        return JSONResponse(result)

    @app.get("/messages/media/{message_id}/{index}")
    async def message_media(message_id: str, index: int, request: Request) -> Any:
        """Serve one element's content, assembling the URL now rather than at store time.

        Order of preference: a local copy QSICK already staged, then a URL stored with the element, then a fresh
        URL built from `file_id` and an rkey fetched at this moment. The last one is what makes an older record
        viewable: the token in it would have expired.
        """
        denied = guard(request)
        if denied:
            return denied
        try:
            wanted = int(message_id)
        except (TypeError, ValueError):
            return PlainTextResponse("bad message id", status_code=400)

        target = None
        try:
            target = runtime.cache_store.get(wanted)
        except Exception:  # noqa: BLE001 - a missing record is a 404, not a 500
            target = None
        if target is None:
            return PlainTextResponse("no such message", status_code=404)
        elements = list(getattr(target, "elements", []) or [])
        if index < 0 or index >= len(elements):
            return PlainTextResponse("no such element", status_code=404)
        element = elements[index]
        data = getattr(element, "data", {}) or {}

        local = data.get("path") or data.get("local_path")
        if local:
            try:
                path = Path(str(local))
                if path.is_file():
                    return FileResponse(path)
            except OSError:
                pass

        # An image goes through the rkey builder first: the URL stored with an element carries no token, and the
        # server answers `invalid rkey` for it. The stored value is the fallback, used only when no token can be had.
        stored_url = data.get("url")
        if isinstance(stored_url, str) and stored_url.startswith("/"):
            stored_url = "https://gchat.qpic.cn" + stored_url
        stored_usable = isinstance(stored_url, str) and stored_url.startswith(("http://", "https://"))
        if stored_usable and str(getattr(element, "type", "") or "").lower() not in ("image", "mface"):
            # Non-image kinds have no builder of their own; the stored URL is all there is.
            return RedirectResponse(stored_url)

        kind = str(getattr(element, "type", "") or "").lower()
        remote = ""

        # A group file is not reachable by rkey: its content comes from OIDB 0x6d6_2, which `get_group_file_url`
        # wraps. Without this branch a file element fell through to "nothing to download".
        if kind == "file":
            file_id = str(data.get("file") or data.get("file_id") or "")
            if not file_id:
                return PlainTextResponse("the file element carries no file id", status_code=404)
            if not target.is_group:
                return PlainTextResponse("private file transfer is not supported here", status_code=501)
            try:
                answer = await runtime.invoke("get_group_file_url", {
                    "group_id": int(target.chat_id),
                    "file_id": file_id,
                    "account": str(getattr(target, "account", "") or ""),
                })
            except Exception as exc:  # noqa: BLE001
                return PlainTextResponse(f"could not ask for the file URL: {exc}", status_code=502)
            payload = answer.get("data") if isinstance(answer, dict) else None
            url = None
            if isinstance(payload, dict):
                url = payload.get("url") or payload.get("download_url")
            if isinstance(url, str) and url.startswith(("http://", "https://")):
                remote = url
            else:
                message = (answer.get("message") if isinstance(answer, dict) else "") or "no URL returned"
                if "does not match any active session" in message:
                    # The record belongs to an account that is not logged in, so no session can ask for its file.
                    return PlainTextResponse(
                        f"download failed: this message was received by account "
                        f"{str(getattr(target, 'account', '') or '')}, which is not running. "
                        f"Start that session and try again.", status_code=409)
                return PlainTextResponse(f"the group file URL was refused: {message}", status_code=502)

        elif kind == "image" or kind == "mface":
            # Two shapes arrive with an image, and they are not interchangeable:
            #
            #   /gchatpic_new/{uin}/{group}-{...}-{md5}/...   self-contained, fetched with no token at all
            #   /download?appid=..&fileid=..&spec=0            carries no token, must be given an rkey
            #
            # Measured: the first answers 200 and the bytes; the second answers `invalid rkey` until one is
            # appended. Preferring the first removes the token from the path entirely -- and with it the whole class
            # of failures where the token belonged to an account that could not see the message.
            raw_url = str(data.get("url") or "")
            if raw_url.startswith("/gchatpic_new/"):
                remote = "https://gchat.qpic.cn" + raw_url
            else:
                scope = "group" if target.is_group else "private"
                try:
                    remote = await runtime.media.url_for(element, scope=scope,
                                                        account=str(getattr(target, "account", "") or ""))
                except Exception as exc:  # noqa: BLE001
                    return PlainTextResponse(f"could not build the URL: {exc}", status_code=502)
                if not remote:
                    remote = stored_url if stored_usable else ""
        else:
            remote = stored_url if stored_usable else ""

        if not remote:
            return PlainTextResponse("this element has nothing to download", status_code=404)

        # Fetch it here and hand over the local file. The remote host refuses the browser's Referer, and this is
        # where that header stops being the browser's to send.
        directory = downloads.export_dir(_QSICK_ROOT)
        name = downloads.name_for(wanted, index, str(data.get("file_name") or data.get("name") or ""), remote)
        account = str(getattr(target, "account", "") or "")

        # One retry at most, and only when a genuinely different token came back.
        #
        # The provider serves a cached rkey: four requests in a row returned the same value, so retrying the same
        # moment cannot help and only looks like flakiness. What does help is a token from an account that can see
        # this message -- and whether one exists is not ours to decide.
        saved = await downloads.save_remote(remote, directory, name)
        if not saved.get("ok") and kind in ("image", "mface"):
            refusal = str(saved.get("error") or "")
            if "invalid rkey" in refusal or "invalid url" in refusal or "empty body" in refusal:
                try:
                    runtime.rkey.forget(account)
                    rebuilt = await runtime.media.url_for(
                        element, scope="group" if target.is_group else "private", account=account)
                except Exception:  # noqa: BLE001 - the first error is the one worth reporting
                    rebuilt = ""
                # A different token is worth one more request; the same one would answer the same way.
                if rebuilt and rebuilt != remote:
                    saved = await downloads.save_remote(rebuilt, directory, name)

        if not saved.get("ok"):
            detail = str(saved.get("error") or "")
            if "invalid fileid" in detail:
                return PlainTextResponse(
                    "download failed: the remote host says this file id is not ours "
                    "(the rkey belongs to an account that cannot see this message). "
                    f"Upstream said: {detail}", status_code=502)
            return PlainTextResponse(f"download failed: {detail}", status_code=502)

        logmod.get_logger("WebUI.Messages").debug(
            "downloaded for the cache page", message_id=wanted, name=name, size=saved.get("size"))
        return RedirectResponse(f"/downloads/{quote(name)}")

    @app.get("/downloads/{name:path}")
    async def downloads_file(name: str, request: Request) -> Any:
        """Serve one file from the download directory, and nothing outside it."""
        denied = guard(request)
        if denied:
            return denied
        directory = downloads.export_dir(_QSICK_ROOT)
        target = downloads.resolve_under(directory, name)
        if target is None:
            return PlainTextResponse("no such download", status_code=404)
        if not target.is_file():
            return PlainTextResponse("no such download", status_code=404)
        # `filename` sets Content-Disposition, so the browser saves it under the name the element carried.
        return FileResponse(target, filename=target.name)

    @app.get("/messages", response_class=HTMLResponse)
    async def messages_page(request: Request, q: str = "", page: int = 1, account: str = "") -> HTMLResponse:
        denied = guard(request)
        if denied:
            return denied
        return templates.TemplateResponse(request, "messages.html",
                                              await _message_page(request, q, page, 30, account))

    @app.get("/api/messages")
    async def messages_api(request: Request, q: str = "", page: int = 1) -> JSONResponse:
        denied = guard(request)
        if denied:
            return denied
        limit = 50
        page = max(1, page)
        # Same store as the page, and the same merged-field search: two views of one cache must not disagree
        # about what it contains.
        model = await _message_page(request, q, page, limit)
        return JSONResponse(
            {
                "total": model["total"],
                "page": model["page_no"],
                "limit": limit,
                "messages": [m.to_dict() for m in model["messages"]],
            }
        )

    @app.get("/healthz")
    async def healthz() -> PlainTextResponse:
        return PlainTextResponse("ok")

    @app.get("/api/logs/download")
    async def logs_download(request: Request) -> PlainTextResponse:
        denied = guard(request)
        if denied:
            return denied
        lines = []
        for record in logmod.BUFFER.since(0):
            data = record.to_dict()
            fields = " ".join(f"{key}={value!r}" for key, value in data["fields"].items())
            lines.append(f"{data['time']} {data['level']:<5} {data['category']:<12} {data['message']} {fields}".rstrip())
        return PlainTextResponse(
            "\n".join(lines) + "\n",
            headers={"Content-Disposition": 'attachment; filename="qsick.log"'},
        )

    @app.post("/api/logs/clear")
    async def logs_clear(request: Request) -> JSONResponse:
        denied = guard(request)
        if denied:
            return denied
        logmod.BUFFER.clear()
        return JSONResponse({"ok": True})

    return app


def _config_fields(manifest: Any, current: dict[str, Any]) -> list[dict[str, Any]]:
    declared = [dict(item) for item in getattr(manifest, "config_fields", [])]
    if not declared:
        for key, value in current.items():
            declared.append({"key": key, "label": key, "type": _infer_type(value), "default": value})
    fields = []
    for item in declared:
        key = str(item.get("key", ""))
        if not key:
            continue
        kind = str(item.get("type", "str")).lower()
        raw_enable = item.get("configEnable", item.get("config_enable"))
        has_enable = isinstance(raw_enable, dict)
        enable = raw_enable if has_enable else {}
        visible = bool(enable.get("ifShow", enable.get("if_show", False))) if has_enable else True
        widget = str(enable.get("type", "text")).lower()
        if widget in ("switch", "toggle", "bool", "boolean"):
            widget = "switch"
        elif widget in ("select", "dropdown", "choice"):
            widget = "select"
        else:
            widget = "text"

        value = current.get(key, item.get("default"))
        if widget == "switch":
            value = value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes", "on")
            rendered = "true" if value else "false"
            kind = "bool"
        elif kind in ("json", "object", "array") or isinstance(value, (dict, list)):
            rendered = json.dumps(value, ensure_ascii=False, indent=2) if value is not None else "{}"
            kind = "json"
        elif isinstance(value, bool) or kind in ("bool", "boolean"):
            rendered = "true" if value else "false"
            kind = "bool"
        elif value is None:
            rendered = ""
        else:
            rendered = str(value)

        options = item.get("options") or enable.get("options") or item.get("choices") or []
        if not isinstance(options, list):
            options = []
        options = [str(option) for option in options]
        if widget == "select" and rendered not in options and options:
            options.insert(0, rendered)

        fields.append(
            {
                "key": key,
                "label": str(item.get("label", key)),
                "type": kind,
                "value": rendered,
                "help": str(item.get("help", "")),
                "visible": visible,
                "widget": widget,
                "options": options,
            }
        )
    return fields


def _infer_type(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return "json"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    return "str"


def _coerce_field(raw: str, kind: str) -> Any:
    text = raw.strip()
    if kind in ("int", "integer"):
        try:
            return int(text)
        except ValueError:
            return text
    if kind in ("float", "number"):
        try:
            return float(text)
        except ValueError:
            return text
    if kind in ("bool", "boolean"):
        return text.lower() in ("1", "true", "yes", "on")
    if kind in ("json", "object", "array"):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
    return raw


def _write_setting(runtime: "Runtime", updates: dict[str, Any]) -> None:
    """Merge one section into the on-disk config, preserving everything else.

    写回**启动时真正读到的那份文件**。早先这里只看 ``QSICK_CONFIG`` 与
    ``qsick.json``，于是用 ``--config other.json`` 起的进程一旦保存设置，就把
    端口写进了 ``qsick.json``——正在用的那份没变，另一份被改坏，表现就是
    "WebUI 端口莫名其妙变了"。
    """
    from dataclasses import asdict

    recorded = str(getattr(runtime.config, "source_path", "") or "")
    # 没有来源时写进**这个运行时自己的数据目录**，不要写当前工作目录。
    #
    # 原来回退到 "qsick.json"，于是测试（直接构造 RuntimeConfig、source_path 为空）每跑一遍
    # 就会覆盖仓库根目录那份真实配置。消息保留的 1234/3 就是这么来的：tests/test_webui_ui.py
    # 往 /settings/storage post 了这两个数字，落进了开发者的 qsick.json。
    path = Path(recorded or os.environ.get("QSICK_CONFIG") or runtime.config.path("qsick.json"))
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = asdict(runtime.config)
    else:
        data = asdict(runtime.config)
    for section, values in updates.items():
        if isinstance(values, dict):
            data.setdefault(section, {}).update(values)
        else:
            data[section] = values
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _filter_specs(specs: list[dict[str, Any]], q: str, category: str) -> list[dict[str, Any]]:
    needle = q.strip().lower()
    out = []
    for spec in specs:
        if category and spec["category"] != category:
            continue
        if needle and needle not in spec["name"].lower() and needle not in spec["summary"].lower():
            continue
        out.append(spec)
    return out


def _config_json(runtime: "Runtime") -> str:
    from dataclasses import asdict

    data = asdict(runtime.config)
    data["onebot"]["access_token"] = "***" if runtime.config.onebot.access_token else ""
    data["webui"]["access_token"] = "***" if runtime.config.webui.access_token else ""
    return json.dumps(data, ensure_ascii=False, indent=2)


def _preview(message: Any) -> str:
    from ..protocol import elements as E

    return E.preview(message.elements, limit=80)

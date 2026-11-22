"""The state link between QSICK and one pipe session.

A pipe session's login state is an **event**: the session knows when it logs in, when its message listener
attaches, and when it is shutting down. It used to be discovered by asking -- a `login.info` post every three
seconds, plus a retry ladder to tell "busy" from "gone" -- which cost a request per session per tick forever and
flickered whenever a reply was slow.

This connects to the session's WebSocket endpoint (`/qsick` on the same port the action API already uses) and
receives:

    state      whenever the injected side's stage changes (login, listener attached, QR, failure)
    heartbeat  every 30 s, so an idle link is known to be alive
    closing    when the session is shutting down normally

The same object also owns the HTTP side for that session, so one component talks to one session and the port is
the identity -- there is no second place that has to agree about which session is which.
"""
from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

from .. import log as logmod

log = logmod.get_logger("SessionLink")

#: How long to wait before retrying a link that could not connect, in seconds.
#:
#: Short while a session is starting (it will answer once its action listener is up), and there is no reason to
#: back off further: this is a loopback connect attempt, not traffic to a server.
RECONNECT_DELAYS: tuple[float, ...] = (0.5, 1.0, 2.0, 3.0)

#: Startup attempts, and the interval between them, in seconds.
#:
#: Pressing 启动 opens a window: one attempt every five seconds, five attempts. All five failing means the
#: launch did not come up and the session is reported as 未启动 -- it is not retried until 启动 is pressed again.
#:
#: Five times five seconds is enough because the WebSocket endpoint exists as soon as the DLL's HTTP server is
#: listening, which is early in the launch. A handshake says nothing about the login: the first state frame
#: reports whether there is an account. (This is also why the window is not sized for "QQ has logged in" -- that
#: can take minutes and is a different question.)
STARTUP_ATTEMPTS = 5
STARTUP_INTERVAL = 5.0


@dataclass
class SessionState:
    """What one session last told us about itself."""

    uin: str = ""
    stage: str = ""
    ready: bool = False
    connected: bool = False
    closing: bool = False
    detail: str = ""
    heartbeats: int = 0
    #: False until the link has received anything at all; used to tell "starting" from "gone".
    heard_from: bool = False
    #: True once the startup window ran out without a handshake.
    failed: bool = False
    #: Whether the session's action API answered at all; false means it is up but not ready.
    answered: bool = True

    def phase(self) -> str:
        """Which of the three states the session is in.

        The distinction matters to a consumer: a handshake proves the action listener is up, not that the
        account exists, and an account without a message listener cannot deliver events yet. Flattening these
        into a boolean is what made the UI show 未登录 for a session that was logged in but still exporting.
        """
        if self.failed:
            return "unstarted"
        if not self.connected and not self.heard_from:
            return "connecting"
        if not self.uin or self.uin == "0":
            return "unlogged"
        # The account is known but the API has not answered: logged in, not yet exporting.
        if not self.ready:
            return "logged_not_ready"
        return "ready"

    def as_row(self) -> dict[str, Any]:
        # `"0"` is this codebase's sentinel for "no account" -- the injected side reports it before a session
        # exists, and treating it as a uin would show a row for account zero.
        has_account = bool(self.uin) and self.uin != "0"
        logged_in = has_account and not self.closing
        return {
            "running": bool(self.connected and not self.closing and not self.failed),
            "logged_in": logged_in,
            "closed": bool(self.closing),
            "detail": self.detail,
            "account": {"uin": self.uin, "nickname": ""} if logged_in else {},
            "stage": self.stage,
            "ready": self.ready,
            "phase": self.phase(),
            "probed": self.heard_from,
        }


class SessionLink:
    """Keeps one WebSocket open to one session and records what it says.

    Deliberately passive: it reports state and never issues actions. Actions stay on the HTTP endpoint, which is
    request/response and needs no long-lived connection -- mixing the two would make a slow action look like a
    dropped state link.
    """

    def __init__(self, port: int, token: str, *, name: str = "") -> None:
        self.port = int(port or 0)
        self.token = str(token or "")
        self.name = name
        self.state = SessionState()
        self._stopping = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        #: Called with this link whenever the state changes, so the owner can react without polling.
        self.on_change: Callable[["SessionLink"], None] | None = None
        #: True while the link is inside the five-attempt startup window.
        self._starting = False

    # -- lifecycle -------------------------------------------------------------------------------------

    def start(self, *, starting: bool = False) -> None:
        """Begin (or resume) the connection.

        starting=True opens the startup window: five attempts five seconds apart, then 未启动. A link that is
        merely reconnecting after a drop uses the short backoff instead -- a launch that never came up and a
        connection that was lost are different events and should not share a budget.
        """
        if starting:
            self._starting = True
            self.state.failed = False
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name=f"qsick-link-{self.name or self.port}")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - stopping must not raise
                pass
            self._task = None

    def _changed(self) -> None:
        if self.on_change is not None:
            try:
                self.on_change(self)
            except Exception as exc:  # noqa: BLE001 - a listener must not kill the link
                log.debug("session state listener failed", error=str(exc))

    # -- protocol --------------------------------------------------------------------------------------

    async def _run(self) -> None:
        import websockets

        attempt = 0
        # Startup budget: five attempts, five seconds apart. Once a handshake succeeds the timer stops and the
        # normal short backoff takes over -- a dropped connection later is a different event from a launch that
        # never came up, and it should be retried promptly rather than counted against a budget.
        startup_left = STARTUP_ATTEMPTS if self._starting else 0
        while not self._stopping.is_set():
            if self.port <= 0:
                # Nothing to connect to yet; a port appears once the session is launched.
                await asyncio.sleep(1.0)
                continue
            url = f"ws://127.0.0.1:{self.port}/qsick"
            headers = {"X-QSICK-Token": self.token} if self.token else {}
            try:
                connector = websockets.connect(url, additional_headers=headers, max_size=1 << 20,
                                               open_timeout=5, ping_interval=None)
            except TypeError:  # websockets < 12
                connector = websockets.connect(url, extra_headers=headers, max_size=1 << 20,
                                               open_timeout=5, ping_interval=None)
            try:
                async with connector as socket:
                    attempt = 0
                    startup_left = 0          # the startup window is closed by a successful handshake
                    self._starting = False
                    self.state.failed = False
                    self.state.connected = True
                    self.state.closing = False
                    self._changed()
                    async for raw in socket:
                        self._consume(raw)
                    # A clean close means the session said goodbye (or is going away); report it rather than
                    # calling it an error.
                    self.state.connected = False
                    self.state.detail = self.state.detail or "会话已断开"
                    self._changed()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect on any failure
                self.state.connected = False
                self.state.detail = f"{type(exc).__name__}: {exc}"
                if startup_left > 0:
                    startup_left -= 1
                    if startup_left == 0:
                        # The launch did not come up. Report it as 未启动 and stop: the operator's rule, and it
                        # also stops a burst of retries against a port nothing will ever bind.
                        self.state.failed = True
                        self.state.detail = f"启动失败：{STARTUP_ATTEMPTS} 次握手均未成功（{exc}）"
                        log.warn("session did not answer the state handshake", session=self.name,
                                 attempts=STARTUP_ATTEMPTS, error=str(exc))
                        self._changed()
                        return
                    self._changed()
                    try:
                        await asyncio.wait_for(self._stopping.wait(), timeout=STARTUP_INTERVAL)
                    except asyncio.TimeoutError:
                        pass
                    continue
                self._changed()
                delay = RECONNECT_DELAYS[min(attempt, len(RECONNECT_DELAYS) - 1)]
                attempt += 1
                try:
                    await asyncio.wait_for(self._stopping.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
                continue
            if self._stopping.is_set():
                return
            await asyncio.sleep(RECONNECT_DELAYS[0])

    def _consume(self, raw: str | bytes) -> None:
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            return
        if not isinstance(payload, dict):
            return
        kind = str(payload.get("type") or "")
        # Every frame names its session. Two sessions share one Pipe/ client, so a frame from another id (a
        # previous launch on a recycled port, most likely) must not be believed about this one.
        reported = str(payload.get("sessionid") or "")
        if reported and self.name and reported != self.name:
            log.debug("ignoring a frame from another session", expected=self.name, got=reported)
            return
        self.state.heard_from = True
        if kind == "state":
            # The session reports only what it knows about itself: whether its API answered and, if so, which
            # account. No stage, no shared file -- naming the phase is this side's job.
            self.state.answered = bool(payload.get("answered", True))
            self.state.uin = str(payload.get("uin") or "")
            self.state.stage = ""
            self.state.ready = self.state.answered and bool(self.state.uin)
            self.state.detail = ""
        elif kind == "heartbeat":
            self.state.heartbeats += 1
            self.state.detail = ""
        elif kind == "closing":
            self.state.closing = True
            self.state.detail = f"会话关闭：{payload.get('reason') or ''}".strip("：")
        else:
            return
        self._changed()


def http_action(port: int, op: str, params: dict[str, Any] | None = None, *,
                token: str = "", timeout: float = 20.0) -> dict[str, Any]:
    """POST one action to a session's HTTP endpoint.

    Lives beside the link so there is one component per session, but kept a plain function: it is
    request/response and must not depend on the WebSocket being up.
    """
    body = json.dumps({"op": op, "params": params or {}}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-QSICK-Token"] = token
    request = urllib.request.Request(f"http://127.0.0.1:{int(port)}/action", data=body,
                                     headers=headers, method="POST")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            return json.loads(response.read(256 * 1024).decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read(4096).decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001
            return {"ok": False, "error": f"HTTP {exc.code}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def request_shutdown(port: int, token: str = "", timeout: float = 5.0) -> bool:
    """Ask a session to shut down normally, so it can announce it before going away.

    This is the graceful half of closing a session: the DLL notifies its subscribers and then exits, which means
    the state link sees `closing` instead of having to infer death from silence. A caller that gets False must
    fall back to terminating the process.
    """
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-QSICK-Token"] = token
    request = urllib.request.Request(f"http://127.0.0.1:{int(port)}/quit", data=b"{}",
                                     headers=headers, method="POST")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            payload = json.loads(response.read(4096).decode("utf-8", "replace"))
            return bool(payload.get("ok"))
    except Exception:  # noqa: BLE001 - a session that cannot be asked is a session to terminate
        return False

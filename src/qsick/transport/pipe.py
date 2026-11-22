"""Windows named-pipe client for the native payload.

Two pipes, mirroring the payload: ``.ctl`` carries request/reply frames,
``.evt`` carries one-way events. Blocking reads live on daemon threads; the
asyncio side only sees futures and an async iterator, so the event loop is never
blocked by a pipe that stalls.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from concurrent.futures import Future
from pathlib import Path
from typing import Any, AsyncIterator

from .. import log as logmod
from ..config import TransportConfig
from . import injector, payload, session
from .base import LoginState, PacketEvent, SendResult, Transport, TransportError
from .framing import Frame, FrameDecoder, Op, ProtocolError

log = logmod.get_logger("Transport.Pipe")

log = logmod.get_logger("Transport.Pipe")

_READ_CHUNK = 65536


class PipeTransport(Transport):
    name = "pipe"

    def __init__(self, config: TransportConfig) -> None:
        self.config = config
        self._login = LoginState(logged_in=False, uin="0")
        self.payload_info: dict[str, Any] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._events: asyncio.Queue[PacketEvent] = asyncio.Queue(maxsize=2048)
        self._pending: dict[int, Future[Frame]] = {}
        self._pending_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._request_id = 0
        self._ctl = None
        self._rsp = None
        self._evt = None
        self._threads: list[threading.Thread] = []
        self._stopped = threading.Event()
        self._login_frame_seen = threading.Event()
        self._pid = 0
        self._token = ""
        self._hello = asyncio.Event()

    # ------------------------------------------------------------------ state

    @property
    def login(self) -> LoginState:
        return self._login

    @property
    def pid(self) -> int:
        return self._pid

    # --------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        if os.name != "nt":
            raise TransportError("the pipe transport requires Windows")
        self._loop = asyncio.get_running_loop()
        pid = _configured_pid() or _discover_pid()
        if not pid:
            raise TransportError("no QQ process found; start QQ or set QSICK_QQ_PID")
        self._pid = pid
        self._token = await asyncio.to_thread(self._resolve_token, pid)
        try:
            await asyncio.to_thread(self._connect, pid)
        except TransportError:
            if self.config.attach_only:
                raise
            await asyncio.to_thread(self._inject, pid)
            await asyncio.to_thread(self._connect, pid)
        # Readers must be running before the handshake: the reply is delivered
        # asynchronously by the reader thread and would otherwise be lost.
        self._start_reader("rsp")
        self._start_reader("evt")
        await self._handshake()
        try:
            await asyncio.wait_for(self._wait_login(), timeout=3.0)
        except asyncio.TimeoutError:
            log.warn("payload connected but no login frame yet", pid=pid)

    def _resolve_token(self, pid: int) -> str:
        mode = (self.config.pipe_auth or "none").lower()
        if mode == "none":
            log.warn("pipe authentication disabled; any local process can drive this account")
            return ""
        if mode == "fixed":
            token = self.config.pipe_token or os.environ.get("QSICK_PIPE_TOKEN", "")
            if not token:
                raise TransportError("pipe_auth=fixed requires transport.pipe_token or QSICK_PIPE_TOKEN")
            session.write_token(pid, token, self.config.session_dir)
            return token
        # random: reuse a live session token, otherwise mint one
        existing = session.read_token(pid, self.config.session_dir)
        if existing and self._probe(pid, existing):
            log.info("reusing the payload session token", pid=pid)
            return existing
        token = session.new_token()
        session.write_token(pid, token, self.config.session_dir)
        log.info("generated a per-session pipe token", pid=pid)
        return token

    def _probe(self, pid: int, token: str) -> bool:
        ctl_path, _, _ = session.pipe_names(self.config.pipe_prefix, pid, token)
        try:
            handle = open(ctl_path, "r+b", buffering=0)  # noqa: SIM115
        except OSError:
            return False
        try:
            handle.close()
        except OSError:
            pass
        return True

    def _inject(self, pid: int) -> None:
        dll = payload.locate_payload(self.config.payload_path)
        if dll is None:
            raise TransportError(
                "payload not built: expected native/qsick-payload/target/release/qsick_payload.dll "
                "or set QSICK_PAYLOAD / transport.payload_path"
            )
        injector.inject_dll(pid, dll)
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            if self._probe(pid, self._token):
                return
            time.sleep(0.1)
        raise TransportError("payload injected but its pipes never appeared")

    def _connect(self, pid: int) -> None:
        ctl_path, rsp_path, evt_path = session.pipe_names(self.config.pipe_prefix, pid, self._token)
        deadline = time.monotonic() + self.config.connect_timeout
        last: Exception | None = None
        while time.monotonic() < deadline:
            try:
                self._ctl = open(ctl_path, "wb", buffering=0)   # requests only
                self._rsp = open(rsp_path, "rb", buffering=0)   # replies only
                self._evt = open(evt_path, "rb", buffering=0)   # events only
                log.info("connected to payload pipes", pid=pid)
                return
            except OSError as exc:
                last = exc
                for handle in (self._ctl, self._rsp, self._evt):
                    if handle is not None:
                        try:
                            handle.close()
                        except OSError:
                            pass
                self._ctl = self._rsp = self._evt = None
                time.sleep(0.2)
        raise TransportError(f"cannot open payload pipes for pid {pid}: {last}")

    async def _handshake(self) -> None:
        frame = await self._request(op=Op.HELLO, cmd="hello", msg=self._token, timeout=3.0)
        if frame.status != -2 and frame.status != 0:
            raise TransportError(f"payload rejected hello: {frame.msg or frame.status}")
        if frame.status == -2:
            raise TransportError("payload rejected the session token (nonce mismatch)")
        if frame.body:
            import json

            try:
                parsed = json.loads(frame.body.decode("utf-8"))
                if isinstance(parsed, dict):
                    self.payload_info = parsed
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass
        log.info(
            "payload handshake ok",
            pid=self._pid,
            version=self.payload_info.get("version", "?"),
            hooks=bool(self.payload_info.get("hooks", False)),
        )

    def info(self) -> dict[str, Any]:
        return {
            "pid": self._pid,
            "prefix": self.config.pipe_prefix,
            "auth": self.config.pipe_auth,
            "payload": dict(self.payload_info),
        }

    def _start_reader(self, which: str) -> None:
        handle = self._rsp if which == "rsp" else self._evt
        thread = threading.Thread(target=self._read_loop, args=(which, handle), name=f"qsick-pipe-{which}", daemon=True)
        thread.start()
        self._threads.append(thread)

    def _read_loop(self, which: str, handle: Any) -> None:
        decoder = FrameDecoder()
        while not self._stopped.is_set():
            try:
                chunk = handle.read(_READ_CHUNK)
            except (OSError, ValueError) as exc:
                if not self._stopped.is_set():
                    log.warn("pipe read failed", pipe=which, error=str(exc))
                    self._publish(PacketEvent(cmd="runtime.error", kind="runtime", body=str(exc).encode()))
                return
            if not chunk:
                if not self._stopped.is_set():
                    log.warn("pipe closed", pipe=which)
                    self._publish(PacketEvent(cmd="runtime.closed", kind="runtime", body=b"{}"))
                return
            try:
                frames = decoder.push(chunk)
            except ProtocolError as exc:
                log.error("framing error", pipe=which, error=str(exc))
                continue
            for frame in frames:
                self._on_frame(which, frame)

    def _on_frame(self, which: str, frame: Frame) -> None:
        if which == "rsp":
            with self._pending_lock:
                future = self._pending.pop(frame.request, None)
            if future is not None and not future.done():
                try:
                    self._loop.call_soon_threadsafe(future.set_result, frame)  # type: ignore[union-attr]
                except RuntimeError:
                    pass
                return
            if frame.op in (Op.ACK, Op.REPLY, Op.HELLO):
                return
        if frame.op == Op.LOGIN:
            self._apply_login(frame)
            return
        if frame.op == Op.EVENT:
            self._publish(_event_of(frame))
            return
        if frame.op in (Op.ERROR,):
            log.warn("payload error frame", status=frame.status, message=frame.msg)
            return

    def _apply_login(self, frame: Frame) -> None:
        import json

        payload: dict[str, Any] = {}
        if frame.body:
            try:
                payload = json.loads(frame.body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                payload = {}
        self._login = LoginState(
            logged_in=bool(payload.get("logged_in", True)),
            uin=str(payload.get("uin", frame.value or "0")),
            nickname=str(payload.get("nickname", "")),
        )
        self._login_frame_seen.set()
        self._publish(PacketEvent(cmd="login", kind="login", body=frame.body))

    async def _wait_login(self) -> None:
        # A payload without QQ hooks still reports a login frame (logged_in=false);
        # stop waiting as soon as any frame arrived, not only when logged in.
        while not self._login.logged_in and not self._login_frame_seen.is_set():
            await asyncio.sleep(0.05)

    def _publish(self, event: PacketEvent) -> None:
        loop = self._loop
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(self._enqueue, event)
        except RuntimeError:
            pass

    def push(self, event: dict[str, Any]) -> None:
        """Feed a message reported by an injected session into the normal event pipeline.

        Same contract as `HeadlessTransport.push`, because both transports carry events that originate
        outside this process and both must land in the same store, OneBot stage and network-profile dispatch.
        The runtime's pipe poller calls this with `{kind, instance, payload}` where `instance` is the
        **account** (uin), not the client directory: every pipe session writes into one shared
        `events.jsonl`, so the uin on each line is what identifies who received it.

        The uin doubles as the instance key, which is what network profiles are owned by -- so the value
        passed here decides where a message gets routed.
        """
        kind = str(event.get("kind") or "message")
        payload = event.get("payload")
        if not isinstance(payload, dict):
            raise ValueError("event payload must be an object")
        instance = str(event.get("instance") or "").strip()

        if kind == "login":
            state = LoginState(logged_in=bool(payload.get("logged_in", False)),
                               uin=str(payload.get("uin") or "0"),
                               nickname=str(payload.get("nickname") or ""),
                               online=bool(payload.get("online", payload.get("logged_in", False))),
                               kicked=bool(payload.get("kicked", False)),
                               kick_reason=str(payload.get("reason") or payload.get("kick_reason") or ""))
            self._login = state
            self._login_frame_seen.set()
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
            self._publish(PacketEvent(cmd="login", kind="login", body=body))
            return

        if kind in ("message", "message_sent"):
            chat = payload.get("chat")
            if not isinstance(chat, dict) or chat.get("type") not in ("group", "private"):
                raise ValueError("unsupported message chat type")
            mid = payload.get("message_id")
            if not isinstance(mid, (int, str)) or isinstance(mid, bool) or not str(mid).isdigit():
                raise ValueError("message_id must be a positive decimal integer/string")
            cmd = "message.sent" if kind == "message_sent" else "message." + chat["type"]
            log.debug("pipe message received", message_id=str(mid),
                      chat_type=chat["type"], chat_id=chat.get("id"), account=instance)
        elif kind in ("notice", "request", "runtime"):
            cmd = str(event.get("cmd") or "")
            if not cmd.startswith(kind + "."):
                raise ValueError("event kind/cmd mismatch")
        else:
            raise ValueError("unsupported pipe event kind")

        # The account owns the event. Falling back to the payload's own `account` keeps this working if a
        # caller has the payload but not the attribution.
        owner = str(payload.get("account") or "").strip() or instance
        if owner == "0":
            owner = ""
        packet = PacketEvent(cmd=cmd, kind=kind, instance=owner, account=owner,
                             body=json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8"))
        self._publish(packet)

    def _enqueue(self, event: PacketEvent) -> None:
        try:
            self._events.put_nowait(event)
        except asyncio.QueueFull:
            try:
                self._events.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                self._events.put_nowait(event)
            except asyncio.QueueFull:
                pass

    async def stop(self) -> None:
        self._stopped.set()
        for handle in (self._ctl, self._rsp, self._evt):
            if handle is None:
                continue
            try:
                handle.close()
            except OSError:
                pass
        # Fail any request still in flight.
        with self._pending_lock:
            pending, self._pending = self._pending, {}
        for future in pending.values():
            if not future.done():
                future.set_exception(TransportError("transport stopped"))
        self._ctl = None
        self._rsp = None
        self._evt = None

    async def ping(self) -> float:
        started = time.perf_counter()
        await self._request(op=Op.PING, cmd="ping", timeout=2.0)
        return (time.perf_counter() - started) * 1000

    # -------------------------------------------------------------------- send

    async def send(
        self, op: str, params: dict[str, Any] | None = None, *, timeout: float | None = None
    ) -> SendResult:
        import json

        body = json.dumps(params or {}, ensure_ascii=False).encode("utf-8")
        try:
            frame = await self._request(op=Op.SEND, cmd=op, body=body, timeout=timeout or self.config.reply_timeout)
        except TransportError as exc:
            return SendResult.failure(str(exc))
        data: dict[str, Any] = {}
        if frame.body:
            try:
                parsed = json.loads(frame.body.decode("utf-8"))
                if isinstance(parsed, dict):
                    data = parsed
            except (UnicodeDecodeError, json.JSONDecodeError):
                data = {}
        return SendResult(
            ok=frame.status == 0,
            status=frame.status,
            message=frame.msg,
            body=frame.body,
            data=data,
        )

    async def _request(
        self,
        *,
        op: int,
        cmd: str = "",
        body: bytes = b"",
        msg: str = "",
        timeout: float = 5.0,
    ) -> Frame:
        if self._ctl is None:
            raise TransportError("control pipe is not connected")
        future: Future[Frame] = Future()
        with self._pending_lock:
            self._request_id = (self._request_id + 1) & 0xFFFFFFFF
            request_id = self._request_id
            self._pending[request_id] = future
        frame = Frame(op=op, request=request_id, cmd=cmd, msg=msg, body=body)
        try:
            with self._write_lock:
                self._ctl.write(frame.to_bytes())
        except OSError as exc:
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise TransportError(f"write failed: {exc}") from exc

        try:
            return await asyncio.wait_for(asyncio.wrap_future(future), timeout=timeout)
        except asyncio.TimeoutError:
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise TransportError(f"payload did not reply to {cmd or op} within {timeout}s") from None

    # ------------------------------------------------------------------ events

    def events(self) -> AsyncIterator[PacketEvent]:
        async def stream() -> AsyncIterator[PacketEvent]:
            while True:
                yield await self._events.get()

        return stream()


def _event_of(frame: Frame) -> PacketEvent:
    cmd = frame.cmd
    if cmd.startswith("message."):
        kind = "message"
    elif cmd.startswith("login"):
        kind = "login"
    elif cmd.startswith("notice.") or cmd.startswith("request."):
        kind = "packet"
    else:
        kind = "packet"
    return PacketEvent(cmd=cmd, body=frame.body, value=frame.value, kind=kind)


def _configured_pid() -> int:
    value = os.environ.get("QSICK_QQ_PID", "").strip()
    try:
        return int(value) if value else 0
    except ValueError:
        return 0


def _discover_pid() -> int:
    try:
        from .injector import find_qq_pids
    except Exception:  # noqa: BLE001 - non-Windows import guard
        return 0
    pids = find_qq_pids()
    if not pids:
        return 0
    return max(pids)

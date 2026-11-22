"""Headless client instances.

Each account lives in ``headlessClient/C0``, ``C1`` ... and runs the Node host
(``headless_host.js``), which loads QQ's ``wrapper.node`` through QSICK's own
``QQNT.dll`` shim. The host serves QR/status on a per-instance loopback port.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import secrets
import socket
import subprocess
import sys
import time
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import log as logmod
from .config import DEFAULT_WEBUI_PORT
from .job_guard import JobGuard

log = logmod.get_logger("Headless")

ROOT_NAME = "headlessClient"

#: The pipe instance's fixed directory name.
#:
#: Deliberately not C<number>: there is only ever one pipe instance, so a sequence number
#: carries no information, and mixing it into the C0/C1 series makes it look like deleting it
#: would let the next headless instance reuse the number (it would not).
PIPE_INSTANCE_ID = "Pipe"

#: First port handed to a pipe session, and the top of the range it may use.
#:
#: Each pipe session exposes its injected DLL's control API on its own TCP port, so two sessions cannot
#: share one: the second `QsickStartHttp` would fail to bind and that session would be unreachable while
#: still looking started. Ports are therefore allocated per session, from 7540 upward, and recorded in
#: the instance config so a restart reuses the same one.
#:
#: 9199 remains the default for an instance that has no port recorded (older configs, and the
#: ad-hoc instances the tools create), which is why it is still referenced below.
PIPE_PORT_BASE = 7540
PIPE_PORT_MAX = 9198

#: Legacy fixed port, kept as the fallback for instances created before per-session ports existed.
PIPE_PORT = 9199

_LOGIN_LOCKS: dict[str, threading.Lock] = {}
_LOGIN_LOCKS_GUARD = threading.Lock()
HOST_JS = Path(__file__).with_name("headless_host.js")
REPO_ROOT = Path(__file__).resolve().parents[2]


def read_appid(major: Path) -> str:
    if not major.is_file():
        return ""
    data = major.read_bytes()
    marker = b"QQAppId/"
    index = data.find(marker)
    if index < 0:
        return ""
    start = index + len(marker)
    end = data.find(b"\x00", start)
    value = data[start:end].decode("ascii", "ignore") if end > start else ""
    return value if value.isdigit() else ""


def _runtime_version(runtime: Path) -> str:
    """Version recorded by an already prepared runtime copy (``9.9.33-52230``).

    ``runtime/package.json`` comes from QQ's app directory, so it describes the
    binaries that are actually on disk — unlike ``versions/config.json`` of the
    installed client, which moves on when QQ updates itself.
    """
    try:
        payload = json.loads((runtime / "package.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    return str(payload.get("version") or "") if isinstance(payload, dict) else ""


#: Overridable "is this pid alive?" probe.
#:
#: Production leaves this None and `_pid_running` answers. Tests set it so a fixture can declare a process
#: alive without spawning QQ -- the real answer depends on the machine, and a test asserting on it would
#: pass or fail by environment rather than by code.
_ALIVE_PROBE: Callable[[int], bool] | None = None


def _pid_running(pid: int) -> bool:
    """Whether a process with this pid exists right now.

    On Windows the cheapest reliable check is asking the OS for the process handle; `os.kill` with
    signal 0 is the POSIX equivalent. A pid that has been reused by an unrelated process will read as
    running, which is why callers use this only as one of several signals rather than the sole answer.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=5, creationflags=creation, check=False)
        except (OSError, subprocess.SubprocessError):
            return False
        return str(pid) in (result.stdout or "")
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


#: Alphabet for a session id: lowercase letters and digits, four of them.
#:
#: Short and lowercase so the id is readable in a URL, a log line and a directory listing. Excluded on purpose:
#: `i`, `l`, `o`, `0`, `1` are the pairs people mistype when reading an id off the screen and typing it back
#: into a form. Four characters from the remaining 32 symbols is ~1 million combinations, and allocation
#: retries on collision anyway (see `start_session`), so length is not the constraint.
SESSION_ID_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
SESSION_ID_LENGTH = 4


def new_session_tmp_id() -> str:
    """A fresh 4-character lowercase id for one pipe session.

    The id names the session's directory and is the session's identity: it is what the list shows, what the
    start/close endpoints take, and what the auto-start setting stores. It has to be short enough to read at a
    glance and stable enough to type, which is why it is four characters from a reduced alphabet rather than a
    hash.

    Kept regenerated per launch: two launches of the same session are different processes with different
    ports, and reusing the directory would let one launch's leftover DLL config affect the next. Relaunching an
    existing session passes its id in, so this only runs for a genuinely new one.
    """
    return "".join(secrets.choice(SESSION_ID_ALPHABET) for _ in range(SESSION_ID_LENGTH))


def is_session_id(name: str) -> bool:
    """Whether a directory name is a session id.

    Two shapes are accepted, and both have to be:

    * the current four lowercase alphanumerics (`k3m9`)
    * the older 16 binary digits (`1110000010111011`) -- sessions created before the format changed are still
      on disk with their port assignments and their config, and refusing to list them would make them
      unreachable: no way to start, close or delete what the list does not show.
    """
    text = str(name or "")
    if len(text) == SESSION_ID_LENGTH and all(ch in SESSION_ID_ALPHABET for ch in text):
        return True
    return len(text) == 16 and all(ch in "01" for ch in text)


def session_dll_dir(root: Path | str = ROOT_NAME) -> Path:
    """`headlessClient/Pipe/dll/` -- scratch space for per-launch DLL copies.

    One shared parent for all sessions rather than a directory inside each: the copies are temporary and
    only need to outlive the process that loaded them, so keeping them together makes it obvious they can
    all be thrown away together.
    """
    return Path(root) / PIPE_INSTANCE_ID / "dll"


def reset_session_dll_dir(root: Path | str = ROOT_NAME) -> None:
    """Empty the DLL scratch directory, called once at QSICK startup.

    Copies from a previous run serve no purpose: each launch writes its own. Leaving them would grow the
    tree by one DLL per launch forever, and a stale copy next to a stale config is exactly the kind of
    leftover that later gets mistaken for the live one.

    A copy still mapped into a running QQ cannot be deleted on Windows; those are skipped rather than
    treated as an error, since the point is housekeeping, not enforcement.
    """
    directory = session_dll_dir(root)
    if not directory.is_dir():
        return
    for entry in directory.iterdir():
        try:
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink()
        except OSError:
            continue


def read_session_config(tmp_id: str, root: Path | str = ROOT_NAME) -> dict[str, Any]:
    """Read one session's config, or an empty dict if it cannot be read.

    Centralised because the file is read from several places -- listing, probing, starting, killing -- and
    they must agree on which key wins. `port` is what the operator wants; `nowport` is what the process
    actually bound (see `read_session_dirs` for why those differ).
    """
    path = session_dll_dir(root) / str(tmp_id) / PIPE_DLL_CONFIG
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def session_ports(config: dict[str, Any]) -> tuple[int, int]:
    """Split a session config into `(port, nowport)`.

    `port` is the requested port and only changes when the operator edits it.
    `nowport` is the port the running process actually bound; it is aligned to `port` on start and set to 0
    on close, so while a session runs the two can differ. That difference is the point: anything talking to
    a live session must use `nowport`, because the process keeps listening on the old number after an edit
    (the DLL creates its listener once per process and cannot rebind).

    A config written before `nowport` existed has no such key at all. It is treated as aligned with `port`
    -- the common case of a session nobody has edited -- rather than as zero, which would look like a
    stopped session.

    An explicit `nowport` of 0 is kept as 0 and is not treated as "unset": that is how a closed session
    records that it has bound nothing, and widening it back to `port` would claim a listener that is not
    there.
    """
    block = config.get("session")
    block = block if isinstance(block, dict) else {}

    def as_int(value: Any, fallback: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return fallback

    port = as_int(block.get("port") or config.get("port") or 0)
    if "nowport" in block:
        nowport = as_int(block.get("nowport"), 0)
    elif "nowport" in config:
        nowport = as_int(config.get("nowport"), 0)
    else:
        # Key absent entirely: written before `nowport` existed, so assume it matches the request.
        nowport = port
    return port, nowport


def write_session_ports(tmp_id: str, root: Path | str = ROOT_NAME,
                        *, port: int | None = None, nowport: int | None = None) -> dict[str, Any]:
    """Update `port` and/or `nowport` in one session's config, leaving everything else alone.

    Pass `nowport=None` to leave it unchanged, or `nowport=0` to clear it (a closed session binds nothing).
    Both keys are mirrored at the top level and under `session`, because the injected DLL reads the flat
    ones while this side reads the nested ones -- writing one shape only would let the two drift.
    """
    directory = session_dll_dir(root) / str(tmp_id)
    config_path = directory / PIPE_DLL_CONFIG
    config: dict[str, Any] = {}
    if config_path.is_file():
        try:
            loaded = json.loads(config_path.read_text(encoding="utf-8"))
            config = loaded if isinstance(loaded, dict) else {}
        except (OSError, json.JSONDecodeError):
            config = {}
    block = config.get("session")
    block = block if isinstance(block, dict) else {}
    block["tmp_id"] = str(tmp_id)
    if port is not None:
        config["port"] = int(port)
        block["port"] = int(port)
    if nowport is not None:
        config["nowport"] = int(nowport)
        block["nowport"] = int(nowport)
    elif "nowport" not in block:
        # Always materialise `nowport`. If it is absent, `session_ports` falls back to treating it as equal
        # to `port`; writing it explicitly here means a later edit to `port` cannot silently move a number
        # that is supposed to mean "what is actually bound".
        current_port, current_nowport = session_ports(config)
        config["nowport"] = int(current_nowport)
        block["nowport"] = int(current_nowport)
    config["session"] = block
    directory.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    return config


def read_session_dirs(root: Path | str = ROOT_NAME) -> list[dict[str, Any]]:
    """Describe every session directory under `dll/`, newest first.

    A directory counts as a session when it is named with a 16-character binary id -- that is the only thing
    written there, so anything else is not ours and treating it as a session would aim the close button at
    something unrelated.

    Each entry carries two ports, and which one a caller wants depends on what it is doing:

        port      what the operator asked for; the number the next start will use
        nowport   what a running process actually bound; 0 when nothing is running

    They differ exactly when someone edits the port of a live session, and `nowport` is what any probe must
    use. Reading only `port` would send a health check to a number nothing is listening on and report a
    perfectly healthy session as stopped.

    A directory without a readable `qsick_pipe.json` is not reported as a session. It has no port, so
    nothing can be done with it -- it cannot be started (no port to bind), probed (nowhere to ask) or
    meaningfully closed. Showing it produced a row reading "配置缺失" that survived a delete attempt and
    only went away on a second click, which looks like a bug in the UI rather than leftover state.

    Such a directory is what a delete that could not remove the locked DLL leaves behind. `close_session`
    now retries until the handle is released and reports failure if it cannot, so the leftovers this
    skips are the ones nothing could be done about anyway.
    """
    directory = session_dll_dir(root)
    if not directory.is_dir():
        return []
    sessions: list[dict[str, Any]] = []
    for entry in directory.iterdir():
        if not entry.is_dir():
            continue
        tmp_id = entry.name
        if not is_session_id(tmp_id):
            continue
        config = read_session_config(tmp_id, root)
        if not config:
            # No config: nothing actionable. See the note above.
            continue
        port, nowport = session_ports(config)
        sessions.append({
            "tmp_id": tmp_id,
            "port": port,
            "nowport": nowport,
            "path": str(entry),
            "has_dll": (entry / PIPE_DLL_NAME).is_file(),
        })
    # Newest first: the session just started is the one being looked at.
    sessions.sort(key=lambda item: Path(item["path"]).stat().st_mtime, reverse=True)
    return sessions


def rotate_pipe_events(root: Path | str = ROOT_NAME, *, max_bytes: int = 16 * 1024 * 1024) -> bool:
    """Rename the event stream away when it has grown too large. Returns True when it rotated.

    Nothing pruned this file: every session appends to it for as long as it runs, and an account under test had
    it at 9 MB after a single day. It is a stream, read forward by offset, not a record store -- so the useful
    part is always the recent end, exactly like the task queue.

    Only safe while nothing is writing, which is why the caller runs it at start-up. Renaming a file another
    process has open for append either fails or leaves that process writing into the renamed handle, and both are
    worse than a big log.
    """
    path = Path(root) / PIPE_INSTANCE_ID / "owndata" / "events.jsonl"
    try:
        if not path.is_file() or path.stat().st_size < max_bytes:
            return False
        rotated = path.with_suffix(".jsonl.1")
        # One generation only: a second rotation replaces the first, so this cannot grow without bound either.
        rotated.unlink(missing_ok=True)
        path.rename(rotated)
        return True
    except OSError:
        return False


def pipe_event_offset(root: Path | str = ROOT_NAME) -> int:
    """Current end of the pipe event stream, in bytes.

    Used to start the reader at "now" rather than at the beginning of the file.

    The stream is append-only and outlives QSICK, so on startup it contains everything the sessions ever
    reported. Replaying that is not harmless: a message written while QSICK was down is *proof that an account
    was logged in then*, not that it is logged in now — and acting on it opened a network config for a session
    that no longer exists. Measured: an app started with no QQ process at all still activated a reverse
    connection, because the first replayed line named an account.

    Messages are still durable: the ones delivered while QSICK was running are in the store, and the ones it
    never saw belong to a session that has since gone away.
    """
    path = Path(root) / PIPE_INSTANCE_ID / "owndata" / "events.jsonl"
    try:
        return int(path.stat().st_size)
    except OSError:
        return 0


def read_pipe_events(root: Path | str = ROOT_NAME,
                     offsets: dict[str, int] | None = None) -> dict[str, Any]:
    """Read new lines from every session's event stream, advancing `offsets` in place.

    Each session appends to `<root>/Pipe/dll/<tmp_id>/events.jsonl` -- its **own** file. The stream used to be one
    shared `owndata/events.jsonl` carrying a `uin` per line, which relied on every process reporting its own
    account correctly. It does not hold up: with two sessions live, a message one of them sent was recorded
    against the other, and one event landed in both accounts' stores. A file per session removes the question --
    which session wrote a line is known from the path, not from a field inside it.

    A session that has not been migrated still writes to `owndata/events.jsonl`; that stream is read too, so an
    older launch is not silently ignored.

    Tail-style rather than read-everything: these files grow for as long as QSICK runs. One byte offset is kept per
    stream, keyed by its path, so adding or removing a session does not disturb the others.

    A partially written line at the end is left for the next call: the writer may be mid-append, and parsing half
    a JSON object would drop a message on every poll.

    Returns `{"events": [...], "error": str}`.
    """
    state = offsets if offsets is not None else {}
    root_path = Path(root)
    streams: list[Path] = []
    session_root = session_dll_dir(root_path)
    if session_root.is_dir():
        streams.extend(sorted(session_root.glob("*/events.jsonl")))
    legacy = root_path / PIPE_INSTANCE_ID / "owndata" / "events.jsonl"
    if legacy.is_file():
        streams.append(legacy)

    events: list[dict[str, Any]] = []
    errors: list[str] = []
    for stream in streams:
        result = _read_one_stream(stream, state)
        events.extend(result["events"])
        if result["error"]:
            errors.append(result["error"])
    return {"events": events, "error": "; ".join(errors)}


def _read_one_stream(path: Path, state: dict[str, int]) -> dict[str, Any]:
    """Advance one stream's offset and return the whole lines it gained."""
    key = str(path)
    start = int(state.get(key) or 0)
    try:
        size = path.stat().st_size
    except OSError as exc:
        return {"events": [], "error": f"{type(exc).__name__}: {exc}"}
    if size < start:
        # Truncated or replaced: start over rather than seeking past the end.
        start = 0
    if size == start:
        state[key] = start
        return {"events": [], "error": ""}
    events: list[dict[str, Any]] = []
    consumed = start
    try:
        with path.open("rb") as handle:
            handle.seek(start)
            for raw in handle:
                if not raw.endswith(b"\n"):
                    # Half a line: the writer has not finished. Leave it for next time.
                    break
                consumed += len(raw)
                try:
                    item = json.loads(raw.decode("utf-8", "replace"))
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict):
                    events.append(item)
    except OSError as exc:
        return {"events": events, "error": f"{type(exc).__name__}: {exc}"}
    state[key] = consumed
    return {"events": events, "error": ""}


def probe_session_health(port: int, timeout: float = 1.5) -> dict[str, Any]:
    """Ask a session's DLL whether it is up, by GET /health on its port.

    The DLL serves `/health` with `{"status":"ok","transport":"pipe"}`, and it only does so while the
    process is alive and has bound its port. So the answer is the state: a reply means running, and a
    refused connection means the session is not started.

    This replaces guessing from a status file. A file records what was true when it was written -- a killed
    session keeps its last heartbeat -- whereas a connection either succeeds now or does not.

    Every failure mode is "not running", because from the caller's side they are indistinguishable and the
    only action is the same: a connection refused means no process, a timeout means the port is bound by
    something that is not answering as this DLL, and a pool of other socket errors means much the same.

    Returns `{"running": bool, "detail": str}`; `detail` carries the reply so a caller can log it.
    """
    if port <= 0:
        return {"running": False, "detail": "没有端口"}
    url = f"http://127.0.0.1:{int(port)}/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        # WinError 10061 (connection refused) is the ordinary case for a stopped session, not an error
        # worth surfacing differently from any other failure to reach it.
        return {"running": False, "detail": str(reason)}
    except (OSError, ValueError) as exc:
        return {"running": False, "detail": f"{type(exc).__name__}: {exc}"}
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return {"running": False, "detail": f"响应不是 JSON: {body[:80]}"}
    if not isinstance(payload, dict):
        return {"running": False, "detail": f"响应不是对象: {body[:80]}"}
    # `status: ok` is what distinguishes our DLL from some unrelated listener that happens to own the port.
    running = str(payload.get("status") or "") == "ok"
    return {"running": running, "detail": body[:120]}


def probe_session_login(port: int, timeout: float = 5.0) -> dict[str, Any]:
    """Ask one session's DLL who it is logged in as, via `POST /action {op: login.info}`.

    This is the account answer, and the running answer at the same time. The DLL builds its action
    dispatcher with a **live** identity source (`qsick_http.cpp`: `getIdentity` returns the session's own
    uin), so a successful reply describes *this process*, not a shared file. Every session has its own port,
    so the port is the identity: nothing has to be written anywhere to tell sessions apart.

    What each reply means:

        connection refused   -> the session is not running
        timeout / socket error -> also "not running"; from here it is indistinguishable and the only action
                                is the same
        ok, data with a uin  -> running and logged in, and able to serve
        ok: false            -> running but not logged in *yet* (the DLL answered, so the process is up; the
                                dispatcher refuses `login.info` until a session exists -- and, since the
                                identity carries `ready`, until the kernel message listener is attached)

    "Logged in" here is deliberately the strict reading. A session restored from stored credentials knows its
    uin about two seconds before its message listener is attached, and taking that earlier answer made the
    network config open while the session still received nothing.

    The timeout is generous on purpose. `login.info` runs on QQ's JS thread, which also drains the action queue;
    measured under load it can take a couple of seconds to answer. A 2 s budget turned that into "not logged
    in", which read as a logged-in session flickering between states -- and the flapping got worse with more
    sessions because they share that thread's attention. A session that is really gone fails the connect test
    below in 50 ms, so nothing slow is paid for a stopped one.

    The refused case is deliberately not an exception. A stopped session is ordinary -- it is what "已关闭"
    means -- and turning it into an error would make every list refresh look like a failure.
    """
    if port <= 0:
        # `answered: False` marks "we never got to ask", which is not the same as "it said no". The two are
        # flattened into `logged_in: False` for callers that only want a boolean, and `answered` is what lets
        # the session list avoid presenting a missed probe as a logged-out session.
        return {"running": False, "closed": True, "logged_in": False, "answered": False,
                "uin": "", "nickname": "", "detail": "没有端口"}
    # Cheap connect test before the HTTP round trip.
    #
    # Measured on this machine: connecting a TCP socket to a closed loopback port does not get refused, it
    # waits out whatever timeout it was given (50 ms -> 56 ms, 250 ms -> 250 ms). So the budget *is* the cost
    # of a closed session, and it has to stay small: the session list is polled every few seconds. 200 ms is
    # enough for a listening socket to accept on loopback even when the process is busy.
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=0.2):
            pass
    except ConnectionRefusedError as exc:
        # Nothing is listening. That is a real answer about the process, not a failed question.
        return {"running": False, "closed": True, "logged_in": False, "answered": True,
                "uin": "", "nickname": "", "detail": f"连接失败: {exc}"}
    except OSError as exc:
        # A timeout here is *not* the same thing as "went away". On Windows a closed loopback port does not get
        # refused, it waits out the budget -- and a session under load can also miss a 200 ms connect. So the
        # answer is reported unusable (`answered: False`) while still saying nothing is reachable (`closed`):
        # callers that only want "is it there" keep working, and callers that need certainty do not act on it.
        return {"running": False, "closed": True, "logged_in": False, "answered": False,
                "uin": "", "nickname": "", "detail": f"连接超时: {exc}"}
    body = json.dumps({"op": "login.info", "params": {}}).encode("utf-8")
    request = urllib.request.Request(f"http://127.0.0.1:{int(port)}/action", data=body,
                                     headers={"Content-Type": "application/json"}, method="POST")
    # Bypass any system proxy: this is a loopback call, and a proxy would turn a plain "not running" into a
    # confusing upstream error.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:  # noqa: S310 - fixed loopback
            raw = response.read(256 * 1024).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        # 503 is the DLL saying "this instance could not serve the request in time" -- the JS thread was busy,
        # which says nothing about the account. Marked unanswered so the caller keeps the last known state
        # instead of showing 未登录 for one poll.
        body = ""
        try:
            body = exc.read(512).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - the status alone is enough
            body = ""
        if exc.code == 503 or "did not answer in time" in body:
            return {"running": True, "closed": False, "logged_in": False, "answered": False,
                    "uin": "", "nickname": "", "detail": body[:120] or "队列繁忙"}
        return {"running": True, "closed": False, "logged_in": False, "answered": False,
                "uin": "", "nickname": "", "detail": f"HTTP {exc.code}: {body[:100]}"}
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        # A timeout means the session was listening but did not answer in time -- under load it shares one busy
        # JS thread with the action queue. That says nothing about the account, so it is marked unanswered and
        # the caller keeps the last known answer instead of showing 未登录.
        timed_out = isinstance(reason, (TimeoutError, socket.timeout)) or "timed out" in str(reason).lower()
        return {"running": False, "closed": not timed_out, "logged_in": False,
                "answered": not timed_out,
                "uin": "", "nickname": "", "detail": str(reason)}
    except (OSError, ValueError) as exc:
        return {"running": False, "closed": True, "logged_in": False, "answered": True,
                "uin": "", "nickname": "", "detail": f"{type(exc).__name__}: {exc}"}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        # Something answered but it was not this DLL. Nothing can be concluded about the session.
        return {"running": False, "closed": False, "logged_in": False, "answered": False,
                "uin": "", "nickname": "", "detail": f"响应不是 JSON: {raw[:80]}"}
    if not isinstance(payload, dict):
        return {"running": False, "closed": False, "logged_in": False, "answered": False,
                "uin": "", "nickname": "", "detail": f"响应不是对象: {raw[:80]}"}
    if payload.get("ok") is not True:
        # The DLL answered, so the process is alive; it just has no session yet.
        #
        # One exception: a DLL from before the 503 change reported a task-queue timeout this same way. Without
        # the check it reads as "said no", which is the flicker this function exists to avoid.
        error = str(payload.get("error") or "未登录")
        if "did not answer in time" in error:
            return {"running": True, "closed": False, "logged_in": False, "answered": False,
                    "uin": "", "nickname": "", "detail": error[:160]}
        return {"running": True, "closed": False, "logged_in": False, "answered": True,
                "uin": "", "nickname": "", "detail": error[:160]}
    data = payload.get("data")
    data = data if isinstance(data, dict) else {}
    uin = str(data.get("uin") or data.get("user_id") or "").strip()
    # `"0"` is this codebase's sentinel for "no account" -- LoginState uses it, and the DLL reports it
    # before a session exists. Treating it as a uin would show a row for account zero.
    if not (uin.isdigit() and uin != "0" and len(uin) <= 20):
        return {"running": True, "closed": False, "logged_in": False, "answered": True,
                "uin": "", "nickname": "", "detail": "登录信息里没有 uin"}
    return {"running": True, "closed": False, "logged_in": True, "answered": True, "uin": uin,
            "nickname": str(data.get("nickname") or ""), "detail": ""}


def kill_session_process(root: Path | str, tmp_id: str) -> bool:
    """Kill one session's process and leave its directory in place.

    Closing a session is about stopping the process. The directory is the session's identity and holds the
    port the operator chose, so keeping it means the same session can be started again on the same port
    instead of becoming a new one with a new number.

    `nowport` is cleared afterwards, because nothing is bound once the process is gone. That is the other
    half of the alignment: on start `nowport` follows `port`, on close it goes back to 0, and in between the
    two values may differ legitimately.

    The process is found by `nowport` -- not `port` -- because a live session listens on the number it
    actually bound. After an edit those differ, and killing by `port` would miss the process entirely and
    find nothing to stop.

    Returns whether anything was actually killed, so a caller can say "it was already stopped" rather than
    implying an action that did not happen.
    """
    directory = session_dll_dir(root) / str(tmp_id)
    if not directory.is_dir():
        return False
    port, nowport = session_ports(read_session_config(tmp_id, root))
    killed = False
    for candidate in (nowport, port):
        if candidate <= 0:
            continue
        for pid in pids_on_port(candidate):
            kill_pid(pid)
            killed = True
        if killed:
            break
    # `nowport` is deliberately left alone here.
    #
    # It is what a running session actually bound, and it is the only handle a caller has for asking that
    # session to shut down -- the `/quit` request needs the port the process is listening on, and the state
    # link needs it to know it is talking to the right process. Clearing it inside the kill meant the caller
    # asked first and then lost the number it had asked with, which is why the graceful path never landed.
    #
    # Settling it is the close flow's job, after the session has said goodbye (see the close endpoint): align
    # `nowport` with `port` once it is confirmed stopped.
    return killed


def close_session(root: Path | str, tmp_id: str) -> bool:
    """Kill one session's process and delete its directory.

    This is the destructive one: the directory is the session, so removing it removes the port assignment
    with it.

    Deleting a *running* session is the case that needs care. `taskkill` returns once the process is told to
    die, not once Windows has released its file handles, and the injected `qsick_pipe.dll` is mapped into
    that process. Removing the directory immediately therefore often failed on a locked DLL -- and because
    the failure was swallowed, what remained was a directory with no config, which the UI rendered as
    "配置缺失" until the operator pressed delete a second time.

    So: kill, then retry the removal for a few seconds while the handles drain, and report whether the
    directory is really gone instead of assuming it.
    """
    directory = session_dll_dir(root) / str(tmp_id)
    if not directory.is_dir():
        return False
    kill_session_process(root, tmp_id)
    for attempt in range(20):
        try:
            shutil.rmtree(directory)
            return True
        except FileNotFoundError:
            return True
        except OSError:
            # A mapped DLL or a still-open log file. Give the process time to exit and try again; the
            # first couple of attempts are the ones that usually fail.
            if attempt < 19:
                time.sleep(0.25)
    # Still there after ~5 s: something still holds the DLL. Report it rather than returning success, so the
    # UI can say "被占用，稍后再试" instead of showing a row that silently survives.
    return not directory.is_dir()


def pids_on_port(port: int) -> list[int]:
    """Pids listening on a TCP port, from `netstat -ano`.

    Module-level rather than only a method because closing a session happens without a manager instance:
    the session list is read from disk, so the close path should not have to build one.

    Failure returns an empty list rather than raising. Every caller treats "no listeners" as a normal
    state -- a stopped session has none -- so an unavailable netstat must not look like an error.
    """
    if not port:
        return []
    try:
        result = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, timeout=5,
                                check=False,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError, TypeError, ValueError):
        return []
    pids: list[int] = []
    for line in (result.stdout or "").splitlines():
        if f":{port} " in line and "LISTENING" in line.upper():
            match = re.search(r"(\d+)\s*$", line)
            if match:
                pids.append(int(match.group(1)))
    return pids


def kill_pid(pid: int) -> None:
    """Terminate a process and its children, ignoring failure.

    `/T` matters here: QQ spawns helper processes, and killing only the parent leaves them holding the
    DLL and the port.
    """
    if pid <= 0:
        return
    if os.name == "nt":
        creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            creationflags=creation,
            check=False,
        )
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass


def port_is_free(port: int) -> bool:
    """Whether nothing is listening on ``port`` right now.

    Used only as a hint when handing out a port: between this check and the DLL binding there is a
    window, and another process can take the port. The real defence is that a failed bind is reported
    back from the injected side, which the caller surfaces -- this just avoids picking an obviously
    taken port in the common case.
    """
    if port <= 0:
        return False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def allocate_pipe_port(taken: set[int] | None = None) -> int:
    """Pick the next free port for a pipe session, starting at ``PIPE_PORT_BASE``.

    ``taken`` holds ports already promised to other sessions in the same pass. It matters because a
    session that has been created but not yet started is not listening, so its port still looks free --
    without this, two sessions created back to back would both be offered 7540.

    Returns 0 when the range is exhausted, which callers treat as "let the DLL use its default" rather
    than failing the creation outright.
    """
    reserved = taken or set()
    for candidate in range(PIPE_PORT_BASE, PIPE_PORT_MAX + 1):
        if candidate in reserved:
            continue
        if port_is_free(candidate):
            return candidate
    return 0


#: Hot-reloaded map of session id -> port, kept at the project root.
#:
#: The operator wants to be able to pin a session's DLL port without going through the UI, so this file
#: is read on every start. It is deliberately *not* a second source of truth: the instance config holds
#: the port that was actually used, and this only overrides it when it names that session.
#:
#: It is emptied at QSICK startup, on the stated requirement that the file is scratch space for the
#: current run rather than durable state. That means an override has to be written after QSICK starts,
#: which is what the UI does -- editing it beforehand has no effect by design.
PORT_OVERRIDE_FILE = "session_ports.json"


def read_port_override(root: Path | str = ROOT_NAME) -> dict[str, int]:
    """Read the session->port override map, ignoring anything malformed.

    A bad file must not stop sessions from starting: a wrong or half-written override is worth less
    than a working default, so unparsable content is treated as no override at all.
    """
    path = Path(root) / PORT_OVERRIDE_FILE
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    result: dict[str, int] = {}
    for key, value in payload.items():
        try:
            port = int(value)
        except (TypeError, ValueError):
            continue
        if PIPE_PORT_BASE <= port <= PIPE_PORT_MAX:
            result[str(key)] = port
    return result


def write_port_override(overrides: dict[str, int], root: Path | str = ROOT_NAME) -> None:
    """Write the override map. Missing keys mean "allocate normally"."""
    path = Path(root) / PORT_OVERRIDE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    cleaned = {str(k): int(v) for k, v in (overrides or {}).items()
               if int(v) > 0}
    path.write_text(json.dumps(cleaned, ensure_ascii=False, indent=2), encoding="utf-8")


def reset_port_override(root: Path | str = ROOT_NAME) -> None:
    """Empty the override map. Called once at QSICK startup.

    The operator asked for the file to be reset on every start so it never accumulates stale pins from
    a previous run. Writing `{}` rather than deleting keeps the file present for anyone editing it by
    hand, which is friendlier than a file that appears and disappears.
    """
    path = Path(root) / PORT_OVERRIDE_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
    except OSError:
        pass


def host_shim() -> Path | None:
    env = os.environ.get("QSICK_HOST_DLL")
    if env and Path(env).is_file():
        return Path(env)
    candidate = REPO_ROOT / "build" / "QQNT.dll"
    return candidate if candidate.is_file() else None


#: pipe 实例注入的 DLL 与它读取的配置文件名。
PIPE_DLL_NAME = "qsick_pipe.dll"
PIPE_DLL_CONFIG = "qsick_pipe.json"

#: pipe 实例要用的 JS 模块，随实例拷贝到客户端 resources/app。
#:
#: 注入脚本在运行时按 wrapper.node 的位置去找这些模块，所以它们必须真的躺在那里。
#: 之前靠手工拷贝，重启即丢，表现为 "dispatcher unavailable"——代码没问题，文件不在。
PIPE_JS_MODULES = (
    "headless_codec.cjs",
    "headless_actions.cjs",
    "headless_oidb.cjs",
    "headless_sender.cjs",
    "headless_media.cjs",
    # Group file download builds OIDB packets; without this the op reports the module as unavailable.
    "headless_groupfile.cjs",
)

#: `headless_groupfile.cjs` 需要的原生插件（QQ 传输层的钩子模块）。
#:
#: 它是编译产物，源码不在仓库里。必须和 .cjs 一起放到实例的 ``resources/app``，因为 cjs 用 ``__dirname``
#: 找它——放在别处会因为实例目录和仓库目录不同而读不到，表现为群文件下载报 "native hook unavailable"。
INITHOOK_MODULE = "inithook.node"


def inithook_module() -> Path | None:
    """返回该原生插件；文件不存在就返回 None（群文件下载会报 NO_BUFFER，其他功能不受影响）。"""
    candidate = REPO_ROOT / "native" / "inithook" / INITHOOK_MODULE
    return candidate if candidate.is_file() else None


def pipe_dll() -> Path | None:
    """返回要注入 pipe 实例的 DLL。

    ``QSICK_PIPE_DLL`` 可以指向别处（开发和调试时有用）；否则用仓库里构建好的
    那份。放在 ``native/qsick-pipe/`` 而不是 ``build/``：``build/`` 里是模拟
    QQNT.dll 的垫片，两者用途完全不同，混在一起容易拿错。
    """
    env = os.environ.get("QSICK_PIPE_DLL")
    if env and Path(env).is_file():
        return Path(env)
    candidate = REPO_ROOT / "native" / "qsick-pipe" / PIPE_DLL_NAME
    return candidate if candidate.is_file() else None


@dataclass
class _PipeInjection:
    """一次 pipe 启动的结果：进程句柄 + 注入结果 + 生命周期守卫。

    ``job`` 是把这棵进程树绑在 QSICK 身上的 Job Object。它必须活到实例停止为止，
    所以挂在这里由调用方持有——被回收就等于关掉内核句柄，QQ 会被立刻终止。
    """

    process: subprocess.Popen
    injection: Any
    module_base: int | None
    job: Any = None


def _inject_into(pid: int, payload: Path) -> int | None:
    """把 ``payload`` 注入到 ``pid``，返回模块基址。

    单独抽出来是为了让测试能替换掉它：注入需要真实的 Windows 进程句柄，
    而测试里的进程是假的（``Popen`` 被替身接管），没法真的注入。
    """
    global _last_injection
    from .transport import inject as injector

    if os.name != "nt":
        return None

    handle = None
    deadline = time.time() + 5.0
    while time.time() < deadline:
        handle = injector.open_process(pid)
        if handle:
            break
        time.sleep(0.005)
    if not handle:
        raise RuntimeError("无法获取 QQ.exe 进程句柄")

    injection = injector.Injection(pid=pid, hprocess=int(handle), hthread=0)
    last_error: Exception | None = None
    # Bounded retries.
    #
    # 60 attempts at 20 ms is 1.2 s of retrying plus whatever each attempt costs, and with the
    # process frozen a failing attempt can block for longer than the sleep. Measured: the call
    # appeared hung at the "frozen, injecting" log line and never returned, which left QQ
    # suspended with no DLL and no way to recover short of Task Manager.
    #
    # A wall-clock deadline is the honest bound here: after it, give up and let the caller
    # resume the process and carry on without the payload.
    deadline = time.time() + 3.0
    attempts = 0
    while time.time() < deadline:
        attempts += 1
        try:
            base = injector.inject(injection, payload)
            # Keep the handle reachable so the caller can re-enumerate modules after resuming.
            # The Injection object owns it, but discarding that object here would lose the one
            # handle needed for the deferred check.
            global _last_injection
            _last_injection = injection
            return base
        except injector.InjectError as exc:
            last_error = exc
            time.sleep(0.02)
    raise RuntimeError(
        f"注入 {payload.name} 失败（{attempts} 次尝试，超过 3 秒）：{last_error}")


# Handle from the most recent injection, used for the post-resume module check.
_last_injection = None


class _LaunchedProcess:
    """The pid of a process the injector created.

    `transport.inject.launch_and_inject` builds the process with `CreateProcessW` and hands back an
    `Injection`, not a `subprocess.Popen`. Callers only ever need the pid, so this stands in for it.
    """

    def __init__(self, pid: int) -> None:
        self.pid = pid


def _command_line_arguments(arguments: list[str]) -> str:
    """Render arguments for `CreateProcessW`, quoting any value that contains a space.

    `transport.inject.launch_suspended` splices the string into the command line verbatim. `subprocess.Popen`
    used to do this quoting; going through the injector means doing it here -- otherwise a user-data path such
    as ``{QQ 文件目录}\\Tencent Files`` arrives split across several arguments and QQ gets a truncated
    path.
    """
    parts: list[str] = []
    for item in arguments:
        if " " in item and not item.startswith('"'):
            name, sep, value = item.partition("=")
            item = f'{name}="{value}"' if sep else f'"{item}"'
        parts.append(item)
    return " ".join(parts)


def _spawn_and_inject(exe: Path, payload: Path, arguments: list[str],
                      workdir: Path, instance_id: str = "") -> _PipeInjection:
    """Launch ``exe`` suspended, inject ``payload``, bind it to a job, then let it run.

    ## Why suspended, after all

    An earlier version started the client normally, froze every thread, injected, then resumed. Same idea, one
    step too late: by the time the freeze lands, QQ has already walked its napi registration, so the DLL's
    import patches go in after the burst they need to observe. The symptom is subtle -- the client looks fine
    and the HTTP channel answers, but the injected script never gets a JS environment, so every action times
    out. Measured: with the frozen path ``usable env on JS thread`` never appeared; with ``CREATE_SUSPENDED``
    it appears every time.

    The note that used to be here said suspended launches died after about 12 s with ``0x80000003``, even with
    a blank DLL. That was a misdiagnosis. The real cause was the ACL on the instance directory, which also
    produced "at most two processes, no window rendered, no crash dialog". With the ACL fixed the suspended
    path runs clean, and it is the only one that reliably catches the napi burst.

    ## Job Object, before resume

    ``before_resume`` is the only window in which no child process exists yet. QQ spawns its renderers within
    milliseconds and children inherit the job at creation, so binding any later leaves them outside it -- and
    then closing QSICK kills only the main process, which is the very symptom the job exists to prevent.

    ## Environment

    ``launch_suspended`` cannot pass a custom environment block (CPython 3.13 returns ERROR_INVALID_PARAMETER
    for any non-NULL ``lpEnvironment``), so the child inherits ours. The only thing that costs is the
    per-instance TEMP redirect: APPDATA and the rest were already meant to be the real values, and QQ's own
    data root comes from ``UserDataInfo.ini`` rather than from the environment.

    ## Arguments

    ``arguments`` arrives already split, and ``launch_suspended`` splices the joined string into the command
    line, so values containing spaces are re-quoted here -- see ``_command_line_arguments``.
    """
    from .job_guard import acquire
    from .transport import inject as injector

    # Registered process-wide, keyed by instance: a guard owned by this frame would be collected on return and
    # kill QQ immediately, and one owned by the manager would die with the WebUI request that created it.
    job = acquire(instance_id or str(workdir))

    def bind_job(injection) -> None:
        if not job.active:
            log.warn("Job Object 不可用，实例可能不会随 QSICK 退出即关闭",
                     instance=instance_id, error=job.error)
            return
        if not job.assign(injection.pid):
            log.warn("无法把 QQ 进程加入 Job Object，实例可能不会随 QSICK 退出即关闭",
                     instance=instance_id, pid=injection.pid, error=job.error)

    try:
        injection = injector.launch_and_inject(
            exe, payload,
            arguments=_command_line_arguments(arguments),
            workdir=workdir,
            before_resume=bind_job,
        )
    except Exception as exc:  # noqa: BLE001 - 失败要带出可读原因
        raise RuntimeError(f"启动 QQ.exe 并注入失败：{exc}") from exc

    base = int(getattr(injection, "module_base", 0) or 0)
    if not base:
        # Enumeration can miss a module whose loader registration has not finished. Now that the process is
        # running, re-check rather than reporting a working instance as broken.
        deadline = time.time() + 3.0
        while time.time() < deadline:
            found = injector.find_module(injection.hprocess, payload.name)
            if found:
                base = int(found[0])
                injection.module_base = base
                break
            time.sleep(0.05)

    if base:
        log.info("已注入 pipe DLL", pid=injection.pid, base=hex(base))
    else:
        log.warn("注入后仍未确认模块基址，实例可能不带 pipe 能力", pid=injection.pid)

    return _PipeInjection(_LaunchedProcess(injection.pid), injection, base or None, job)


def _isolated_env(workdir: Path) -> dict[str, str]:
    """pipe 实例的环境变量：数据目录交给 QQ 自己的配置，不再隔离。

    ## 为什么改回系统配置

    早先这里把 ``APPDATA`` / ``LOCALAPPDATA`` / ``TEMP`` 指向实例的 owndata，
    DLL 也钩了 ``SHGetKnownFolderPath`` 重定向"文档"目录，目的是让 QQ 的数据落在
    实例里。实测两个手段都没达到目的：

    * 钩子报告生效（``g_qsickDataDirHooked == 1``），但 QQ 仍往真实用户目录写；
    * 实例启动后稳定弹出"个人文件夹保存位置无效"，指名
      ``{QQ 文件目录}\\Tencent Files``。

    逆向报告 ``reports/06-安装目录与数据目录.md`` 给了完整答案：QQ 的业务数据根
    **不是**从文档目录推出来的，而是读

        <CSIDL_COMMON_DOCUMENTS>\\Tencent\\QQ\\UserDataInfo.ini
            [UserDataSet] UserDataSavePath

    即**公共文档**下的那份 INI（本机为 ``{QQ 文件目录}\\Tencent Files``）。
    文档目录只是该配置缺失时的兜底。所以重定向文档改的是永不执行的那条分支，
    而真正的配置仍指向共享目录——QQ 请求那个路径、我们的钩子让它看起来不存在，
    于是 QQ 拒绝继续。那个弹窗就是这么来的。

    现在按 QQ 自己的方式取数据目录：环境变量一律用系统的，实例创建时给该目录
    授权（见 ``_grant_instance_data_access``）。QQ 客户端本身支持多开，
    共用同一个配置好的数据根是它设计内的用法。

    ``TEMP`` 仍指向实例：临时文件不属于账号数据，放实例里便于清理，
    也不会和真实客户端互相干扰。
    """
    env = dict(os.environ)
    data_root = workdir / "owndata"
    temp = data_root / "temp"
    temp.mkdir(parents=True, exist_ok=True)
    env["TEMP"] = str(temp)
    env["TMP"] = str(temp)
    # APPDATA / LOCALAPPDATA / USERPROFILE 全部保持真实值：QQ 从系统配置取数据根，
    # 改写它们只会让 Electron 解析出它自己之后拒绝的路径。
    return env


def qq_data_root() -> Path | None:
    """读出 QQ 自己配置的业务数据根。

    机制来自 ``reports/06-安装目录与数据目录.md``：``wrapper.node`` 读

        <CSIDL_COMMON_DOCUMENTS>\\Tencent\\QQ\\UserDataInfo.ini
            [UserDataSet] UserDataSavePath

    即**公共文档**下的 INI，不是用户文档目录，也不是 %APPDATA%。

    可以用 ``QSICK_QQ_DATA_ROOT`` 覆盖。这不是给测试开后门，而是必要的能力：
    数据根是"用户机器上的配置"，QSICK 不该把它写死；用户可能想换到另一个盘，
    测试也需要在不碰真实账号目录的前提下验证授权逻辑。

    读不到配置时返回 ``None`` —— 调用方应当把它当作"按 QQ 的兜底逻辑走"，
    而不是猜一个路径。这个函数只读，不改任何东西。
    """
    override = os.environ.get("QSICK_QQ_DATA_ROOT")
    if override:
        return Path(override)

    common = os.environ.get("PUBLIC")
    if not common:
        # CSIDL_COMMON_DOCUMENTS 在 Windows 上就是 %PUBLIC%\\Documents。
        common = r"C:\Users\Public"
    ini = Path(common) / "Documents" / "Tencent" / "QQ" / "UserDataInfo.ini"
    if not ini.is_file():
        return None
    try:
        raw = ini.read_text(encoding="mbcs", errors="replace")
    except OSError:
        return None
    section = ""
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith((";", "#")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
            continue
        if section != "userdataset" or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip().lower() == "userdatasavepath":
            value = value.strip().strip('"')
            if value:
                return Path(value)
    return None


def _grant_instance_data_access(data_root: Path) -> bool:
    """给当前用户授予数据根的写权限，已可写时什么也不做。

    为什么需要：pipe 实例按 QQ 自己的配置使用同一个数据根（``reports/06``），
    而 QQ 在写入前会检查该目录是否授权。实测本机 ``Authenticated Users`` 已有
    Modify，所以正常情况这里是空操作；但换一台机器、或目录是管理员创建的，
    就可能缺权限——那正是"您未授权 QQ 在此位置写入数据"的来源。

    只在**创建实例**时调用，不在每次启动时调用：授权是配置行为，不该藏在启动
    路径里。失败不抛异常，返回 False 让调用方决定要不要提示用户。
    """
    if os.name != "nt":
        return True
    try:
        data_root.mkdir(parents=True, exist_ok=True)
        probe = data_root / ".qsick_write_test"
        try:
            probe.write_text("x", encoding="utf-8")
            probe.unlink()
            return True
        except OSError:
            pass
        # 不可写才动 ACL。icacls 比直接操作安全描述符更不容易把 ACL 弄坏，
        # 而且失败时给出的错误可读。
        user = os.environ.get("USERNAME") or ""
        if not user:
            return False
        completed = subprocess.run(
            ["icacls", str(data_root), "/grant",
             f"{user}:(OI)(CI)M", "/T", "/C", "/Q"],
            capture_output=True, text=True, timeout=180)
        return completed.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def default_runtime_dir() -> Path:
    """QQ 客户端副本的位置：优先项目根 ``runtime/``。

    历史上它在 ``headlessClient/runtime``（实例目录旁边）。现在挪到项目根，理由：

    * 所有实例共用同一份，放在实例容器里名不副实；
    * ``headlessClient/`` 整个被 .gitignore 排除，那是 QQ 登录数据——把 runtime
      混在里面，既不好单独备份，也让"客户端"和"账号数据"混为一谈。

    仍兼容旧位置：如果新的不存在而旧的还在，就用旧的，免得用户迁移时白下载一次。
    """
    env = os.environ.get("QSICK_RUNTIME_DIR")
    if env:
        return Path(env)
    preferred = REPO_ROOT / "runtime"
    legacy = REPO_ROOT / ROOT_NAME / "runtime"
    if preferred.is_dir() or not legacy.is_dir():
        return preferred
    return legacy


#: 自动重登的等待档位（秒）。由用户在下拉里选，落盘到 instance.json。
#:
#: 为什么给下限 5 秒而不是更短：被踢之后立刻重登会让腾讯侧看到"同一账号秒级
#: 反复登录"，那本身就是风控特征。5 秒是"让上一步真的完成"的量级，不是惩罚。
#: 档位上限给到 4 小时，因为账号被风控时，快重登只会加深标记，等久一点反而
#: 更容易成功。
RELOGIN_DELAY_CHOICES: tuple[int, ...] = (
    5,          # 5 秒
    60,         # 1 分钟
    600,        # 10 分钟
    1800,       # 30 分钟
    3600,       # 1 小时
    4 * 3600,   # 4 小时
)

#: 没有显式配置时用的档位。
DEFAULT_RELOGIN_DELAY = 600

#: 界面上的档位标签。
RELOGIN_DELAY_LABELS: dict[int, str] = {
    5: "5 秒",
    60: "1 分钟",
    600: "10 分钟",
    1800: "30 分钟",
    3600: "1 小时",
    4 * 3600: "4 小时",
}


def _normalize_delay(value: Any) -> int:
    """把落盘值收敛到一个合法的重登档位，坏数据不该让重登失效。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return DEFAULT_RELOGIN_DELAY
    return number if number in RELOGIN_DELAY_CHOICES else DEFAULT_RELOGIN_DELAY


#: 实例类型。老实例的 instance.json 里没有这个字段，一律按 headless 处理——
#: 它们本来就是那么建的，不能因为升级就把已有实例判成别的类型。
INSTANCE_KINDS: tuple[str, ...] = ("headless", "pipe")


def _normalize_kind(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text in INSTANCE_KINDS else "headless"


def _retry_remaining(instance_id: str, delay_seconds: int | None = None) -> float | None:
    """距离自动重登还有多少秒。

    以"首次观测到被踢"为起点，用用户选的档位减去已等待时间。返回 ``None`` 表示
    "不会再有下一次"（已熔断），而不是 ``inf``：``inf`` 传进模板既不能整除也不
    好显示，索性在这里就收敛成可判定的形状。
    """
    from .backoff import BOOK, KICK_CLOCK

    if BOOK.entry(instance_id).exhausted:
        return None
    if delay_seconds is None:
        return 0.0
    remaining = KICK_CLOCK.remaining(instance_id, delay_seconds)
    if remaining is None:
        return 0.0
    return round(remaining, 1)


def _retry_exhausted(instance_id: str) -> bool:
    """连续失败过多，已经停止自动重试。"""
    from .backoff import BOOK

    return BOOK.entry(instance_id).exhausted


@dataclass(slots=True)
class Instance:
    id: str
    path: Path
    qq: str = ""
    avatar: str = ""
    port: int = 0
    pid: int = 0
    auto_relogin: bool = False
    #: 被踢后等待多久才重登（秒）。只有 ``auto_relogin`` 打开时才起作用。
    auto_relogin_delay: int = DEFAULT_RELOGIN_DELAY
    #: QSICK 启动时是否自动拉起这个实例。
    #:
    #: 默认关闭：启动就拉起全部实例会让每个账号立刻占用内存和网络，而多数时候
    #: 用户只想用其中一个。由账户管理页的开关逐实例决定。
    auto_start: bool = False
    #: 实例显示名。缺省用 id，用户可以起一个认得出的名字（不可重复）。
    name: str = ""
    #: ``headless``（无壳，Node 直接加载 wrapper.node）或 ``pipe``（真客户端 + 注入）。
    kind: str = "headless"

    def status(self) -> dict[str, Any]:
        status_path = self.path / "data" / "status.json"
        if not status_path.is_file():
            return {}
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if self.kind == "pipe":
            self._merge_pipe_login(status)
        return status

    def _merge_pipe_login(self, status: dict[str, Any]) -> None:
        """把注入 DLL 写出的登录状态并入 status。

        pipe 实例的登录流程跑在 QQ 进程里面（桥接脚本，那里的 JS 事件循环是活的），
        它把结果写到 ``owndata/login-boot.json`` 和 ``owndata/qrcode.txt``。
        这两个文件是唯一的真实来源——Python 侧拿不到那个上下文。

        为什么不直接让 DLL 写 ``data/status.json``：那个文件由 Python 在启动时写，
        进程活着期间不该被两边同时改写；分开写再合并，职责清楚，也不会互相覆盖。
        """
        owndata = self.path / "owndata"

        boot_path = owndata / "login-boot.json"
        if boot_path.is_file():
            try:
                boot = json.loads(boot_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                boot = {}
            stage = str(boot.get("stage") or "")
            # DLL 写的 stage 名和 headless 的 phase 名不完全一致，映射一下，
            # 让账户表和二维码面板不需要为 pipe 分支。
            if stage == "waiting_qrcode":
                status["phase"] = "waiting_qrcode"
            elif stage == "scanned":
                status["phase"] = "scanned"
            elif stage == "qrcode_failed":
                status["phase"] = "qrcode_failed"
            elif stage == "login_failed":
                status["phase"] = "login_failed"
            elif stage == "login_succeed":
                status["phase"] = "initializing"
                status["uin"] = str(boot.get("uin") or status.get("uin") or "")
            elif stage == "session_ready":
                # Session accepted and initialised: actions and messages are possible from
                # here, so this is the pipe equivalent of "ready".
                #
                # Kept distinct from logged_in because they mean different things: the DLL
                # reports session_ready when init() completed, while logged_in comes from
                # onUserLoggedIn. A session can be ready before the account is fully online,
                # and showing "ready" too early would make the UI offer actions that fail.
                status["phase"] = "ready"
                status["uin"] = str(boot.get("uin") or status.get("uin") or "")
                status["session"] = str(boot.get("sessionId") or "")
                status["error"] = ""
            elif stage == "session_failed":
                # The session could not be created. Report it rather than sitting in
                # "initializing" forever, which is indistinguishable from a slow login.
                status["phase"] = "error"
                status["error"] = str(boot.get("sessionErr") or "session 初始化失败")
            elif stage == "logged_in":
                status["phase"] = "ready"
                status["uin"] = str(boot.get("uin") or status.get("uin") or "")
            if boot.get("err"):
                status["error"] = str(boot["err"])
            # 只要 DLL 报过状态，就刷新 updated_at，否则 to_dict() 会因为在 10 秒
            # 窗口外判定实例已死。
            if stage:
                status["updated_at"] = int(time.time())
                if not status.get("pid"):
                    status["pid"] = self.pid

        qr_path = owndata / "qrcode.txt"
        if qr_path.is_file():
            try:
                qr = qr_path.read_text(encoding="utf-8").strip()
            except OSError:
                qr = ""
            if qr:
                status["qr"] = qr

    def last_account(self) -> dict[str, str]:
        try:
            data = json.loads((self.path / "data" / "login.json").read_text(encoding="utf-8"))
            uin, uid = str(data.get("uin") or ""), str(data.get("uid") or "")
            if re.fullmatch(r"[1-9]\d{0,19}", uin) and re.fullmatch(r"u_[A-Za-z0-9_-]+", uid):
                return {"uin": uin, "uid": uid}
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        return {}

    def is_process_alive(self) -> bool:
        """Whether this session's QQ process still exists, independent of its status file.

        `to_dict` decides `alive` from a heartbeat: a status file refreshed within 10 seconds. That is
        right for detecting a wedged or half-dead session, but it also means a session the user killed
        keeps its last status on disk and the row lingers until something else notices.

        This answers a simpler question -- is the process still there -- which is what the UI needs to
        drop a row promptly. It checks the recorded pid first (cheap, exact), then anything listening on
        the session's port, because the pid in the config can be stale after a restart that failed
        halfway.

        A session that was never started has no pid and no listener, and returns False.

        The pid is read from `status.json`, not from the instance record: `instance.json` never stores one,
        so `self.pid` is 0 for every instance and using it would report every session dead.

        `_ALIVE_PROBE` exists so a test can state "this process is alive" without spawning QQ: the real
        answer depends on the machine, and a test asserting on it would pass or fail by environment.
        """
        status = self.status()
        probe = _ALIVE_PROBE or _pid_running
        pid = int(status.get("pid") or 0)
        if pid > 0 and probe(pid):
            return True
        port = int(self.port or 0)
        if port <= 0:
            return False
        # Anything listening on the session port counts. A port check rather than a process-name check
        # because the process is a real QQ.exe and its name says nothing about which session it is.
        return bool(pids_on_port(port))

    def to_dict(self) -> dict[str, Any]:
        status = self.status()
        phase = str(status.get("phase") or "stopped")
        alive = bool(
            phase != "stopped"
            and int(status.get("pid") or 0) != 0
            and status.get("updated_at")
            and time.time() - float(status["updated_at"]) < 10
        )
        # A fresh heartbeat is not proof of life: the status file is written once at launch, so for the
        # first ten seconds it looks current even if the process died immediately. Measured exactly that
        # -- QQ gone, `running=True` for the whole window -- which made the UI show a live session that
        # could not be called.
        #
        # For pipe sessions the process is the authority, so ask it. The heartbeat still decides for
        # headless instances, where it is the only signal available.
        if alive and self.kind == "pipe" and not self.is_process_alive():
            alive = False
        # 被踢是一种"已结束但原因重要"的状态：进程早就没了，所以不能只看
        # ``phase``——它会被 stop()/重启改写成 stopped。``previous_phase`` 是
        # 停止时特意留下的痕迹，两者任一为 kicked 都按被踢呈现。
        #
        # 另一个坑：被踢后 host 往往会退出，status.json 不再刷新，``alive``
        # 早已是 False。早先的实现因此把界面显示成"未登录"，用户完全看不到
        # 自己被踢了——而"为什么掉线"恰恰是最该显示的信息。
        kicked = phase == "kicked" or str(status.get("previous_phase") or "") == "kicked"
        return {
            "id": self.id,
            "name": self.name or self.id,
            "kind": self.kind,
            "path": str(self.path),
            "qq": self.qq or str(status.get("uin") or ""),
            "avatar": self.avatar,
            "port": self.port,
            "pid": int(status.get("pid") or self.pid or 0),
            "running": alive,
            "logged_in": alive and status.get("phase") == "ready",
            "phase": "kicked" if kicked else phase,
            "qr": str(status.get("qr") or ""),
            "error": str(status.get("error") or ""),
            "nickname": str(status.get("nickname") or ""),
            "last_qq": self.last_account().get("uin", ""),
            "can_quick_login": bool(self.last_account()),
            "auto_start": self.auto_start,
            "auto_relogin": self.auto_relogin,
            "auto_relogin_delay": self.auto_relogin_delay,
            "auto_relogin_delay_label": RELOGIN_DELAY_LABELS.get(
                self.auto_relogin_delay, f"{self.auto_relogin_delay} 秒"),
            "kicked": kicked,
            "kick_reason": str(status.get("error") or "") if kicked else "",
            "security_kicked": bool(status.get("security_kicked_type")),
            # 退避状态要可见：否则开了"自动重新登陆"却一直不动，用户会以为坏了。
            # 这些字段让界面能说明"还要等多久"或"已停止自动重试"。
            #
            # ``next_retry_in`` 一律转成有限的秒数（熔断时给 None），免得模板里
            # 出现 inf，既要防整除异常又要防显示成 "inf 分钟后"。
            "next_retry_in": _retry_remaining(self.id, self.auto_relogin_delay),
            "retry_exhausted": _retry_exhausted(self.id),
            "auto_start": self.auto_start,
            # 数据目录按 QQ 自己的方式来取，不猜、不造。
            #
            # pipe 实例用的是系统配置的那一个（reports/06：UserDataInfo.ini 的
            # UserDataSavePath），所以这里直接报它；headless 实例的数据在
            # data/qq 下，由 host.js 传给 session.init。
            "data_dir": self._reported_data_dir(),
        }

    def _reported_data_dir(self) -> str:
        """返回"这个实例的数据实际存放在哪"，取不到就返回空串。

        pipe 与 headless 的来源不同，所以分别处理，且都不编造：

        * pipe —— QQ 自己的配置决定数据根（公共文档下的 UserDataInfo.ini）。
          配置拿不到就返回空串，让界面显示"未知"，而不是显示一个我们猜的路径。
        * headless —— host.js 把 ``desktopPathConfig.account_path`` 指到
          ``data/qq``，这是它的事实来源。
        """
        if self.kind == "pipe":
            config_path = self.path / "config.json"
            try:
                config = json.loads(config_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                config = {}
            recorded = str(config.get("qqDataRoot") or "")
            if recorded:
                return recorded
            # 配置里没记（老实例）就现读一次，读不到返回空。
            root = qq_data_root()
            return str(root) if root is not None else ""
        return str(self.path / "data" / "qq")


class HeadlessManager:
    def __init__(self, root: Path | str = ROOT_NAME, runtime: Path | str | None = None) -> None:
        self.root = Path(root)
        #: QQ 客户端副本。放在**项目根**而不是实例目录里：所有实例共用一份，
        #: 而它能被 .gitignore 排除的同时保持路径稳定（不再随 headlessClient/
        #: 一起被当成运行期垃圾）。旧布局是 headlessClient/runtime，仍然兼容。
        self.runtime = Path(runtime) if runtime is not None else default_runtime_dir()
        # Job guards are NOT stored here.
        #
        # HeadlessManager is built per WebUI request, so anything held on it dies with the
        # request. A guard on this object would close its job when the request ended and the
        # kernel would terminate QQ on the spot. They live in job_guard's process-wide
        # registry instead, which is what makes instances follow QSICK rather than a manager.

    def stop_all(self) -> None:
        """停掉全部实例。退出时调用，不抛异常。

        ``stop`` 已经会用 ``taskkill /T /F`` 清理进程树；这里再兜一层与 Job Object 有关
        的状态：如果某个实例的 job 句柄还在，即便 taskkill 因权限或时序失败，关闭句柄
        也会让内核终止它。
        """
        for path in self._dirs():
            try:
                self.stop(path.name)
            except Exception as exc:  # noqa: BLE001 - 关机路径不能因为一个实例中断
                log.warn("停止实例失败", instance=path.name, error=str(exc))
        # Belt and braces: if any job is still holding a process (a stop raced with a
        # start), close it now. Closing is what triggers KILL_ON_JOB_CLOSE.
        from .job_guard import release_all

        release_all()

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def _migrate_pipe_dir_name(self) -> None:
        """把老的 ``C<n>`` 形式的 pipe 实例改成 ``Pipe``。

        为什么要迁移：pipe 的目录名原本跟着 headless 走 ``C0``/``C1``，改成固定
        ``Pipe`` 之后，老机器上的实例会变成"目录名是 C1、内容是 pipe"的状态。
        那不会立刻出错（``kind`` 从 instance.json 读，功能照常），但会让
        "pipe 只能有一个"的判断和界面上的一眼识别都变模糊。

        只在目标名不存在时移动，绝不覆盖已有目录——宁可留着旧的让人看见，
        也不能把两份数据合成一份。
        """
        if not self.root.is_dir():
            return
        target = self.root / PIPE_INSTANCE_ID
        if target.exists():
            return
        for path in self.root.iterdir():
            if not path.is_dir() or path == target:
                continue
            if not (path.name.startswith("C") and path.name[1:].isdigit()):
                continue
            meta_path = path / "instance.json"
            if not meta_path.is_file():
                continue
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if _normalize_kind(meta.get("kind")) != "pipe":
                continue
            try:
                shutil.move(str(path), str(target))
            except OSError as exc:
                log.warn("pipe 实例目录改名失败，保持原样",
                         path=str(path), error=str(exc))
                return
            # id 也要跟着改，否则 instance.json 里的 id 与目录名不一致。
            try:
                meta["id"] = PIPE_INSTANCE_ID
                (target / "instance.json").write_text(
                    json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            except OSError as exc:
                log.warn("pipe 实例 id 更新失败", error=str(exc))
            log.info("pipe 实例目录已改为固定名", old=path.name, new=PIPE_INSTANCE_ID)
            return

    def _dirs(self) -> list[Path]:
        """Directories under `headlessClient/` that are headless instances.

        A directory counts only when both hold:

          1. its name starts with `C` followed by digits, and
          2. it contains a `host.js`.

        Both conditions matter, and neither is sufficient. The name alone would accept a half-created
        directory, a failed template copy, or any unrelated folder dropped in the root -- shown as an
        instance that can never start, indistinguishable from a real one until it fails. And requiring
        the host is what makes the name meaningful: an instance without it has nothing to launch.

        `host.js` rather than `wrapper.node`: headless instances do not carry the QQ runtime themselves.
        They load the shared copy at the project root (`runtime/versions/<ver>/resources/app/wrapper.node`),
        and `create()` writes only `host.js` into the instance directory. Requiring `wrapper.node` here
        would match nothing -- measured exactly that, `instances()` came back empty for every real
        instance.

        `Pipe/` is not an instance and is not returned here. It holds the single client that sessions are
        launched from, and callers that need it use `pipe_instance_id()`.
        """
        if not self.root.is_dir():
            return []

        # Older instances named their pipe directory C<n> (it used to share headless's numbering).
        self._migrate_pipe_dir_name()

        found: list[Path] = []
        for item in self.root.iterdir():
            if not item.is_dir():
                continue
            name = item.name
            if not (name.startswith("C") and name[1:].isdigit()):
                continue
            if not (item / "host.js").is_file():
                continue
            found.append(item)

        def name_key(item: Path) -> int:
            try:
                return int(item.name[1:])
            except ValueError:
                return 0

        return sorted(found, key=name_key)

    def set_port(self, instance_id: str, port: int) -> dict[str, Any]:
        """Record a new DLL port for one session, without applying it to a running process.

        A running QQ already bound its port; rewriting the config cannot move it. So this only takes
        effect on the next start, and the returned summary says whether a restart is needed rather than
        letting the operator assume it took effect immediately.
        """
        instance = self._get(instance_id)
        if instance.kind != "pipe":
            raise ValueError("只有 pipe session 有独立端口")
        config_path = instance.path / "config.json"
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        previous = int(payload.get("port") or 0)
        payload["port"] = int(port or 0) or previous
        config_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

        # The instance.json copy is what the list renders, so keep the two in step.
        meta_path = instance.path / "instance.json"
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            meta = {}
        if isinstance(meta, dict):
            meta["port"] = int(payload["port"])
            meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

        summary = self._get(instance_id).to_dict()
        summary["port_changed"] = int(payload["port"]) != previous
        summary["needs_restart"] = bool(summary.get("running")) and summary["port_changed"]
        return summary

    def live_sessions(self) -> list[dict[str, Any]]:
        """Sessions that currently answer on their own port, newest first.

        This replaces an earlier `prune_dead_sessions`, which filtered `instances()` by `kind == "pipe"`.
        That list has excluded `Pipe` since the client stopped being an instance, so the function silently
        returned an empty list while looking like it did work.

        Liveness and identity both come from one call: `login.info` on the session's own port. A reply means
        the process is up (it is the DLL answering, not a file on disk), and the uin in that reply belongs to
        that process because the dispatcher's identity source is live.

        That replaces two weaker signals: a recorded pid (survives its process on disk) and a shared
        `login-boot.json` (cannot tell sessions apart -- every one reports the same account).

        "Which port" means `nowport` -- the one actually bound -- not `port`, which is what the operator
        asked for and may have been edited since.
        """
        sessions: list[dict[str, Any]] = []
        for item in read_session_dirs(self.root):
            probe = probe_session_login(int(item.get("nowport") or 0))
            if not probe.get("running"):
                continue
            entry = dict(item)
            entry["running"] = True
            entry["logged_in"] = bool(probe.get("logged_in"))
            entry["detail"] = str(probe.get("detail") or "")
            entry["account"] = ({"uin": probe["uin"], "nickname": probe["nickname"]}
                                if probe.get("logged_in") else {})
            sessions.append(entry)
        return sessions


    def pipe_instance_id(self) -> str:
        """The id of the single `Pipe/` client directory.

        One client, many sessions: sessions are launches of this, not copies of it. The id is fixed rather
        than discovered because there is only ever one.
        """
        return PIPE_INSTANCE_ID

    def start_session(self, port: int = 0, core_port: int = DEFAULT_WEBUI_PORT,
                      tmp_id: str = "") -> dict[str, Any]:
        """Launch one session from the shared client.

        A session is a repeated launch of `Pipe/QQ.exe`, not a new client copy. Each launch gets a DLL copy
        under `dll/<tmp_id>/` and its own port; nothing else is duplicated.

        `tmp_id` decides whether this is a new session or an existing one being started again:

          * empty -- a new session. A fresh 4-character id names a new directory, which is what "新增 session"
            means and why the list grows by one.
          * given -- relaunch that session. The launch reuses its directory, so the row keeps its identity
            and its port. Without this, pressing 启动 created a second row for a process that was already
            running, and the two rows were the same session under different ids.

        The port is passed straight to `_start_pipe` rather than written onto the shared instance config:
        that config is one file for all sessions, so a per-session port there would be overwritten by the
        next launch and two sessions would claim the same number.
        """
        chosen = int(port or 0)
        if chosen <= 0:
            chosen = allocate_pipe_port(self._reserved_ports())
        instance = self._get(PIPE_INSTANCE_ID)
        result = self._start_pipe(instance, core_port, port=chosen,
                                  tmp_id=str(tmp_id or "").strip())
        result["sessions"] = read_session_dirs(self.root)
        return result

    def session_dll_dir(self) -> Path:
        """This manager's per-launch DLL scratch directory."""
        return session_dll_dir(self.root)

    def _reserved_ports(self) -> set[int]:
        """Ports already claimed, so the next session does not take one.

        Two sources, both needed:

          * `Pipe/dll/<tmp_id>/qsick_pipe.json` -- every session that has been started, whether or not its
            process is currently alive. A session directory outlives its process, so this is what stops a
            new launch from being handed a port an older session still owns.

          * each headless instance's `config.json` -- the 9100+n range, so a pipe session can never land on
            a number a headless host is listening on.

        A port recorded but not currently bound still counts: between allocation and the DLL binding there
        is a window, and a probe would report the port free during it.
        """
        used: set[int] = set()

        for session in read_session_dirs(self.root):
            port = int(session.get("port") or 0)
            if port > 0:
                used.add(port)

        for path in self._dirs():
            try:
                payload = json.loads((path / "config.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            try:
                port = int(payload.get("port") or 0)
            except (TypeError, ValueError):
                continue
            if port > 0:
                used.add(port)
        return used

    def instances(self) -> list[dict[str, Any]]:
        values: list[dict[str, Any]] = []
        for path in self._dirs():
            meta: dict[str, Any] = {}
            meta_path = path / "instance.json"
            if meta_path.is_file():
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    meta = {}
            values.append(
                Instance(
                    id=path.name,
                    path=path,
                    qq=str(meta.get("qq") or ""),
                    avatar=str(meta.get("avatar") or ""),
                    port=int(meta.get("port") or 0),
                    auto_start=bool(meta.get("autoStart")),
                    auto_relogin=bool(meta.get("autoRelogin")),
                    auto_relogin_delay=_normalize_delay(meta.get("autoReloginDelay")),
                    name=str(meta.get("name") or ""),
                    kind=_normalize_kind(meta.get("kind")),
                ).to_dict()
            )
        return values

    def names(self) -> set[str]:
        """已占用的实例名（小写比较，避免"客服A"和"客户a"并存）。"""
        taken = set()
        for item in self.instances():
            for candidate in (item.get("name") or "", item.get("id") or ""):
                text = str(candidate).strip().lower()
                if text:
                    taken.add(text)
        return taken

    def create(self, name: str = "", kind: str = "headless",
               port_hint: int = 0) -> dict[str, Any]:
        """新建一个实例。

        ``kind`` 决定这个目录里放什么：

        * ``headless`` —— 只有几个 json 加 host.js。启动时由 Node 加载共享的
          ``runtime/wrapper.node``，不占额外空间。
        * ``pipe`` —— 铺一份完整 QQNT 客户端（模板用硬链接，磁盘上只多一份的
          分量），并生成 ``launch.bat``，双击即可启动这个实例的 QQ。

        ``name`` 是显示名，必须唯一。它和目录名分开：目录名保持 ``C0``/``C1``
        这样的稳定标识（配置、数据库路径都引用它），改名字不会让已有数据失联。
        """
        from .instance_template import TemplateError, build_template, prepare_instance

        self.ensure()
        kind = _normalize_kind(kind)

        # There is exactly one pipe client.
        #
        # Sessions are repeated launches of this client (`start_session`), not copies of it. Copying cost
        # ~1.3 GB per session and is not what "repeat-launch QQ.exe" means. The restriction is therefore
        # about the *client directory*, not about how many sessions may run.
        #
        # This is what makes the dll/ scheme coherent: one client means one `dll/` directory, so the set of
        # sessions is a single readable list rather than one list per copy.
        if kind == "pipe":
            # Checked on disk, not through `instances()`: that list is headless-only by design, so asking it
            # about the pipe client always reports "none present" and the singleton rule would never fire.
            existing_pipe = self.root / PIPE_INSTANCE_ID
            if existing_pipe.is_dir():
                name = ""
                try:
                    meta = json.loads((existing_pipe / "instance.json").read_text(encoding="utf-8"))
                    name = str(meta.get("name") or "")
                except (OSError, json.JSONDecodeError):
                    name = ""
                raise ValueError(
                    f"已经存在 pipe 客户端「{name or PIPE_INSTANCE_ID}」。"
                    "pipe 客户端只能有一份——多开请用「新增 session」，那是把同一个客户端"
                    "再启动一次，不会复制文件。")

        label = str(name or "").strip()
        if label:
            if len(label) > 32:
                raise ValueError("实例名最多 32 个字符")
            if not re.fullmatch(r"[\w\u4e00-\u9fff][\w\u4e00-\u9fff \-.]*", label):
                raise ValueError("实例名只能包含字母、数字、中文、空格、- _ .")
            if label.lower() in self.names():
                raise ValueError(f"实例名「{label}」已被占用")

        # The pipe client's directory is always `Pipe`.
        #
        # Numbered variants (Pipe2, Pipe3 …) came from an earlier design where each session was its own
        # copy of the client. That is gone: sessions are launches of this one directory, so a suffix has
        # nothing to distinguish and would only suggest there can be more than one client.
        #
        # `C<n>` stays reserved for headless instances. Mixing pipe into that sequence would imply the
        # next headless instance fills the gap left by a deleted pipe directory, which it does not.
        if kind == "pipe":
            instance_id = PIPE_INSTANCE_ID
            path = self.root / instance_id
            index = 0
        else:
            used = {path.name for path in self._dirs()}
            index = 0
            while f"C{index}" in used:
                index += 1
            instance_id = f"C{index}"
            path = self.root / instance_id

        # pipe 实例要先确认模板可用，再动磁盘：模板生成失败时不要在
        # headlessClient 下留一个半成品目录，否则用户看到的是一个永远起不来的
        # 实例，还得手工删。
        template = None
        if kind == "pipe":
            template = build_template()

        (path / "data").mkdir(parents=True)
        # A pipe session exposes its injected DLL's control API on its own port, so each one gets a
        # distinct number from the 7540 range. Two sessions on the same port would mean the second
        # DLL cannot bind, and that session would look started while being unreachable.
        #
        # Headless instances keep the 9100+n scheme: they run host.js and the port has to stay stable
        # across restarts, which that range already gives them.
        #
        # Ports promised to other sessions in this same pass are excluded, because a created-but-not-yet
        # -started session is not listening and its port still looks free.
        # An explicit request wins, then the hot-reloaded override map, then allocation. The map is
        # checked because the operator wants to pin a port without going through the UI; the config
        # written below remains the record of what was actually used.
        if kind == "pipe":
            requested = int(port_hint or 0)
            override = read_port_override(self.root).get(instance_id, 0)
            port = requested or override or allocate_pipe_port(self._reserved_ports())
        else:
            port = 9100 + index

        summary: dict[str, object] = {}
        if kind == "pipe" and template is not None:
            try:
                summary = prepare_instance(template, path, label or instance_id)
            except (OSError, TemplateError) as exc:
                shutil.rmtree(path, ignore_errors=True)
                raise ValueError(f"pipe 实例创建失败：{exc}") from exc

        (path / "instance.json").write_text(
            json.dumps({
                "id": instance_id,
                "name": label or instance_id,
                "kind": kind,
                "qq": "",
                "avatar": "",
                "port": port,
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        config_payload: dict[str, object] = {
            "id": instance_id,
            "kind": kind,
            "port": port,
            "dataDir": str(path / "data"),
        }
        if kind == "pipe":
            # pipe 实例不跑 host.js，宿主就是它自己那份 QQ.exe。
            config_payload["qqExe"] = str(path / "QQ.exe")
            config_payload["version"] = summary.get("version", "")
            # 记录 QQ 自己配置的数据根，供 WebUI 展示与授权核对。
            #
            # 不用「猜一个实例内的目录」：这个值的权威来源是 QQ 的
            # UserDataInfo.ini（reports/06），取不到就留空，让上层走 QQ 的兜底逻辑。
            data_root = qq_data_root()
            if data_root is not None:
                config_payload["qqDataRoot"] = str(data_root)
                # 创建时授权一次：QQ 写入前会检查目录是否授权，缺权限时弹的正是
                # "您未授权 QQ 在此位置写入数据"。
                granted = _grant_instance_data_access(data_root)
                config_payload["dataRootGranted"] = granted
                if not granted:
                    log.warn("数据目录授权失败，QQ 可能拒绝在该位置写入",
                             path=str(data_root))
        else:
            shutil.copy2(HOST_JS, path / "host.js")
        (path / "config.json").write_text(
            json.dumps(config_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        # Read back the instance that was just written, by id.
        #
        # Not via `instances()`: that list is headless-only, so a freshly created pipe client is absent from
        # it and `next(...)` raised StopIteration -- which surfaced as a 500 on the create endpoint. Asking
        # for the id directly works for both kinds.
        return self._get(instance_id).to_dict()

    def prepare_runtime(self) -> tuple[Path, str, str, str]:
        """Return ``(runtime dir, version, appid, qua)`` for the headless host.

        实例目录里的 ``runtime`` 一旦存在，它就是**事实来源**：版本号、appid 都从
        它自己的 ``package.json`` / ``major.node`` 读，而不是从本机安装的 QQ 读。
        否则用一份手工放进去的固定版本（例如 9.9.33-52230）替换了 runtime，
        实例配置却仍标成机器上 QQ 自动升级后的版本，登录时 clientVer/qua 与真正的
        二进制对不上。

        获取顺序：
          1. ``runtime/`` 已存在（项目根）——直接用；
          2. 从项目自己的发布地址下载锁定版本（``runtime_fetch.ensure_runtime``）。

        **没有第三步。** 早先下载失败时会退到"发现本机安装的 QQ 并复制一份"，
        这条已经去掉，也不再读取本机安装的位置：

        本机 QQ 会自己升级。用它复制出来的 runtime 版本号随机器而变，而我们注入的
        垫片和 DLL 是按锁定版本（9.9.33-52230）做的——实测本机升到 9.9.37-54395 后
        QQ.exe 在 ntdll 里以 0xc0000374（堆损坏）稳定崩溃。一个"取决于这台机器装了什么"
        的运行时，项目其余部分无从保证正确。

        所以拿不到 runtime 就明确失败，让用户知道该联网或手工放入，而不是悄悄用一份
        版本不匹配的副本把问题推迟到崩溃现场。
        """
        runtime = self.runtime
        runtime.mkdir(parents=True, exist_ok=True)
        shim = host_shim()
        from . import runtime_fetch
        if not runtime_fetch.runtime_ready(runtime):
            # 下载锁定版本：它是唯一能保证版本一致（clientVer/qua 与二进制对得上）的来源。
            try:
                runtime_fetch.ensure_runtime(runtime)
            except runtime_fetch.RuntimeDownloadError as exc:
                raise RuntimeError(
                    f"runtime 不可用，且下载失败：{exc}\n"
                    f"  期望目录：{runtime}\n"
                    f"  联网后重试，或手工把锁定的客户端放进这个目录。") from exc
            except Exception as exc:  # noqa: BLE001 - 网络层异常类型很杂
                raise RuntimeError(
                    f"runtime 不可用，且下载异常：{type(exc).__name__}: {exc}\n"
                    f"  期望目录：{runtime}\n"
                    f"  联网后重试，或手工把锁定的客户端放进这个目录。") from exc
            if not runtime_fetch.runtime_ready(runtime):
                raise RuntimeError(
                    f"runtime 下载后仍不可用。\n"
                    f"  期望目录：{runtime}\n"
                    f"  该目录下应当有 versions/<版本>/resources/app/wrapper.node。")
        # runtime/ 是客户端的**纯副本**，不往里放任何东西——它可能被当作模板
        # 硬链接给 pipe 实例，混进垫片会污染那些实例。
        #
        # 垫片改由每个实例自己持有：启动时复制进实例目录，并把实例目录加到
        # PATH 上。Windows 解析 wrapper.node 的导入时会搜当前目录与 PATH，所以
        # 实例目录里的 15 KB 垫片会盖过 versions/<ver>/ 那个 204 MB 原库
        # （实测：PATH 含实例目录时加载的是垫片，见 tests/test_headless.py）。
        app = runtime_fetch.app_dir(runtime)
        version = _runtime_version(app)
        if not version:
            # No local-install fallback: a version read from the machine would describe a binary that is
            # not the one being launched, and clientVer/qua would then disagree with it at login.
            raise RuntimeError(
                f"无法从 runtime 判断版本号（找不到 package.json / versions 目录）。\n"
                f"  目录：{app}")
        appid = read_appid(app / "major.node")
        qua = f"V1_WIN_NQ_{version.replace('-', '_')}_GW_B" if version else ""
        # 返回 app 层而不是 runtime 根：调用方要的是能直接 require(wrapper.node)
        # 的目录。完整客户端布局下两者不同（差 versions/<ver>/resources/app）。
        return app, version, appid, qua

    def _get(self, instance_id: str) -> Instance:
        # Accept both naming schemes: C<number> for headless instances, and Pipe / Pipe2 / Pipe3 … for
        # pipe sessions. The strict pattern exists to reject path traversal (`../`, absolute paths), so
        # it stays strict -- it just has to admit the whole pipe family, not only the first one.
        is_headless = bool(re.fullmatch(r"C(?:0|[1-9]\d*)", instance_id))
        is_pipe = instance_id == PIPE_INSTANCE_ID or bool(
            re.fullmatch(rf"{PIPE_INSTANCE_ID}[1-9]\d*", instance_id))
        if not (is_headless or is_pipe):
            raise KeyError(instance_id)
        path = self.root / instance_id
        if not path.is_dir():
            raise KeyError(instance_id)
        meta: dict[str, Any] = {}
        meta_path = path / "instance.json"
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                meta = {}
        return Instance(
            id=instance_id,
            path=path,
            qq=str(meta.get("qq") or ""),
            avatar=str(meta.get("avatar") or ""),
            port=int(meta.get("port") or 0),
            auto_start=bool(meta.get("autoStart")),
            auto_relogin=bool(meta.get("autoRelogin")),
            auto_relogin_delay=_normalize_delay(meta.get("autoReloginDelay")),
            name=str(meta.get("name") or ""),
            kind=_normalize_kind(meta.get("kind")),
        )

    def set_auto_start(self, instance_id: str, enabled: bool) -> dict[str, Any]:
        """开关单个实例的"随 QSICK 启动"。

        默认关闭。启动就拉起全部实例会让每个账号立刻占内存和网络，而用户通常只用
        其中一个；要不要自动起来应当由人决定，而不是由程序猜。
        """
        instance = self._get(instance_id)
        meta_path = instance.path / "instance.json"
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else {}
        except (OSError, json.JSONDecodeError):
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        meta["autoStart"] = bool(enabled)
        meta.setdefault("id", instance.id)
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        return self._get(instance_id).to_dict()

    def set_auto_relogin(self, instance_id: str, enabled: bool,
                         delay_seconds: int | None = None) -> dict[str, Any]:
        """Persist the 自动重新登陆 switch (and its delay) for one instance."""
        instance = self._get(instance_id)
        meta_path = instance.path / "instance.json"
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else {}
        except (OSError, json.JSONDecodeError):
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        meta["autoRelogin"] = bool(enabled)
        if delay_seconds is not None:
            # 只接受预设档位，别让调用方塞任意值——这个数字直接决定重登节拍，
            # 大到离谱等于永不重登，小到离谱就是风控加速器。
            if int(delay_seconds) not in RELOGIN_DELAY_CHOICES:
                raise ValueError(f"不支持的自动重登时延：{delay_seconds}")
            meta["autoReloginDelay"] = int(delay_seconds)
        meta.setdefault("id", instance.id)
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        return self._get(instance_id).to_dict()

    def recover_kicked(self, instance_id: str, core_port: int = DEFAULT_WEBUI_PORT,
                       *, respect_cooldown: bool = False) -> dict[str, Any]:
        """Restart an instance QQ kicked, then try the cached login.

        Only allowed when the user enabled 自动重新登陆 for it; otherwise the user
        decides what happens.

        重试必须受 :mod:`qsick.backoff` 约束：被踢之后秒级反复重登，在腾讯侧看
        就是"脚本在疯狂重连"，只会让风控咬得更紧。这里会在冷却未到时直接拒绝，
        并把剩余时间告诉调用方。
        """
        from .backoff import BOOK, describe_remaining

        instance = self._get(instance_id)
        if not instance.auto_relogin:
            raise ValueError("该实例未开启自动重新登陆")
        # ``respect_cooldown`` 只有自动路径会传 True：时间控制由 watch_kicked 的
        # 被踢计时器负责，这里的退避账本是第二道保险（挡"重登成功但立刻又被踢"
        # 的连续失败）。手动点重登时不该被它拦住——用户已经明确要求再试一次。
        if respect_cooldown and not BOOK.ready(instance_id):
            wait = BOOK.remaining(instance_id)
            raise ValueError(
                f"重登冷却中，{describe_remaining(wait)}再试"
                + ("（连续失败过多，已停止自动重试，请手动处理）"
                   if wait == float("inf") else "")
            )
        self.stop(instance_id)
        started = self.start(instance_id, core_port, auto_login=True)
        result: dict[str, Any] = {"account": started, "relogin": False, "error": ""}
        if instance.last_account():
            try:
                # 等一会儿再登录：宿主刚起来就要 session，失败率反而更高，
                # 而失败就意味着又一轮重试。这个等待是"让上一步真的完成"，
                # 不是"惩罚"，所以比退避本身短。
                time.sleep(2.5)
                login = self.quick_login(instance_id, core_port)
                result["account"] = login.get("account", started)
                result["relogin"] = bool(login.get("accepted"))
                if result["relogin"]:
                    BOOK.succeed(instance_id)
                else:
                    why = str(login.get("message") or "宿主没有接受快捷登录")
                    delay = BOOK.fail(instance_id, why)
                    result["error"] = why
                    result["next_retry_in"] = delay
            except Exception as exc:  # noqa: BLE001 - report the reason to the UI
                why = str(exc)
                delay = BOOK.fail(instance_id, why)
                result["error"] = why
                result["next_retry_in"] = delay
        else:
            # 没有可复用的登录态：重试也不会变好，别把它算进退避账本，
            # 但要说明为什么不继续。
            result["error"] = "没有可复用的登录记录，需要手动登录"
        return result

    def watch_kicked(self, core_port: int = DEFAULT_WEBUI_PORT) -> list[dict[str, Any]]:
        """Recover kicked instances that opted into 自动重新登陆.

        判断依据是**客户端状态**（``phase == "kicked"``，含被 stop() 保留到
        ``previous_phase`` 的痕迹），而不是"上次重登失败过"。识别到被踢的那一刻
        起，按用户选定的档位开始计时；计时未到就什么都不做。

        早先的实现每 5 秒就会重登一次，而被风控的账号几乎立刻又被踢，于是形成
        持续的高频重登——那正是风控最敏感的特征。
        """
        from .backoff import KICK_CLOCK

        results: list[dict[str, Any]] = []
        for path in self._dirs():
            instance = self._get(path.name)
            state = instance.status()
            # 被踢后 host 常会退出，随后 stop() 会把 phase 改写成 stopped 并把
            # 原因记进 previous_phase。两种拼写都要认，否则开了自动重登也永远
            # 不会触发。
            kicked = (str(state.get("phase") or "") == "kicked"
                      or str(state.get("previous_phase") or "") == "kicked")
            # 没被踢就把计时清掉：下次被踢要重新从零开始等满整个档位，
            # 而不是沿用上一次的旧时钟。
            if not kicked:
                KICK_CLOCK.clear(path.name)
                continue
            if not instance.auto_relogin:
                KICK_CLOCK.clear(path.name)
                continue
            # 第一次看到这个实例被踢 → 开始计时。此后每 5 秒来问一次都只是
            # 查表，不会重置时钟。
            KICK_CLOCK.start(path.name)
            if not KICK_CLOCK.elapsed_enough(path.name, instance.auto_relogin_delay):
                continue
            try:
                outcome = self.recover_kicked(path.name, core_port, respect_cooldown=True)
            except Exception as exc:  # noqa: BLE001 - keep watching the others
                outcome = {"account": instance.to_dict(), "relogin": False, "error": str(exc)}
            results.append({"id": path.name, **outcome})
        return results

    def start(self, instance_id: str, core_port: int = DEFAULT_WEBUI_PORT, auto_login: bool = True) -> dict[str, Any]:
        instance = self._get(instance_id)
        current = instance.to_dict()
        old_config: dict[str, Any] = {}
        config_path = instance.path / "config.json"
        if config_path.is_file():
            try:
                old_config = json.loads(config_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                old_config = {}
        same_parent = int(old_config.get("parentPid") or 0) == os.getpid()
        if current.get("running") and same_parent and self._pid_alive(int(current.get("pid") or 0)):
            return current
        if current.get("running") or old_config.get("parentPid"):
            # A stale host from a previous QSICK process, or a heartbeat that is
            # still fresh after the process died. Always replace it so the new
            # parent watchdog is installed.
            self.stop(instance_id)
        # pipe 实例启动的是真的 QQ.exe，不是 node 宿主。两条路的分岔点就在这里：
        # 再往下会写一份 headless 的 config.json 并拉起 host.js，那对 pipe 实例
        # 毫无意义（它的宿主是自己那份 QQ.exe，也不需要共享 runtime/）。
        if _normalize_kind(instance.kind) == "pipe":
            return self._start_pipe(instance, core_port)

        shutil.copy2(HOST_JS, instance.path / "host.js")
        for name in ("headless_codec.cjs", "headless_actions.cjs", "headless_sender.cjs", "headless_login.cjs", "headless_media.cjs", "headless_oidb.cjs"):
            shutil.copy2(HOST_JS.with_name(name), instance.path / name)
        runtime, version, appid, qua = self.prepare_runtime()
        node = os.environ.get("QSICK_NODE") or shutil.which("node")
        if not node:
            raise RuntimeError("node executable not found")
        config = {
            "id": instance.id,
            "port": instance.port,
            "dataDir": str((instance.path / "data").resolve()),
            # appDir 是真正加载 wrapper.node 的那一层；runtime 指向完整客户端根，
            # 供宿主解析资源、Electron 组件等相对路径时参照。
            "appDir": str(runtime.resolve()),
            "runtime": str(self.runtime.resolve()),
            "corePort": int(core_port),
            "autoLogin": bool(auto_login),
            "parentPid": os.getpid(),
            "bridgeToken": secrets.token_urlsafe(32),
            "version": version,
            "appid": appid,
            "qua": qua,
        }
        (instance.path / "config.json").write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        creation = 0
        if os.name == "nt":
            # CREATE_NO_WINDOW keeps the headless host invisible without detaching it
            # from the console in a way that can disturb the parent process group.
            creation = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
        instance_dir = instance.path.resolve()
        # 垫片随实例走：runtime/ 保持纯客户端副本，这个实例目录里放一份 15 KB
        # 的 QQNT.dll，再把它加进 PATH，wrapper.node 的导入就会命中它，而不是
        # 客户端自带那个 204 MB 原库。
        shim = host_shim()
        if shim is not None:
            shutil.copy2(shim, instance_dir / "QQNT.dll")
        elif not (instance_dir / "QQNT.dll").is_file():
            raise RuntimeError("QQNT.dll shim missing (build/native host not built)")
        # Same profile isolation as pipe instances. This was missing: only PATH was
        # being set, so a headless instance resolved its data root from the real user
        # profile -- which is what the "personal folder location invalid" dialog was
        # reporting. Both kinds now share one helper so they cannot drift apart again.
        env = _isolated_env(instance_dir)
        env["PATH"] = str(instance_dir) + os.pathsep + env.get("PATH", "")
        log_handle = (instance_dir / "host.log").open("w", encoding="utf-8")
        try:
            process = subprocess.Popen(
                [node, str(instance_dir / "host.js"), "--config", str(instance_dir / "config.json")],
                cwd=str(instance_dir),
                env=env,
                creationflags=creation,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
            )
        finally:
            log_handle.close()
        for _ in range(50):
            time.sleep(0.2)
            status = instance.status()
            if int(status.get("pid") or 0) == process.pid:
                break
        return instance.to_dict()

    def _pipe_app_dir(self, instance: Instance) -> Path | None:
        """返回 pipe 实例的客户端 ``resources/app`` 目录，找不到就返回 None。

        判据是 ``wrapper.node`` 在那儿：注入脚本按同样规则定位模块，两边必须指向同一处，
        否则 Python 把文件拷到 A、脚本去 B 找，症状还是 "dispatcher unavailable"。

        配置里的 ``runtime`` 直接就是 app 目录；也接受它指向上一级（resources），
        因为不同版本的写法不完全一致。
        """
        try:
            config = json.loads((instance.path / "config.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        runtime = str(config.get("runtime") or "").strip()
        if not runtime:
            return None
        for candidate in (Path(runtime), Path(runtime) / "app"):
            try:
                if (candidate / "wrapper.node").is_file():
                    return candidate
            except OSError:
                continue
        return None

    def _start_pipe(self, instance: Instance, core_port: int, port: int = 0,
                    tmp_id: str = "") -> dict[str, Any]:
        """启动 pipe 实例：跑**未经修改**的原版 QQ.exe，再由传输层注入 payload。

        ## 为什么不改 ``main``（实测结论）

        曾经按 NapCat 的思路把 ``resources/app/package.json`` 的 ``main`` 换成自己的
        loader。实测在这个版本上必然失败，且失败得很彻底：

        ===================  ===============  =============
        对 package.json 的动作  ``[preload]`` 成功次数  结果
        ===================  ===============  =============
        不动                  2               正常启动
        加一个无害键           0               弹「退出」框
        **只改缩进**           0               弹「退出」框
        ===================  ===============  =============

        「只改缩进也失败」这一条是关键：QQ 对 ``package.json`` 做的是**内容哈希**
        校验（``application.json`` 里存着四个文件的 RSA 签名，校验代码在
        ``major.node``），不是解析后比对字段。改一个字节就等于改文件，没有"保留
        原意"的余地。所以这条路上不存在"小心地改"的写法。

        ## 那 NapCat 靠什么

        它**不动磁盘上的文件**：改过的 ``package.json`` 放在 NapCat 自己的目录
        （``qqnt.json``），再由 ``NapCatWinBootHook.dll`` 在进程内拦截文件读取，
        让 QQ 读到那份副本。磁盘上那份签名依旧有效，校验自然通过。

        这个做法需要我们自己写一个 hook ``CreateFileW`` / ``GetFileInformationByHandleEx``
        的 DLL。我拿 NapCat 的原版二进制在本客户端上试过（``NapCatWinBootMain.exe``
        + 它的 hook DLL + 我们的 loader），进程起来后立刻退出、loader 没被执行——
        说明那套 hook 与本版本客户端不匹配，直接照搬并不成立。

        ## 所以这样启动

        原版客户端照常启动（所有受校验文件一字节不动，校验必定通过），由
        ``transport/pipe.py`` 用 ``CreateRemoteThread(LoadLibraryW)`` 把
        ``qsick_payload.dll`` 注入进去接管命名管道。

        代价：**窗口会出现**。这是不改受校验文件的必然结果——原版入口就是建窗口的
        那个。至少它一定能启动，而不是卡在「文件已损坏」上。

        状态写进 ``data/status.json``，与 headless 共用同一套字段，这样账户表、
        自动重登、聊天页都不需要为 pipe 单独分支。
        """
        from . import instance_template

        exe = instance.path / "QQ.exe"
        if not exe.is_file():
            raise RuntimeError(
                f"pipe 实例缺少 QQ.exe：{exe}\n"
                "  这个实例可能是用早期版本创建的，删掉重建即可（会重新铺模板）。")

        version = instance_template.detect_version(instance.path)
        app = instance.path / "versions" / version / "resources" / "app"
        if not (app / "package.json").is_file():
            raise RuntimeError(
                f"pipe 实例缺少客户端应用目录：{app}\n"
                "  删掉重建即可（会重新铺模板）。")

        # pipe 实例有两种历史形态：
        #   * 早期改 package.json 的 main（NapCat 式），已废弃——它靠改客户端文件
        #     实现，而要求明确禁止改动客户端任何文件；
        #   * 现在：启动原版 QQ.exe，在它跑起来之前注入 build/qsick_pipe.dll。
        # 老实例可能还带着 loader，先撤掉（幂等，且不碰没改过的实例）。
        instance_template.remove_loader(instance.path, app)

        data_dir = instance.path / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        # 原生层（Documents\Tencent Files）指向实例自己的目录：不碰用户真实 QQ 的
        # 配置，也就不会撞上它的单实例锁。注入的 DLL 靠 hook 文件夹 API 实现。
        own_data = instance.path / "owndata"
        own_data.mkdir(parents=True, exist_ok=True)
        config_path = instance.path / "config.json"

        # 先写 config：传输层与（可能存在的）loader 都按它的路径定位。
        config: dict[str, Any] = {}
        if config_path.is_file():
            try:
                config = json.loads(config_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                config = {}
        config.update({
            "id": instance.id,
            "kind": "pipe",
            # The launching session's port when one was supplied, otherwise whatever the instance
            # records. Per-session ports cannot live in this shared file -- it is one config for all
            # sessions -- so the caller's value takes precedence and is used for the DLL config below.
            "port": int(port or instance.port),
            "dataDir": str(data_dir.resolve()),
            "runtime": str(app.resolve()),
            "qqExe": str(exe.resolve()),
            "owndata": str(own_data.resolve()),
            "corePort": int(core_port),
            "parentPid": os.getpid(),
            "bridgeToken": secrets.token_urlsafe(32),
            "version": version,
        })
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")

        payload = pipe_dll()
        if payload is None:
            raise RuntimeError(
                "qsick_pipe.dll 不存在：先运行 native/qsick-pipe/build/build_pipe_dll.bat。\n"
                f"  期望位置：{PIPE_DLL_NAME}")

        # Each session gets its own DLL copy beside its own config, because the DLL reads
        # `qsick_pipe.json` from its own directory -- a shared copy would let concurrent launches overwrite
        # each other's port.
        #
        # `tmp_id` names that directory and is the session's identity:
        #
        #   new session   a fresh 16-bit id, so the list grows by one row
        #   relaunch      the id passed in, so the row keeps its identity and its port
        #
        # Generating a new id unconditionally is what made 启动 produce a second row for a session that was
        # already there: same process, two ids, two rows.
        tmp_id = str(tmp_id or "").strip() or new_session_tmp_id()
        session_dir = self.session_dll_dir() / tmp_id
        session_dir.mkdir(parents=True, exist_ok=True)
        instance_payload = session_dir / PIPE_DLL_NAME
        shutil.copy2(payload, instance_payload)
        # A fresh token per launch: the previous process may still be dying while this one starts, and a
        # reused token would let the old connection's state be attributed to the new process.
        token = secrets.token_hex(16)

        # 配置写入同一个目录，紧挨着 DLL——DLL 只从自己所在目录找这个文件。
        #
        # owndata 仍写进去：即便不做数据目录隔离，DLL 的这个字段也决定 QQ 把
        # nt_data / nt_db 放哪；指向系统配置的目录即可，见下面 own_data 的来源。
        #
        # 结构按新的格式带一层 session：
        #     { "session": { "tmp_id": "...", "port": 7540 } }
        # 保留旧的顶层 port，是因为已在运行的旧 DLL 仍读顶层字段；两个都写，谁读谁取。
        (session_dir / PIPE_DLL_CONFIG).write_text(
            json.dumps({
                # `port` is what this launch binds. `nowport` mirrors it for a freshly launched session --
                # the process has not had time to diverge from the request. It is cleared again on close,
                # so a stopped session reports "nothing bound" rather than its last number.
                "session": {"tmp_id": tmp_id, "port": int(port or instance.port),
                            "nowport": int(port or instance.port)},
                # ABSOLUTE, and it has to be.
                #
                # The DLL resolves this path against the QQ process's working directory, which is the
                # client root (`Pipe/`). A relative value here produced
                # `Pipe/headlessClient/Pipe/owndata` -- a second, nested copy of the state tree: the client
                # data (`qq/`), the injection artefacts, the probe files, everything. The expected
                # `Pipe/owndata` stayed empty, so `login-boot.json` never appeared where it was read from,
                # and each launch started against a different data root than the previous one.
                "owndata": str(own_data.resolve()),
                # Where this session alone puts its action queue and event stream.
                #
                # owndata above is shared by every session, and the queue used to live there: a task posted
                # to one session's port was claimed by whichever session drained the queue first and ran with
                # that session's account. QQ's own data stays on owndata; only the queue moves.
                "taskDir": str(session_dir.resolve()),
                "port": int(port or instance.port),
                "nowport": int(port or instance.port),
                # Token the WebSocket state channel requires.
                #
                # The port is on loopback, which every process on the machine can reach, and a state
                # subscription names the account -- so the upgrade is authenticated. One token per launch,
                # written here and read by the DLL from this same file: neither side has to be told separately.
                "ws_token": token,
            }, ensure_ascii=False, indent=2),
            encoding="utf-8")

        # 实例内的 JS 模块。
        #
        # pipe 实例跑的是真 QQ.exe，它不会像 headless 实例那样由 host.js 去 require 这些
        # 模块——注入脚本自己在运行时按 wrapper.node 的位置去找它们。所以这些文件必须
        # 落在客户端 resources/app 下，而不是只待在源码树里。
        #
        # 之前是手工拷的，结果是每次重启实例都丢掉，表现为 "dispatcher unavailable"：
        # 代码没错，文件不在。这里让它跟着实例走，重启就自动恢复。
        #
        # 目标目录从 runtime 推出（runtime 指向 resources/app），找不到就跳过——某些
        # 版本布局不同，静默跳过比写错位置更好，注入脚本会自己再找一遍并报告。
        app_dir = self._pipe_app_dir(instance)
        if app_dir is not None:
            for module in PIPE_JS_MODULES:
                source = HOST_JS.with_name(module)
                if not source.is_file():
                    continue
                try:
                    shutil.copy2(source, app_dir / module)
                except OSError:
                    # 实例正在跑时文件可能被占用；下一次启动会再试，不值得因此让实例起不来。
                    pass

            # 原生插件跟着一起放，理由同上：cjs 用 __dirname 找它，而实例目录和仓库目录不同。
            hook = inithook_module()
            if hook is not None:
                try:
                    shutil.copy2(hook, app_dir / INITHOOK_MODULE)
                except OSError:
                    pass

        log_path = instance.path / "host.log"
        # --user-data-dir 把 Electron 的 userData（缓存、Cookies、Local Storage、Crashpad）指到
        # 该账号自己的目录，而不是共享 %APPDATA%\QQ。
        #
        # 这里按账号分而不是按 session 分：多个 session 是同一份客户端被重复启动，它们共用
        # 一份 QQ 数据（UserDataInfo.ini 指定），但 Electron 的 userData 是进程级缓存，两个
        # 进程同时写同一份会互相破坏 Cookies 和 Local Storage。
        #
        # 目录名取 QQ 号；还没有号时退回 session 的临时 id——第一次启动必然还没登录。
        account_key = str(getattr(instance, "qq", "") or "").strip()
        if not (account_key.isdigit() and len(account_key) <= 20):
            account_key = tmp_id
        user_data = own_data / "userdata" / account_key
        user_data.mkdir(parents=True, exist_ok=True)
        # 列表形式：路径原样交给 Popen，不需要任何人再加引号。
        #
        # **默认不加 --no-sandbox。**
        #
        # 这里原先硬编码它，理由是"不加 QQ 会以 0x80000003 退出"。那个观察是对的，
        # 但因果搞反了：真正的原因是实例目录带着特殊 ACL（DSH 沙箱环境留下的），
        # QQ 在那个路径下无法正常启动；--no-sandbox 只是绕过了它。而绕过的代价是
        # 关掉 Chromium 沙箱，QQ 在关掉沙箱的分支里会判定"个人文件夹未授权"，
        # 于是用弹窗顶替了崩溃——问题换了个样子出现，更难查。
        #
        # 目录 ACL 正常之后 QQ 自己就能起来，这个参数从"必需"变成了"新的故障源"。
        # 需要时用 QSICK_PIPE_NO_SANDBOX=1 临时打开。
        arguments = [f"--user-data-dir={user_data}"]
        if os.environ.get("QSICK_PIPE_NO_SANDBOX") == "1":
            arguments.insert(0, "--no-sandbox")
            log.warn("已启用 --no-sandbox（QSICK_PIPE_NO_SANDBOX=1）："
                     "它会关闭 Chromium 沙箱，可能触发"
                     "“个人文件夹保存位置无效”的判定", instance=instance.id)

        # 注入流程：先普通启动（挂起启动会让 QQ 直接崩在同一个校验上），再立刻
        # 注入并把实例目录加进 PATH。inject 后的进程就是本实例的 QQ.exe，
        # 由它自己跑 Electron 主进程 + 原生后端，只是窗口会被隐藏。
        injection = None
        process = None
        try:
            # 绝对路径：QQ 会拿自身路径去推导安装根、versions 目录和数据目录，
            # 传相对路径会让它算出一套错的位置（实测命令行里出现过
            # "headlessClient\C1\QQ.exe" 这种写法）。
            injection = _spawn_and_inject(exe.resolve(), instance_payload.resolve(),
                                          arguments, instance.path.resolve(),
                                          instance.id)
            process = injection.process
        except Exception as exc:  # noqa: BLE001 - 失败要带出可读原因
            raise RuntimeError(f"启动并注入 QQ.exe 失败：{exc}") from exc

        # 与 headless 一致的状态文件，让上层不需要为 pipe 分支。
        status = {
            "id": instance.id,
            "phase": "starting",
            "pid": process.pid,
            "qr": "",
            "uin": "",
            "uid": "",
            "nickname": "",
            "error": "",
            "transport": "pipe",
            "updated_at": int(time.time()),
        }
        (data_dir / "status.json").write_text(
            json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")

        # The guard lives in the process-wide registry (see job_guard.acquire). It must not
        # be a local -- that would be collected on return and QQ would die with it -- and it
        # must not live on the manager either, since the WebUI builds a manager per request.

        log.info("pipe 实例已启动", instance=instance.id, pid=process.pid)
        return instance.to_dict()

    def quick_login(self, instance_id: str, core_port: int = DEFAULT_WEBUI_PORT) -> dict[str, Any]:
        instance = self._get(instance_id)
        key = str(instance.path.resolve())
        with _LOGIN_LOCKS_GUARD:
            lock = _LOGIN_LOCKS.setdefault(key, threading.Lock())
        if not lock.acquire(blocking=False):
            raise RuntimeError("该实例正在尝试登录，请勿重复点击")
        try:
            return self._quick_login(instance_id, core_port)
        finally:
            lock.release()

    def _quick_login(self, instance_id: str, core_port: int) -> dict[str, Any]:
        instance = self._get(instance_id)
        if not instance.last_account():
            raise ValueError("该实例没有可用的上次登录记录，请先扫码登录")
        if not instance.to_dict()["running"]:
            self.start(instance_id, core_port, auto_login=False)
        host_config = json.loads((instance.path / "config.json").read_text(encoding="utf-8"))
        port = int(host_config.get("port") or instance.port)
        request = urllib.request.Request(f"http://127.0.0.1:{port}/quick-login", method="POST",
            headers={"Authorization": "Bearer " + str(host_config.get("bridgeToken") or "")})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=18) as response:
                result = json.loads(response.read(65536).decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                message = json.loads(exc.read(65536).decode("utf-8")).get("error")
            except (UnicodeDecodeError, json.JSONDecodeError):
                message = ""
            raise RuntimeError(message or f"快捷登录失败（HTTP {exc.code}）") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise RuntimeError("无法连接实例登录服务，请确认实例已启动") from exc
        if not isinstance(result, dict) or not result.get("ok"):
            raise RuntimeError(str(result.get("error") if isinstance(result, dict) else "快捷登录响应异常"))
        return {"account": instance.to_dict(), "accepted": bool(result.get("accepted")),
                "already_logged_in": bool(result.get("already_logged_in"))}

    def refresh(self, instance_id: str) -> dict[str, Any]:
        instance = self._get(instance_id)
        data = instance.to_dict()
        if not data.get("running"):
            raise RuntimeError("headless client is not running")
        port = int(data.get("port") or instance.port)
        host_config = json.loads((instance.path / "config.json").read_text(encoding="utf-8"))
        request = urllib.request.Request(f"http://127.0.0.1:{port}/refresh", method="POST",
            headers={"Authorization": "Bearer " + str(host_config.get("bridgeToken") or "")})
        try:
            with urllib.request.urlopen(request, timeout=8) as response:  # noqa: S310 - loopback only
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"headless host refresh failed: {exc}") from exc
        if not payload.get("ok"):
            raise RuntimeError(str(payload.get("error") or "QR refresh rejected"))
        time.sleep(0.3)
        return instance.to_dict()

    def _pid_alive(self, pid: int) -> bool:
        if not pid:
            return False
        if os.name == "nt":
            import ctypes

            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        try:
            os.kill(int(pid), 0)
            return True
        except OSError:
            return False

    def _pids_on_port(self, port: int) -> list[int]:
        return pids_on_port(port)

    def _kill_pid(self, pid: int) -> None:
        kill_pid(pid)

    def stop(self, instance_id: str) -> dict[str, Any]:
        instance = self._get(instance_id)
        # Terminate the job first when there is one.
        #
        # It is more thorough than taskkill, which only walks a parent/child snapshot: a
        # process QQ spawned and re-parented (Chromium does this for utility processes) is
        # not in that tree any more, but it is still in the job. Terminating also keeps the
        # "one stop kills the whole tree" promise when PIDs have been reused.
        from .job_guard import release

        try:
            release(instance_id)
        except Exception as exc:  # noqa: BLE001 - taskkill 仍在后面兜底
            log.warn("Job Object 终止失败，改用 taskkill", error=str(exc))

        status = instance.status()
        candidates: set[int] = set()
        for value in (status.get("pid"), instance.pid):
            try:
                if value:
                    candidates.add(int(value))
            except (TypeError, ValueError):
                pass
        try:
            candidates.update(self._pids_on_port(int(status.get("port") or instance.port or 0)))
        except (TypeError, ValueError):
            pass
        for pid in candidates:
            self._kill_pid(pid)

        # Publish a stopped status immediately so the 5s table refresh cannot
        # resurrect a stale "running" row from an old heartbeat.
        #
        # 保留被踢的事实：整份重写会连 kicked_type / security_kicked_type / error
        # 一起抹掉，于是用户点「停止」后再也看不到"为什么被踢"。停止只该改变
        # "是否在运行"，不该改写"为什么停下来"。
        status_path = instance.path / "data" / "status.json"
        status_path.parent.mkdir(parents=True, exist_ok=True)
        stopped: dict[str, Any] = {"id": instance.id, "phase": "stopped", "pid": 0,
                                   "updated_at": time.time()}
        if str(status.get("phase") or "") == "kicked":
            stopped["previous_phase"] = "kicked"
            for key in ("kicked_type", "security_kicked_type", "same_device", "error"):
                if status.get(key) not in (None, ""):
                    stopped[key] = status[key]
        status_path.write_text(json.dumps(stopped), encoding="utf-8")
        meta_path = instance.path / "instance.json"
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                meta = {}
            meta["pid"] = 0
            meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        time.sleep(0.3)
        return instance.to_dict()

    def kernel_api(self, instance_id: str):
        """Returns the typed kernel API for a ``pipe`` instance.

        Only pipe instances have the injected DLL and therefore the control pipe.
        Headless instances run their own Node host, so they reach the same kernel
        services directly through ``wrapper.node`` -- ``headless_actions.cjs`` covers
        them, and routing those through a pipe would add a hop for nothing.

        Note the limitation: the pipe can only be *used* while QQ's JS thread is still
        running, which is a couple of seconds after injection. For work after that, see
        ``startup_api``.
        """
        from .transport.kernel_api import api_for_instance

        instance = self._get(instance_id)
        meta_path = instance.path / "instance.json"
        kind = ""
        if meta_path.is_file():
            try:
                kind = str(json.loads(meta_path.read_text(encoding="utf-8"))
                           .get("kind") or "")
            except (OSError, json.JSONDecodeError):
                kind = ""
        if kind and kind != "pipe":
            raise RuntimeError(
                f"实例 {instance_id} 是 {kind} 类型；kernel_api 只适用于 pipe 实例")

        api = api_for_instance(instance.path)
        if not api.ready(timeout=5):
            raise RuntimeError(
                f"实例 {instance_id} 的控制管道未就绪（{api.client.pipe_path}）。\n"
                "  可能原因：DLL 未注入、QQ 尚未启动完成、或 JS 运行时还没被接管。")
        return api

    def startup_api(self, instance_id: str):
        """Returns an API that runs work during the instance's startup window.

        Use this for anything that has to happen after launch. QQ's JS main thread only
        executes napi calls while it initialises and then goes idle, so a request made
        later cannot be serviced -- queued work has to be in place first:

            api = manager.startup_api("C1")
            api.add_methods("msg")
            api.write()            # before start()
            manager.start("C1")
            results = api.wait()

        ``write()`` before ``start()`` is the caller's step on purpose: doing it
        implicitly would produce a timeout that looks like a failure.
        """
        from .transport.kernel_api import StartupApi

        instance = self._get(instance_id)
        return StartupApi(instance.path)

    def delete(self, instance_id: str) -> bool:
        """删除实例：停掉它，再把目录移进 .trash。

        任何实例都可以删，包括 "Pipe"。

        早先 C0（后来是 Pipe）被禁止删除，理由是"由 QSICK 管理"。这条限制已经取消：
        它挡住了正常的清理动作——用户想换一台机器、或者数据目录被改坏了，想删掉
        重建，都被拦住还得手工删目录。删掉之后需要的实例会按既有规则重新创建
        （设备上一个实例都没有时，首次启动会建一个 pipe 实例），所以并非不可恢复。

        移进 .trash 而不是直接删：账号数据一旦误删无法找回，留一份在回收目录里
        比"干净"更重要，用户确认无误后可以自己清空。
        """
        instance = self._get(instance_id)
        self.stop(instance_id)
        trash = self.root / ".trash"
        trash.mkdir(exist_ok=True)
        shutil.move(str(instance.path), str(trash / f"{instance_id}-{int(time.time())}"))
        return True

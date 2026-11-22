"""Locate the native payload and make sure a QQ process is hosting it."""

from __future__ import annotations

import os
import time
from pathlib import Path

from .. import log as logmod
from . import injector, session

log = logmod.get_logger("Transport.Payload")

CANDIDATES = (
    "native/qsick-payload/qsick_payload.dll",
    "native/qsick-payload/build/qsick_payload.dll",
    "payload/qsick_payload.dll",
    "qsick_payload.dll",
)


def locate_payload(explicit: str = "") -> Path | None:
    if explicit:
        path = Path(explicit).expanduser()
        return path if path.exists() else None
    env = os.environ.get("QSICK_PAYLOAD", "")
    if env:
        path = Path(env).expanduser()
        if path.exists():
            return path
        log.warn("QSICK_PAYLOAD points at a missing file", path=str(path))
    root = Path(__file__).resolve().parents[3]
    for candidate in CANDIDATES:
        path = root / candidate
        if path.exists():
            return path
    return None


def attach(prefix: str = r"\\.\pipe\qsick", pid: int = 0, token: str = "") -> int:
    """Return a pid that hosts the payload, injecting if necessary."""
    if pid:
        return pid
    pids = injector.find_qq_pids()
    if not pids:
        raise injector.InjectError("no QQ.exe found; start QQ first")
    for candidate in reversed(pids):
        ctl_path, _ = session.pipe_names(prefix, candidate, token)
        if injector.wait_for_pipes(candidate, prefix, timeout=0.2) or _exists(ctl_path):
            log.info("found a resident payload", pid=candidate)
            return candidate
    payload = locate_payload()
    if payload is None:
        raise injector.InjectError(
            "payload not built: expected native/qsick-payload/target/release/qsick_payload.dll "
            "or set QSICK_PAYLOAD"
        )
    target = pids[-1]
    injector.inject_dll(target, payload)
    ctl_path, _ = session.pipe_names(prefix, target, token)
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        if injector.wait_for_pipes(target, prefix, timeout=0.2) or _exists(ctl_path):
            return target
        time.sleep(0.1)
    raise injector.InjectError("payload loaded but its pipes never appeared")


def _exists(path: str) -> bool:
    import os

    return os.path.exists(path)

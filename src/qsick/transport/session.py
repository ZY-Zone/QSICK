"""Per-session pipe tokens.

The payload cannot read the launcher's environment after QQ has started, so the
token is handed over through a session file in the temp directory: QSICK writes it
before injection, the payload reads it at bootstrap, and the pipe names + HELLO
nonce both carry it. The file is owner-scoped (0600) and one per PID.
"""

from __future__ import annotations

import os
import secrets
import tempfile
from pathlib import Path

TOKEN_BYTES = 16  # 128-bit -> 32 hex chars


def new_token() -> str:
    return secrets.token_hex(TOKEN_BYTES)


def session_dir(configured: str = "") -> Path:
    if configured:
        return Path(configured).expanduser()
    return Path(tempfile.gettempdir()) / "qsick" / "sessions"


def token_path(pid: int, configured: str = "") -> Path:
    return session_dir(configured) / f"{pid}.token"


def write_token(pid: int, token: str, configured: str = "") -> Path:
    path = token_path(pid, configured)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token.strip() + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def read_token(pid: int, configured: str = "") -> str | None:
    path = token_path(pid, configured)
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return token or None


def clear_token(pid: int, configured: str = "") -> None:
    try:
        token_path(pid, configured).unlink()
    except OSError:
        pass


def pipe_names(prefix: str, pid: int, token: str = "") -> tuple[str, str, str]:
    """Control (request), response and event pipe names.

    Requests and responses are split so a pending read can never collide with a
    write on the same handle (a real failure mode on synchronous Windows pipes).
    """
    base = prefix.rstrip("\\/")
    suffix = f".{token}" if token else ""
    return (
        f"{base}.{pid}{suffix}.ctl",
        f"{base}.{pid}{suffix}.rsp",
        f"{base}.{pid}{suffix}.evt",
    )

"""A dedicated console window for the QQ-client download.

Why a child process instead of a window in this process
-------------------------------------------------------
`start.bat` already owns a console, and `FreeConsole()` + `AllocConsole()` would give us a second
window at the cost of detaching the caller's console -- every later log line would vanish from the
window the operator is watching. So the download runs in a child started with `CREATE_NEW_CONSOLE`:
the parent keeps its console, the child gets its own window, and the parent just waits.

That works for both launch shapes, which is the point:

* source run   -- `sys.executable` is `python.exe`, a console-subsystem binary; the new console is
                  where its `print` output lands by itself.
* frozen exe   -- `QSICK.exe` was built with `--noconsole` (GUI subsystem), so Windows allocates the
                  console for the child but the CRT does not wire `sys.stdout` to it. The worker
                  therefore opens ``CONOUT$`` itself and writes there. Verified rather than assumed:
                  an unwired `print` in a GUI-subsystem child goes nowhere at all.

Nothing here is allowed to be fatal. If the window cannot be created -- no console permission, a
non-Windows host, a spawn failure -- the caller falls back to downloading in-process, which is what
it did before this module existed.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

#: Set to `1` to keep the download silent and in-process.
#:
#: `获取QQnt.bat` sets this: it already runs inside a console whose whole purpose is to show this
#: download, so opening a second window would put the progress somewhere the operator is not
#: looking and leave their window blank.
DISABLE_ENV = "QSICK_NO_FETCH_CONSOLE"

#: Set to `1` inside the child, so it never tries to spawn another child.
WORKER_ENV = "QSICK_FETCH_WORKER"

#: Title of the window. Kept short: Windows truncates it in the taskbar.
WINDOW_TITLE = "QSICK - 下载 QQ 客户端"


def disabled() -> bool:
    return os.environ.get(DISABLE_ENV, "").strip().lower() in ("1", "true", "yes")


def is_worker() -> bool:
    return os.environ.get(WORKER_ENV, "").strip() == "1"


def console_available() -> bool:
    """Whether a console window can be created at all on this host."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        return bool(ctypes.windll.kernel32.GetConsoleWindow()) or True
    except Exception:  # noqa: BLE001
        return False


class _Tee:
    """Writes every chunk to several streams, ignoring the ones that fail.

    Needed because the two outputs are not the same thing: the window is what the operator watches,
    the log file is what survives the window closing. Dropping either to get the other is worse
    than keeping both.
    """

    def __init__(self, *streams) -> None:
        self.streams = streams

    def write(self, text: str) -> int:
        for stream in self.streams:
            try:
                stream.write(text)
            except Exception:  # noqa: BLE001 - one dead sink must not break the other
                pass
        return len(text)

    def flush(self) -> None:
        for stream in self.streams:
            try:
                stream.flush()
            except Exception:  # noqa: BLE001
                pass

    def fileno(self):
        raise OSError("tee 没有文件描述符")


def _stdout_usable() -> bool:
    """Whether `print` already reaches the console this process was given.

    A console-subsystem child (source runs, `python -m qsick.fetch_worker`) inherits a working
    `sys.stdout`. Replacing it with ``CONOUT$`` would still show the same text, but it would also
    write around any redirection the caller set up — which is how a piped run ended up printing
    nothing at all.
    """
    stream = sys.stdout
    if stream is None:
        return False
    try:
        stream.fileno()
    except Exception:  # noqa: BLE001 - no fileno means it cannot reach the console
        return False
    try:
        stream.write("")
        stream.flush()
    except Exception:  # noqa: BLE001
        return False
    return True


def _stdout_is_console() -> bool:
    """Whether `sys.stdout` is already the console itself, rather than a file standing in for it.

    This is the distinction that broke the frozen worker: `packaging/launcher.py` points
    `sys.stdout` at ``logs/console.log``, so `_stdout_usable()` was true and the function concluded
    there was nothing to do -- while the window it had just been given stayed blank. A file has a
    fileno too, so fileno alone cannot tell them apart; `GetFileType` can.
    """
    stream = sys.stdout
    if stream is None:
        return False
    try:
        import ctypes
        import msvcrt

        handle = msvcrt.get_osfhandle(stream.fileno())
        FILE_TYPE_CHAR = 0x0002
        return ctypes.windll.kernel32.GetFileType(handle) == FILE_TYPE_CHAR
    except Exception:  # noqa: BLE001
        return False


def attach_output(title: str = WINDOW_TITLE) -> bool:
    """Make `print` reach this process's console window.

    The frozen build is a GUI-subsystem binary: Windows gives the child a console (that is what
    `CREATE_NEW_CONSOLE` does) but the C runtime leaves `sys.stdout` as ``None``, so `print()` is a
    no-op. Writing to ``CONOUT$`` reaches the window regardless of subsystem.

    Returns True only if output now reaches the console.
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetConsoleWindow()
        if not handle:
            # No console at all (e.g. started without CREATE_NEW_CONSOLE). Make one.
            if not kernel32.AllocConsole():
                return False
        try:
            kernel32.SetConsoleTitleW(title)
        except Exception:  # noqa: BLE001 - a title is cosmetic
            pass

        if _stdout_is_console():
            # Already writing to this console; adding CONOUT$ would print everything twice.
            return True

        stream = open("CONOUT$", "w", encoding="utf-8", errors="replace", buffering=1)
        # Keep the original sink (the log file, in the frozen build) alongside the window.
        sys.stdout = _Tee(stream, sys.stdout) if _stdout_usable() else stream
        sys.stderr = sys.stdout
        return True
    except Exception:  # noqa: BLE001 - falling back to the log file is acceptable
        return False


def worker_command(runtime: Path, base_url: str = "") -> list[str] | None:
    """The command line that re-runs this program in worker mode.

    Two shapes, because the two launches are genuinely different programs:

    * frozen -- the exe itself, with ``--fetch-worker``, which `packaging/launcher.py` dispatches.
    * source -- ``-m qsick.fetch_worker``. ``PYTHONPATH`` is carried over so a source checkout that
      is *not* installed still resolves ``qsick`` in the child.

    ``base_url`` is carried across when the caller pinned one source, so a server-only download
    does not quietly reopen the GitHub route in the child -- the child would otherwise have no way
    to know the parent had already ruled GitHub out.
    """
    target = str(runtime)
    extra = ["--base", base_url] if base_url else []
    if getattr(sys, "frozen", False):
        return [sys.executable, "--fetch-worker", target, *extra]
    package_root = Path(__file__).resolve().parents[1]
    env_path = os.environ.get("PYTHONPATH", "")
    parts = [str(package_root)] + ([env_path] if env_path else [])
    os.environ["PYTHONPATH"] = os.pathsep.join(parts)
    return [sys.executable, "-m", "qsick.fetch_worker", target, *extra]


def spawn(runtime: Path, base_url: str = "") -> subprocess.Popen | None:
    """Start the worker in its own console window. Returns None if it could not be started."""
    if disabled() or is_worker() or sys.platform != "win32":
        return None
    command = worker_command(runtime, base_url)
    if not command:
        return None
    env = dict(os.environ)
    env[WORKER_ENV] = "1"
    try:
        return subprocess.Popen(  # noqa: S603 - the command is built here, not from input
            command,
            env=env,
            creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
            close_fds=False,
        )
    except Exception:  # noqa: BLE001 - no window is not a failure; the caller downloads inline
        return None

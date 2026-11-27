"""Entry point for the frozen build: `QSICK.exe`.

Why a separate launcher and not `qsick.__main__:main` directly
--------------------------------------------------------------
QSICK resolves most of its directories from `Path(__file__)`: `parents[2]` of a module under
`src/qsick/` is the project root, and the protocol registry, the native DLLs, `runtime/`, `temp/`
and `downloads/` all hang off that. Inside a PyInstaller bundle `__file__` points into the extracted
temp directory, so those paths would resolve somewhere that is deleted on exit.

The build therefore ships the *source tree* (tracked files only, at `<app>/src/`) instead of freezing
the package into the archive, and this launcher puts that directory on `sys.path` before importing.
The result is that a frozen run sees exactly the layout a checkout has: `<app>/src/qsick/...`, so
`parents[2]` is `<app>` and every one of those paths lands next to the exe.

What this replaces
------------------
`start.bat` and `隐藏窗口启动.bat` together. What they did, and what is done here instead:

    start.bat                                    this launcher
    ------------------------------------------   ------------------------------------------------
    cd /d "%~dp0"                                chdir to the exe's directory
    uv sync / uv run qsick                       frozen interpreter, nothing to resolve
    probe 127.0.0.1:5754, open browser if up      same, and exit instead of starting a 2nd copy
    start "" http://127.0.0.1:5754/ (after 3 s)   same delay, then the browser opens
    echo banner                                  written to logs/console.log (there is no console)
    pause on failure                             a message box, and the log path is in it
    隐藏窗口启动.bat: no console window            built with --noconsole
    first run --check / uv missing               not applicable (no uv at runtime)
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import traceback
from pathlib import Path

#: Seconds to wait before opening the browser. `start.bat` waits 3; the WebUI binds late enough that
#: a shorter delay shows a connection error to the operator.
BROWSER_DELAY = 3.0

#: The port `start.bat` probes. Kept in one place so the two checks cannot disagree.
PROBE_TIMEOUT = 2.0


def app_dir() -> Path:
    """The directory the exe lives in -- the frozen analogue of the repository root.

    Deliberately not `sys._MEIPASS`: that is the one-file extraction directory when bundled as
    one-file, and it is deleted on exit. Everything writable has to be beside the exe.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    # Running from source: `packaging/launcher.py` -> repository root.
    return Path(__file__).resolve().parents[1]


def resolve_src(root: Path) -> Path | None:
    """Where the shipped `qsick` package lives.

    `<root>/src` is what `build.ps1` writes. `_MEIPASS/src` is the fallback for a hypothetical
    one-file build, where the tree was added to the archive instead of copied beside the exe.
    """
    for candidate in (root / "src", Path(getattr(sys, "_MEIPASS", "")) / "src"):
        if (candidate / "qsick" / "__init__.py").is_file():
            return candidate
    return None


def redirect_streams(root: Path) -> Path:
    """Point stdout/stderr at a file.

    A windowed build has no console: without this, every `print`, traceback and uvicorn log line is
    discarded, and a failure to start looks like nothing happening at all.
    """
    logs = root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    path = logs / "console.log"
    try:
        handle = open(path, "a", encoding="utf-8", buffering=1)
    except OSError:
        # A read-only install directory must not stop the app from starting.
        return path
    sys.stdout = handle
    sys.stderr = handle
    print(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} QSICK launch ===", file=handle)
    return path


def qsick_is_running(host: str, port: int) -> bool:
    """Whether a QSICK WebUI already answers on this port.

    `start.bat` used the same probe: a plain TCP connect is not enough, because the port may belong
    to something else (the script's message names SnowLuma), so the reply has to say QSICK.
    """
    import urllib.error
    import urllib.request

    url = f"http://{host}:{port}/"
    try:
        with urllib.request.urlopen(url, timeout=PROBE_TIMEOUT) as response:
            body = response.read(2048).decode("utf-8", "replace").lower()
    except urllib.error.HTTPError as exc:
        # Any HTTP answer at all means *something* is listening; only the body tells us what.
        try:
            body = exc.read(2048).decode("utf-8", "replace").lower()
        except Exception:  # noqa: BLE001
            return False
    except (urllib.error.URLError, socket.timeout, OSError):
        return False
    return "qsick" in body


def open_browser(url: str) -> None:
    try:
        os.startfile(url)  # noqa: S606 - opening a URL is the whole point
    except OSError:
        webbrowser_fallback(url)


def webbrowser_fallback(url: str) -> None:
    try:
        import webbrowser

        webbrowser.open(url)
    except Exception:  # noqa: BLE001 - a browser that will not open is not fatal
        pass


def schedule_browser(url: str) -> None:
    """Open the browser shortly after start, without blocking the server.

    A thread rather than a timer: `qsick.__main__.main()` hands control to `asyncio.run` almost
    immediately, and a `threading.Timer` would keep the process alive for its duration on an early
    exit. A daemon thread dies with the process.
    """
    import threading

    def run() -> None:
        time.sleep(BROWSER_DELAY)
        open_browser(url)

    threading.Thread(target=run, name="qsick-open-browser", daemon=True).start()


def show_error(root: Path, log_path: Path, detail: str) -> None:
    """A windowed process has nowhere to print, so a failure needs a dialog to be seen at all."""
    message = (
        f"QSICK 启动失败。\n\n{detail}\n\n日志：{log_path}"
    )
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, "QSICK", 0x10)
    except Exception:  # noqa: BLE001 - on a non-Windows host there is no dialog to show
        print(message)


def main(argv: list[str] | None = None) -> int:
    root = app_dir()
    # Every cwd-relative directory QSICK owns (`data`, `logs`, `headlessClient`, `plugins`, `runtime`)
    # is relative to this, matching how `start.bat` did `cd /d "%~dp0"`.
    os.chdir(root)
    log_path = redirect_streams(root)

    src = resolve_src(root)
    if src is None:
        detail = f"找不到源码目录：{root / 'src'}"
        show_error(root, log_path, detail)
        return 2
    # Insert rather than append: the bundle's own directory must not shadow `qsick` or `protocols`.
    sys.path.insert(0, str(src))
    # `protocols/` is imported by name, and `protocol_bridge.ensure_importable()` adds it too; doing it
    # here as well means a failure there cannot leave an unimportable registry.
    protocols = root / "protocols"
    if protocols.is_dir():
        sys.path.insert(0, str(protocols))

    try:
        from qsick import __version__
        from qsick.__main__ import main as qsick_main
        from qsick.config import load_config
    except Exception:  # noqa: BLE001 - an import error is the most likely first-run failure
        detail = traceback.format_exc()
        print(detail)
        show_error(root, log_path, detail.strip().splitlines()[-1])
        return 3

    # The WebUI port and host come from the config, because the already-running check has to probe
    # the same address the server will bind.
    try:
        config = load_config()
        host, port = config.webui.host, int(config.webui.port)
    except Exception:  # noqa: BLE001 - a broken config is the server's problem to report, not ours
        host, port = "127.0.0.1", 5754

    url = f"http://{host}:{port}/"
    # The already-running check only applies to a bare launch (a double-click).
    #
    # Measured: `QSICK.exe --version` was hijacked by this probe and opened the running instance's
    # page instead of printing the version, because 5754 answered. An explicit argument means the
    # operator wants that command, not the default launch path.
    arguments = argv if argv is not None else sys.argv[1:]
    if not arguments and qsick_is_running(host, port):
        # `start.bat` did the same and waited for a keypress; with no console the only sensible
        # action is to show the instance that is already there.
        print(f"QSICK already answering on {url}; opening it instead of starting a second copy")
        open_browser(url)
        return 0

    print(f"QSICK {__version__} starting; WebUI {url}")
    schedule_browser(url)
    try:
        code = qsick_main(arguments)
    except SystemExit as exc:
        code = int(exc.code or 0)
    except Exception:  # noqa: BLE001
        detail = traceback.format_exc()
        print(detail)
        show_error(root, log_path, detail.strip().splitlines()[-1])
        return 1
    if code:
        show_error(root, log_path, f"qsick 退出码 {code}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())

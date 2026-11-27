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


def show_info(title: str, message: str) -> None:
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, title, 0x40)
    except Exception:  # noqa: BLE001
        print(message)


#: `QSICK.exe --fetch-runtime` replaces `获取QQnt.bat`.
#:
#: The two are not the same path. QSICK already fetches the client by itself at startup
#: (`qsick/runtime_fetch.py`): it downloads the project's pre-built tarball, resumes with HTTP Range
#: and checks a sha256. `qqnt_get.py` is the other route -- it downloads the *official Tencent
#: installer* and unpacks the embedded 7z in pure Python, and it accepts `--source` for a file that
#: is already on disk. That second route is the one that works when the release server is
#: unreachable or the machine is offline, which is exactly when a frozen distribution needs it.
FETCH_FLAG = "--fetch-runtime"

#: Arguments that must never wait on the client download.
#:
#: `--version` / `--help` answer immediately and have nothing to do with the QQ client; `--no-serve`
#: is the test/CI switch. Gating those behind a 480 MB download would make a trivial question cost
#: several minutes.
GATE_EXEMPT_FLAGS = ("--version", "-h", "--help", "--no-serve", FETCH_FLAG, "--fetch-worker")

#: Internal: download the client in this process, with this process's console window.
#:
#: `qsick.fetch_console` starts the exe with this flag and `CREATE_NEW_CONSOLE`, which is how the
#: download gets its own window without detaching the console of whatever started us.
WORKER_FLAG = "--fetch-worker"


def runtime_gate_needed(config: object, arguments: list[str]) -> bool:
    """Whether this launch needs the QQ client on disk before it can be useful.

    Only `headless` and `pipe` do: they attach to a real QQ process, and `headless` starts instances
    from `runtime/`. `mock` runs a synthetic runtime and needs nothing -- gating it would make a
    smoke test pay for a 480 MB download it will never touch.
    """
    mode = str(getattr(getattr(config, "transport", None), "mode", "") or "").lower()
    # The CLI flags win over the file, exactly as `qsick.__main__.apply_args` does it.
    if "--mock" in arguments:
        mode = "mock"
    if "--pipe" in arguments:
        mode = "pipe"
    return mode in ("headless", "pipe")


def ensure_client_ready(root: Path, log_path: Path) -> tuple[bool, str]:
    """Block until `runtime/` is a complete client, or explain why it could not become one.

    The client is not optional for headless/pipe: without `wrapper.node` every instance fails at
    login with an error that names nothing useful. So this runs *before* the server starts rather
    than alongside it -- an earlier version kicked the download off in a background task and started
    serving immediately, which meant the WebUI was up while anything that needed QQ silently failed.

    Order inside `runtime_fetch`: GitHub installer first (5 s probe, then give up on it), then the
    project's own release server.
    """
    try:
        from qsick import runtime_fetch
        from qsick.headless import default_runtime_dir
    except Exception:  # noqa: BLE001
        detail = traceback.format_exc()
        print(detail)
        return False, detail.strip().splitlines()[-1]

    runtime = default_runtime_dir()
    if runtime_fetch.runtime_ready(runtime):
        print(f"client ready: {runtime}")
        return True, ""

    print(f"client missing at {runtime}; fetching before start")
    try:
        did_download = runtime_fetch.ensure_runtime(runtime, console=True)
    except Exception as exc:  # noqa: BLE001 - the reason is what the operator needs
        print(f"client fetch failed: {type(exc).__name__}: {exc}")
        return False, f"{type(exc).__name__}: {exc}"

    if not runtime_fetch.runtime_ready(runtime):
        # Two distinct cases, and conflating them produces a misleading message:
        # `False` from ensure_runtime means it never tried (download disabled), while `True` with an
        # incomplete directory means a source claimed success and lied.
        if not did_download:
            print("client fetch was skipped (runtime download disabled) and runtime/ is incomplete")
            return False, "runtime\\ 不完整，且自动下载已被关闭（QSICK_SKIP_RUNTIME_DOWNLOAD）"
        print("client fetch reported success but runtime/ is still incomplete")
        return False, "下载完成但 runtime/ 仍不完整"
    print(f"client ready: {runtime}")
    return True, ""


def run_runtime_fetch(root: Path, log_path: Path, arguments: list[str]) -> int:
    script = root / "qqnt_get.py"
    if not script.is_file():
        detail = f"缺少 qqnt_get.py：{script}"
        print(detail)
        show_error(root, log_path, detail)
        return 2
    # `qqnt_get` resolves its default output directory from `__file__`, so importing it from this
    # directory makes `<app>/runtime` the default without any argument.
    sys.path.insert(0, str(root))
    try:
        import qqnt_get
    except Exception:  # noqa: BLE001
        detail = traceback.format_exc()
        print(detail)
        show_error(root, log_path, detail.strip().splitlines()[-1])
        return 3

    print(f"fetching the QQ NT client into {root / 'runtime'}")
    try:
        code = int(qqnt_get.main(arguments) or 0)
    except SystemExit as exc:
        # argparse's `--help` and its own errors both land here. Neither is a fetch result, so
        # neither should be dressed up as one -- `--help` exited 0, an error is non-zero.
        code = int(exc.code or 0)
    except Exception:  # noqa: BLE001
        detail = traceback.format_exc()
        print(detail)
        show_error(root, log_path, detail.strip().splitlines()[-1])
        return 1
    # No console means the download progress was invisible; say how it ended.
    if code == 0:
        # ...but only when something was actually fetched.
        #
        # Measured: `QSICK.exe --fetch-runtime --help` hung forever, because argparse answered the
        # help, exited 0, and this dialog then waited for a click that a script would never give.
        # Any argument-only invocation is exempt; a real fetch still gets its confirmation.
        informational = any(flag in arguments for flag in ("--help", "-h"))
        if not informational:
            show_info("QSICK", f"QQ NT 客户端已就绪：\n{root / 'runtime'}")
    else:
        show_error(root, log_path, f"获取 QQ NT 失败，退出码 {code}")
    return code


def main(argv: list[str] | None = None) -> int:
    root = app_dir()
    # Every cwd-relative directory QSICK owns (`data`, `logs`, `headlessClient`, `plugins`, `runtime`)
    # is relative to this, matching how `start.bat` did `cd /d "%~dp0"`.
    os.chdir(root)
    log_path = redirect_streams(root)

    arguments = list(argv if argv is not None else sys.argv[1:])
    # Dispatched before anything else: this command must not care whether a QSICK instance is already
    # running, and it must not try to start one.
    if arguments and arguments[0] == FETCH_FLAG:
        return run_runtime_fetch(root, log_path, arguments[1:])

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

    # Worker mode: this process exists only to download the client into its own console window.
    # Dispatched after `sys.path` is ready (it imports `qsick`), and before anything server-shaped.
    #
    # The env var is set here as well as by the spawner: `--fetch-worker` typed by hand would
    # otherwise reach `ensure_runtime(console=True)`, which spawns another worker, which ...
    if arguments and arguments[0] == WORKER_FLAG:
        os.environ["QSICK_FETCH_WORKER"] = "1"
        try:
            from qsick.fetch_worker import main as worker_main
        except Exception:  # noqa: BLE001
            detail = traceback.format_exc()
            print(detail)
            show_error(root, log_path, detail.strip().splitlines()[-1])
            return 3
        return worker_main(arguments[1:])

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
    if not arguments and qsick_is_running(host, port):
        # `start.bat` did the same and waited for a keypress; with no console the only sensible
        # action is to show the instance that is already there.
        print(f"QSICK already answering on {url}; opening it instead of starting a second copy")
        open_browser(url)
        return 0

    print(f"QSICK {__version__} starting; WebUI {url}")
    # The client gate, before the server starts.
    #
    # Deliberately here rather than before the already-running check: if an instance is already
    # serving, this launch is only going to open its page, and making it download 480 MB first would
    # be absurd.
    if runtime_gate_needed(config, arguments) and not any(
            flag in arguments for flag in GATE_EXEMPT_FLAGS):
        ready, detail = ensure_client_ready(root, log_path)
        if not ready:
            show_error(root, log_path,
                       f"QQ 客户端未就绪，已拒绝启动。\n\n{detail}\n\n"
                       f"可以手动重试：QSICK.exe --fetch-runtime")
            return 4

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

"""Child process that downloads the QQ client in its own console window.

Started by :func:`qsick.fetch_console.spawn`; not meant to be run by hand. Its whole job is to give
the download a window whose output the operator can watch, then report success or failure through
its exit code so the parent can decide whether to start QSICK.

Exit codes:

    0   runtime/ is complete
    1   the download failed (the reason is printed in the window)
    2   bad arguments
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from . import fetch_console

#: Printed after the last line so a window that is about to close still shows the outcome. Windows
#: tears the console down the instant the process exits, and the final message is the one worth
#: reading.
FINAL_HOLD_SECONDS = 2.5


def _say(message: str) -> None:
    print(message, flush=True)


def main(argv: list[str] | None = None) -> int:
    arguments = list(argv if argv is not None else sys.argv[1:])

    # `<dir> [--base URL]`. `--base` pins one source: the caller already decided which one to use
    # (the "server only" launcher does), and re-deriving that here would silently undo the choice.
    base_url = ""
    if "--base" in arguments:
        index = arguments.index("--base")
        try:
            base_url = arguments[index + 1]
        except IndexError:
            print("--base 缺少地址", file=sys.stderr)
            return 2
        del arguments[index:index + 2]

    if not arguments:
        print("用法: <exe> --fetch-worker <runtime 目录> [--base 服务器地址]", file=sys.stderr)
        return 2

    runtime = Path(arguments[0])
    # A GUI-subsystem child has sys.stdout == None; this is what makes the window show anything.
    attached = fetch_console.attach_output()

    _say("=" * 60)
    _say("  正在下载 QQ 客户端")
    _say(f"  目标: {runtime}")
    _say(f"  来源: {base_url or 'GitHub -> 服务器（按优先级）'}")
    if not attached:
        _say("  （无法写入控制台，进度只记录在 logs\\console.log）")
    _say("=" * 60)
    _say("")

    from . import runtime_fetch

    # Route the download's progress into this window, and keep the log file in sync.
    runtime_fetch.set_progress_sink(_say)
    started = time.time()
    try:
        runtime_fetch.ensure_runtime(runtime, base_url=base_url or None)
    except Exception as exc:  # noqa: BLE001 - the message is the point
        _say("")
        _say(f"[失败] {type(exc).__name__}: {exc}")
        _say(f"耗时 {time.time() - started:.0f} 秒")
        time.sleep(FINAL_HOLD_SECONDS)
        return 1
    finally:
        runtime_fetch.set_progress_sink(None)

    if not runtime_fetch.runtime_ready(runtime):
        _say("")
        _say("[失败] 下载结束，但 runtime/ 仍不完整")
        time.sleep(FINAL_HOLD_SECONDS)
        return 1

    _say("")
    _say(f"[完成] 用时 {time.time() - started:.0f} 秒 -> {runtime}")
    _say("这个窗口会自动关闭，QSICK 即将启动。")
    time.sleep(FINAL_HOLD_SECONDS)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

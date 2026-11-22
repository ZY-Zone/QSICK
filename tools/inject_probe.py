"""Inject a chosen DLL into QQ.exe and watch it for a while.

This is the control experiment the crash investigation needs. Rebuilding the payload and watching it die
does not distinguish "our DLL corrupts the heap" from "injection into this client crashes regardless of
payload", nor from "this particular launch sequence is what breaks it". Swapping one variable at a time does.

The three launch modes exist because they turned out to be the difference:

    suspended  CREATE_SUSPENDED -> inject -> resume. The process never runs before the DLL is in place.
               This is what `transport.inject.launch_and_inject` does, and it is the mode that works.
    freeze     start normally, freeze every thread, inject, resume. This is what `_spawn_and_inject` does.
    plain      start normally and inject without freezing.

Usage:

    python tools/inject_probe.py                              # qsick_pipe.dll, suspended, 90 s
    python tools/inject_probe.py --dll blank_probe.dll        # a blank payload
    python tools/inject_probe.py --mode freeze                # reproduce the app's launch
    python tools/inject_probe.py --no-inject                  # no payload at all

Death is measured as "all QQ.exe processes gone", which is what the operator sees.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qsick.transport import inject as injector  # noqa: E402
from qsick.transport import injector as threadctl  # noqa: E402 - suspend/resume live here

NATIVE = ROOT / "native" / "qsick-pipe"
PIPE = ROOT / "headlessClient" / "Pipe"
QQ = PIPE / "QQ.exe"


def stage_payload(dll: Path, template: Path | None = None) -> tuple[Path, Path]:
    """Copy the DLL somewhere with a config beside it, as the real launcher does.

    The payload reads `qsick_pipe.json` from its own directory at attach time. Injecting it straight out of
    `native/qsick-pipe/` therefore ran it without any configuration -- a state production never produces.

    `template` copies a real session's config verbatim instead of synthesising one, so a difference in config
    content can be tested on its own rather than guessed at.

    Returns `(dll path to inject, owndata dir for it)`.
    """
    stage = ROOT / "temp" / "inject-probe" / dll.stem
    stage.mkdir(parents=True, exist_ok=True)
    target = stage / dll.name
    shutil.copy2(dll, target)

    if template is not None:
        config = json.loads(template.read_text(encoding="utf-8"))
        owndata = Path(str(config.get("owndata") or "")).expanduser()
        (stage / "qsick_pipe.json").write_text(
            json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        return target, owndata

    owndata = stage / "owndata"
    owndata.mkdir(parents=True, exist_ok=True)
    (stage / "qsick_pipe.json").write_text(
        json.dumps({
            "session": {"tmp_id": "probe", "port": 7540, "nowport": 7540},
            "owndata": str(owndata.resolve()),
            "port": 7540,
            "nowport": 7540,
            "corePort": 5754,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    return target, owndata


def qq_alive() -> int:
    out = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq QQ.exe", "/NH"],
        capture_output=True, text=True, timeout=30, check=False,
    ).stdout
    return out.lower().count("qq.exe")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dll", default="qsick_pipe.dll")
    parser.add_argument("--no-inject", action="store_true")
    parser.add_argument("--watch", type=float, default=90.0)
    parser.add_argument("--user-data", default="")
    parser.add_argument("--hooks", default="",
                        help="QSICK_PIPE_HOOKS value: none / all / a subset")
    parser.add_argument("--env", action="append", default=[],
                        help="extra KEY=VALUE for the launched process; repeatable")
    parser.add_argument("--mode", choices=("suspended", "freeze", "plain"), default="suspended",
                        help="how the DLL is injected")
    parser.add_argument("--no-sandbox", action="store_true",
                        help="pass --no-sandbox, as QSICK_PIPE_NO_SANDBOX=1 does in the app")
    parser.add_argument("--config", default="",
                        help="use a real session's qsick_pipe.json verbatim")
    parser.add_argument("--job", action="store_true",
                        help="bind the process to a Job Object, as the app does")
    args = parser.parse_args()

    subprocess.run(["taskkill", "/F", "/IM", "QQ.exe"],
                   capture_output=True, check=False, timeout=30)
    time.sleep(2)

    user_data = Path(args.user_data) if args.user_data else ROOT / "temp" / "probe-userdata"
    user_data.mkdir(parents=True, exist_ok=True)

    extra_env: dict[str, str] = {}
    if args.hooks:
        extra_env["QSICK_PIPE_HOOKS"] = args.hooks
    for item in args.env:
        if "=" in item:
            key, _, value = item.partition("=")
            extra_env[key.strip()] = value.strip()

    label = "none" if args.no_inject else f"{args.dll} [{args.mode}]"
    print(f"  launch {QQ.name}, payload = {label}"
          + (f", env={extra_env}" if extra_env else ""))

    arguments = f"--user-data-dir={user_data}"
    if args.no_sandbox:
        arguments = "--no-sandbox " + arguments

    started = time.monotonic()
    injection = None

    if args.no_inject:
        pid = subprocess.Popen([str(QQ), arguments], cwd=str(PIPE),
                               env={**os.environ, **extra_env}).pid
    elif args.mode == "suspended":
        # CREATE_SUSPENDED -> inject -> resume. The process never runs before the DLL is in place.
        staged, owndata = stage_payload(NATIVE / args.dll, Path(args.config) if args.config else None)
        injection = injector.launch_and_inject(
            QQ, staged, arguments=arguments, workdir=PIPE, extra_env=extra_env or None)
        pid = injection.pid
        print(f"  config dir {owndata}")
    else:
        staged, owndata = stage_payload(NATIVE / args.dll, Path(args.config) if args.config else None)
        env = {**os.environ, **extra_env}
        # The app points TEMP/TMP at the instance's owndata; mirror that, not the inherited values.
        temp = owndata / "temp"
        temp.mkdir(parents=True, exist_ok=True)
        env["TEMP"] = str(temp)
        env["TMP"] = str(temp)
        process = subprocess.Popen(
            [str(QQ), arguments], cwd=str(PIPE), env=env,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        pid = process.pid
        suspended = threadctl.suspend_process(pid) if args.mode == "freeze" else []
        if suspended:
            print(f"  froze {len(suspended)} threads")
        try:
            handle = injector.open_process(pid)
            if not handle:
                raise SystemExit("  cannot open process handle")
            injection = injector.Injection(pid=pid, hprocess=int(handle), hthread=0)
            injector.inject(injection, staged)
            print("  injected")
        finally:
            if suspended:
                threadctl.resume_process(suspended)
                print("  resumed")
        print(f"  config dir {owndata}")
    print(f"  pid={pid}")

    job = None
    if args.job:
        # The app binds every instance to a Job Object with KILL_ON_JOB_CLOSE. Reproduce it here: if that is
        # what kills QQ, the death pattern changes (children terminate one by one rather than the whole tree
        # going at once), and nothing else in this probe differs.
        from qsick.job_guard import acquire

        job = acquire(f"probe-{pid}")
        if job.active:
            ok = job.assign(pid)
            print(f"  job assigned: {ok}" + ("" if ok else f" ({job.error})"))
        else:
            print(f"  job unavailable: {job.error}")

    peak = 0
    died_at = None
    while time.monotonic() - started < args.watch:
        alive = qq_alive()
        peak = max(peak, alive)
        elapsed = time.monotonic() - started
        if alive == 0:
            died_at = elapsed
            break
        print(f"  +{elapsed:5.1f}s  QQ processes {alive}")
        time.sleep(5)

    if injection is not None:
        try:
            injector.close(injection)
        except Exception:  # noqa: BLE001
            pass
    subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                   capture_output=True, check=False, timeout=30)

    print()
    if died_at is None:
        print(f"  result: alive for the full {args.watch:.0f}s (peak {peak} processes)")
        return 0
    print(f"  result: all gone after {died_at:.1f}s (peak {peak})")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

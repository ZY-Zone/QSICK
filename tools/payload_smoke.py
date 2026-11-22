"""Integration smoke: load the C++ payload into a host process and speak the pipe protocol.

    uv run python tools/payload_smoke.py

Skips (exit 0) when the payload has not been built.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

from qsick.config import RuntimeConfig
from qsick.transport.pipe import PipeTransport
from qsick.transport.session import new_token

ROOT = Path(__file__).resolve().parents[1]
PAYLOAD_DIR = ROOT / "native" / "qsick-payload"
DLL = PAYLOAD_DIR / "qsick_payload.dll"
HOST = PAYLOAD_DIR / "payload_host.exe"


async def main() -> int:
    if os.name != "nt" or not DLL.exists() or not HOST.exists():
        print("skip: payload not built (run native/qsick-payload/build.bat)")
        return 0

    token = new_token()
    prefix = r"\\.\pipe\qsick-smoke"
    env = {
        **os.environ,
        "QSICK_PIPE_TOKEN": token,
        "QSICK_PIPE_PREFIX": prefix,
    }
    host = subprocess.Popen([str(HOST), str(DLL), "20"], env=env)
    try:
        pid = host.pid
        os.environ["QSICK_QQ_PID"] = str(pid)
        config = RuntimeConfig(data_dir=str(ROOT / "data" / "smoke"))
        config.transport.mode = "pipe"
        config.transport.pipe_prefix = prefix
        config.transport.pipe_auth = "fixed"
        config.transport.pipe_token = token
        config.transport.session_dir = str(ROOT / "data" / "smoke" / "sessions")
        transport = PipeTransport(config.transport)
        await transport.start()
        try:
            latency = await transport.ping()
            print(f"PASS handshake pid={pid} ping={latency:.1f}ms")
            result = await transport.send("login.info", {})
            print(f"PASS op roundtrip ok={result.ok} status={result.status} msg={result.message[:60]}")
        finally:
            await transport.stop()
    finally:
        host.terminate()
        try:
            host.wait(timeout=5)
        except subprocess.TimeoutExpired:
            host.kill()
    print("payload smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

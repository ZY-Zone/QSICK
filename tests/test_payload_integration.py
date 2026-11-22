"""Integration test for the compiled C++ payload (skips when it is not built).

Build first:

    native/qsick-payload/build.bat
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from qsick.config import RuntimeConfig
from qsick.transport.pipe import PipeTransport
from qsick.transport.session import new_token

ROOT = Path(__file__).resolve().parents[1]
PAYLOAD_DIR = ROOT / "native" / "qsick-payload"
DLL = PAYLOAD_DIR / "qsick_payload.dll"
HOST = PAYLOAD_DIR / "payload_host.exe"

pytestmark = pytest.mark.skipif(
    os.name != "nt" or not DLL.exists() or not HOST.exists(),
    reason="payload not built (run native/qsick-payload/build.bat)",
)


async def test_payload_handshake_and_ping(tmp_path: Path):
    token = new_token()
    prefix = rf"\\.\pipe\qsick-test-{os.getpid()}"
    env = {**os.environ, "QSICK_PIPE_TOKEN": token, "QSICK_PIPE_PREFIX": prefix}
    host = subprocess.Popen([str(HOST), str(DLL), "20"], env=env)
    previous_pid = os.environ.get("QSICK_QQ_PID")
    os.environ["QSICK_QQ_PID"] = str(host.pid)
    config = RuntimeConfig(data_dir=str(tmp_path))
    config.transport.mode = "pipe"
    config.transport.pipe_prefix = prefix
    config.transport.pipe_auth = "fixed"
    config.transport.pipe_token = token
    config.transport.session_dir = str(tmp_path / "sessions")
    transport = PipeTransport(config.transport)
    try:
        await transport.start()
        latency = await transport.ping()
        assert latency < 1000
        result = await transport.send("login.info", {})
        # The payload scaffold has no QQ binding yet: it must fail loudly, not pretend.
        assert result.ok is False
        assert result.status == 404
        assert "hook" in result.message or "scaffold" in result.message
    finally:
        await transport.stop()
        host.terminate()
        try:
            host.wait(timeout=5)
        except subprocess.TimeoutExpired:
            host.kill()
        if previous_pid is None:
            os.environ.pop("QSICK_QQ_PID", None)
        else:
            os.environ["QSICK_QQ_PID"] = previous_pid

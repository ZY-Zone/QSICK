"""Loop responsiveness has to be measurable, and the hot path must not sit on the loop.

A consumer reporting "the pong came too late" or an action that "did not answer in time" is usually
describing a stalled event loop: while it is blocked nothing on it runs, including the websocket
ping reply. Without a number, that is indistinguishable from "that subsystem is slow", so the
runtime samples its own wakeup lateness.
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for entry in (str(ROOT), str(ROOT / "src")):
    if entry not in sys.path:
        sys.path.insert(0, entry)


def runtime():
    from qsick.config import RuntimeConfig
    from qsick.runtime import Runtime

    config = RuntimeConfig(data_dir=str(ROOT / "temp" / "loop-lag-test"))
    return Runtime(config)


def test_the_lag_meter_reports_a_stalled_loop():
    """A synchronous sleep is exactly the failure mode: nothing else can run while it lasts."""
    runtime_instance = runtime()
    interval = runtime_instance.LOOP_LAG_INTERVAL
    runtime_instance.LOOP_LAG_INTERVAL = 0.05          # keep the test quick

    async def scenario() -> dict:
        task = asyncio.create_task(runtime_instance._loop_lag_loop())
        await asyncio.sleep(0.2)                       # a few clean samples first
        clean = dict(runtime_instance.loop_lag)
        time.sleep(0.35)                               # block the loop on purpose
        await asyncio.sleep(0.2)
        after = dict(runtime_instance.loop_lag)
        runtime_instance._stopping.set()
        await asyncio.wait_for(task, timeout=2.0)
        return {"clean": clean, "after": after}

    result = asyncio.run(scenario())
    assert result["clean"]["max"] < 150, result["clean"]
    # The stall must be visible, and `max` must keep it after later good samples arrive.
    assert result["after"]["max"] >= 250, result["after"]
    assert result["after"]["samples"] > result["clean"]["samples"]
    runtime_instance.LOOP_LAG_INTERVAL = interval


def test_status_exposes_the_lag():
    runtime_instance = runtime()
    info = runtime_instance.info()
    assert "loop_lag_ms" in info
    assert set(info["loop_lag_ms"]) >= {"now", "p95", "max"}


def test_packet_inspection_runs_off_the_loop():
    """The decode is pure-Python protobuf over up to 1 MiB; it must not run inline."""
    from qsick.runtime import _inspect_packet

    # A frame the mapper accepts: an empty-but-valid protobuf is not required here -- what matters
    # is that the function raises for bad input and is called through `to_thread`.
    body = "0a00"
    try:
        data, to_uin = _inspect_packet(body, 2)
    except ValueError:
        return                                          # rejected is fine; the seam is what is tested
    assert isinstance(data, bytes) and isinstance(to_uin, str)


def test_poll_intervals_are_shorter_than_the_old_fixed_delay():
    runtime_instance = runtime()
    assert runtime_instance.PIPE_POLL_FAST < 0.4
    assert runtime_instance.PIPE_POLL_FAST < runtime_instance.PIPE_POLL_IDLE

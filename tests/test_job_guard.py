"""Tests for tying a pipe instance's lifetime to QSICK.

The bug: closing QSICK left QQ running. Cause was structural -- `_spawn_and_inject` used
Popen, so the child was independent, and only a graceful `stop()` cleaned it up. Ctrl+C, a
crash, or closing the console left QQ alive.

The fix is a Job Object with KILL_ON_JOB_CLOSE, enforced by the kernel: when the last handle to
the job closes -- including on an abnormal exit -- every process in the job dies.

There is one subtlety the tests below pin down, because getting it wrong is worse than the
original bug: the guard must be owned by the *process*, not by a HeadlessManager. The WebUI
builds a manager per request, so a guard on the manager would be collected when the request
finished and the kernel would kill QQ on the spot. Measured before the fix: all four QQ
processes disappeared the moment the temporary manager was dropped.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import qsick.job_guard as job_guard
from qsick.job_guard import JobGuard


windows_only = pytest.mark.skipif(os.name != "nt", reason="Job Object 是 Windows 专有机制")


@pytest.fixture()
def clean_registry():
    """Start and end with an empty registry so tests cannot leak into each other."""
    job_guard.registry().clear()
    try:
        yield
    finally:
        job_guard.release_all()
        job_guard.registry().clear()


@windows_only
def test_job_is_created_with_kill_on_close():
    """The limit flag is the whole mechanism; without it the job does nothing."""
    guard = JobGuard()
    try:
        assert guard.active, f"创建 Job Object 失败：{guard.error}"
    finally:
        guard.close()


@windows_only
def test_inactive_guard_degrades_quietly():
    """Assignment and termination must not raise when the job could not be created.

    A machine where CreateJobObject fails should still run instances -- just without the
    lifetime guarantee -- rather than failing to start at all.
    """
    guard = JobGuard()
    guard.close()
    assert guard.active is False
    assert guard.assign(os.getpid()) is False
    assert guard.terminate() is False
    guard.close()  # idempotent


@windows_only
def test_assigning_a_bad_pid_reports_failure():
    guard = JobGuard()
    try:
        assert guard.active
        assert guard.assign(0) is False
        assert guard.assign(-1) is False
    finally:
        guard.close()


@windows_only
def test_close_is_idempotent():
    guard = JobGuard()
    assert guard.active
    guard.close()
    guard.close()
    assert guard.active is False


# -- the registry ----------------------------------------------------------

def test_acquire_returns_a_stable_guard_per_instance(clean_registry):
    first = job_guard.acquire("Pipe")
    again = job_guard.acquire("Pipe")

    assert first is again, "同一个实例必须复用同一个 job，否则旧句柄会泄漏"
    assert job_guard.registry()["Pipe"] is first


def test_acquire_replaces_a_closed_guard(clean_registry):
    first = job_guard.acquire("Pipe")
    first.close()

    second = job_guard.acquire("Pipe")

    assert second is not first
    assert job_guard.registry()["Pipe"] is second


def test_release_terminates_and_forgets(clean_registry):
    calls: list[str] = []

    class FakeJob:
        active = True

        def terminate(self) -> bool:
            calls.append("terminate")
            return True

        def close(self) -> None:
            calls.append("close")

    job_guard.registry()["Pipe"] = FakeJob()
    job_guard.release("Pipe")

    assert calls == ["terminate", "close"]
    assert "Pipe" not in job_guard.registry()


def test_release_closes_even_when_terminate_raises(clean_registry):
    """A failed terminate must still release the handle, or the job leaks."""
    calls: list[str] = []

    class BadJob:
        active = True

        def terminate(self) -> bool:
            calls.append("terminate")
            raise OSError("boom")

        def close(self) -> None:
            calls.append("close")

    job_guard.registry()["Pipe"] = BadJob()
    with pytest.raises(OSError):
        job_guard.release("Pipe")

    assert calls == ["terminate", "close"]
    assert "Pipe" not in job_guard.registry()


def test_release_of_unknown_instance_is_a_no_op(clean_registry):
    job_guard.release("没有这个实例")
    assert job_guard.registry() == {}


def test_release_all_clears_the_registry(clean_registry):
    closed: list[str] = []

    class FakeJob:
        active = True

        def __init__(self, name: str) -> None:
            self.name = name

        def terminate(self) -> bool:
            return True

        def close(self) -> None:
            closed.append(self.name)

    job_guard.registry()["Pipe"] = FakeJob("Pipe")
    job_guard.registry()["C0"] = FakeJob("C0")

    job_guard.release_all()

    assert sorted(closed) == ["C0", "Pipe"]
    assert job_guard.registry() == {}


def test_release_all_survives_a_broken_guard(clean_registry):
    """Shutdown must finish even if one guard misbehaves."""
    closed: list[str] = []

    class BadJob:
        active = True

        def terminate(self) -> bool:
            raise OSError("boom")

        def close(self) -> None:
            closed.append("bad-close")

    job_guard.registry()["Pipe"] = BadJob()
    job_guard.release_all()

    assert closed == ["bad-close"]
    assert job_guard.registry() == {}


def test_manager_does_not_own_the_guards():
    """The critical design point: guards are process-wide, not manager-owned.

    If a guard lived on HeadlessManager, the WebUI's per-request manager would take it down
    with it and QQ would die mid-session.
    """
    from qsick.headless import HeadlessManager

    manager = HeadlessManager(Path("headlessClient"))
    assert not hasattr(manager, "_pipe_jobs"), (
        "JobGuard 不能挂在 manager 上：WebUI 每次请求都新建 manager，"
        "句柄会随之关闭并杀掉 QQ")


@windows_only
def test_assigned_process_dies_when_the_job_closes():
    """The behaviour that matters, checked with a harmless child process.

    Spawning a real QQ is too heavy for a unit test, so this uses a sleeping Python process:
    if the kernel kills *it* when the job handle closes, the same will happen to QQ.
    """
    import subprocess
    import sys
    import time

    guard = JobGuard()
    if not guard.active:
        pytest.skip(f"Job Object 不可用：{guard.error}")

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        assert guard.assign(child.pid), guard.error
        # Closing the job is what triggers KILL_ON_JOB_CLOSE.
        guard.close()

        deadline = time.time() + 10
        while time.time() < deadline:
            if child.poll() is not None:
                break
            time.sleep(0.1)
        assert child.poll() is not None, "关闭 job 后子进程仍在运行"
    finally:
        if child.poll() is None:
            child.kill()
        guard.close()

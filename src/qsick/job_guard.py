"""Tie pipe instances to QSICK's lifetime with a Job Object.

The problem: closing QSICK leaves QQ running. Measured cause -- `_spawn_and_inject` uses
subprocess.Popen, so the child is independent; `CREATE_NEW_PROCESS_GROUP` reinforces that.
`stop()` cleans up correctly (taskkill /T /F removes the whole tree), but that only runs on a
graceful stop. Ctrl+C, a crash, or closing the console leaves QQ alive.

Why a Job Object rather than the alternatives:

  * a watcher thread calling taskkill on exit dies with the process, so a crash or a Task
    Manager kill leaves QQ behind -- the exact case being reported
  * having QQ poll the parent's pid means changing what runs inside the client
  * a job with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE is enforced by the kernel: when the last
    handle to the job closes -- including when the owning process is killed -- every process
    in the job is terminated. That is the property we actually want.

The job also has to be assigned *before* QQ spawns its own children, or the renderer processes
would be created outside the job. QQ creates them within milliseconds, so the assignment has to
happen immediately after Popen, before injection.

This module is Windows-only and degrades to a no-op elsewhere.
"""
from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

# -- constants ---------------------------------------------------------------

JobObjectExtendedLimitInformation = 9
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800

PROCESS_SET_QUOTA = 0x0100
PROCESS_TERMINATE = 0x0001

IS_WINDOWS = os.name == "nt"


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
        ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class JobGuard:
    """A kill-on-close job holding one instance's process tree.

    Keep a reference for as long as the process should live. When the last reference goes, the
    handle closes and the kernel terminates everything in the job.

    **Use `registry` rather than constructing this directly for instances.** The guard must
    outlive any single HeadlessManager: the WebUI builds a fresh manager per request, so a
    guard owned only by that object would close when the request finished and kill QQ
    immediately. Measured: dropping such a manager took all four QQ processes with it.
    """

    def __init__(self, name: str = "") -> None:
        self.handle: int | None = None
        self.name = name
        self.error: str = ""
        if not IS_WINDOWS:
            return
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            # An unnamed job per instance: no global name to leak, no collision between
            # instances, and each closes independently.
            handle = kernel32.CreateJobObjectW(None, name or None)
            if not handle:
                self.error = f"CreateJobObject 失败 (err={ctypes.get_last_error()})"
                return
            info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            # BREAKAWAY_OK as well as KILL_ON_JOB_CLOSE.
            #
            # Without it, every process QQ creates is forced into this job, including the ones Chromium
            # manages itself. Chromium puts its renderers and GPU process in its own job and relies on being
            # able to configure them; nested inside ours it cannot, and the children fail to start. Measured
            # with the injection probe: the same DLL and the same launch live 55 s (9 processes) with no job,
            # and die in 28 s (2 processes) with one -- the exact pattern the app shows.
            #
            # BREAKAWAY_OK keeps the guarantee that matters (closing QSICK kills the client) while letting a
            # child opt out when its own process management needs to.
            info.BasicLimitInformation.LimitFlags = (
                JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_BREAKAWAY_OK)
            ok = kernel32.SetInformationJobObject(
                wintypes.HANDLE(handle),
                JobObjectExtendedLimitInformation,
                ctypes.byref(info),
                ctypes.sizeof(info),
            )
            if not ok:
                self.error = (f"SetInformationJobObject 失败 "
                              f"(err={ctypes.get_last_error()})")
                kernel32.CloseHandle(handle)
                return
            self.handle = int(handle)
            self._kernel32 = kernel32
        except OSError as exc:  # pragma: no cover - defensive
            self.error = f"{type(exc).__name__}: {exc}"

    @property
    def active(self) -> bool:
        return self.handle is not None

    def assign(self, pid: int) -> bool:
        """Put ``pid`` (and everything it later spawns) into the job."""
        if not self.active or pid <= 0:
            return False
        kernel32 = getattr(self, "_kernel32", None) or ctypes.WinDLL(
            "kernel32", use_last_error=True)
        process = kernel32.OpenProcess(
            PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
        if not process:
            self.error = f"OpenProcess({pid}) 失败 (err={ctypes.get_last_error()})"
            return False
        try:
            return bool(kernel32.AssignProcessToJobObject(
                wintypes.HANDLE(self.handle), wintypes.HANDLE(process)))
        finally:
            kernel32.CloseHandle(process)

    def terminate(self) -> bool:
        """Kill everything in the job now. Used by an explicit stop."""
        if not self.active:
            return False
        kernel32 = getattr(self, "_kernel32", None)
        if kernel32 is None:
            return False
        return bool(kernel32.TerminateJobObject(wintypes.HANDLE(self.handle), 1))

    def close(self) -> None:
        if self.handle is not None:
            kernel32 = getattr(self, "_kernel32", None)
            if kernel32 is not None:
                kernel32.CloseHandle(wintypes.HANDLE(self.handle))
            self.handle = None

    def __del__(self) -> None:  # pragma: no cover - runs at interpreter shutdown
        try:
            self.close()
        except Exception:  # noqa: BLE001 - never raise during finalisation
            pass


# -- process-wide registry ---------------------------------------------------
#
# Why module level and not on the manager: HeadlessManager is constructed per WebUI request
# (`_headless()` builds a fresh one every time). A guard stored on that object dies with the
# request, and closing the job terminates QQ -- measured: the instance vanished the moment the
# temporary manager was collected.
#
# Keeping the guard here ties the instances' lifetime to *the process*, which is the intent:
# they live as long as QSICK does, and die with it. Nothing weaker survives an abnormal exit.

_INSTANCES: dict[str, "JobGuard"] = {}


def registry() -> dict[str, "JobGuard"]:
    """The process-wide guard table, keyed by instance id."""
    return _INSTANCES


def acquire(instance_id: str) -> "JobGuard":
    """Returns the guard for ``instance_id``, creating it if needed.

    Reuses an existing guard so a restart of the same instance does not leave the old job
    handle open. If a guard exists but is no longer active it is replaced.
    """
    existing = _INSTANCES.get(instance_id)
    if existing is not None and existing.active:
        return existing
    fresh = JobGuard()
    _INSTANCES[instance_id] = fresh
    return fresh


def release(instance_id: str, *, terminate: bool = True) -> None:
    """Terminates and closes the guard for ``instance_id``, if any."""
    guard = _INSTANCES.pop(instance_id, None)
    if guard is None:
        return
    try:
        if terminate and guard.active:
            guard.terminate()
    finally:
        guard.close()


def release_all() -> None:
    """Closes every guard. Called at shutdown; closing is what kills the trees."""
    for instance_id in list(_INSTANCES):
        try:
            release(instance_id)
        except Exception:  # noqa: BLE001 - shutdown must not raise
            pass

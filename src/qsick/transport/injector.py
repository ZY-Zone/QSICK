"""Windows injector: find QQ, load the payload DLL, wait for its pipes.

What this can do: a classic ``CreateRemoteThread(LoadLibraryW)`` injection, which
is enough for a payload that is happy to live in the target's module list and be
unloaded with ``FreeLibrary``.

What this deliberately does **not** pretend to do: manual mapping, PE relocations,
unlinking from the loader list, or bypassing anti-cheat. Those need native code
and a per-build offset table; they belong in the payload project, not in ctypes.
``manual_map_dll`` raises with that explanation instead of silently degrading.
"""

from __future__ import annotations

import ctypes
import os
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

from .. import log as logmod

log = logmod.get_logger("Transport.Injector")

IS_WINDOWS = os.name == "nt"

# Thread suspension constants live at module scope because suspend_process/resume_process are
# defined outside the `if IS_WINDOWS` block that holds the rest of the Win32 constants -- those
# names would be unreachable from a module-level function.
TH32CS_SNAPTHREAD = 0x00000004
THREAD_SUSPEND_RESUME = 0x0002

if IS_WINDOWS:  # pragma: no cover - exercised only on Windows
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    PROCESS_CREATE_THREAD = 0x0002
    PROCESS_VM_OPERATION = 0x0008
    PROCESS_VM_READ = 0x0010
    PROCESS_VM_WRITE = 0x0020
    PROCESS_QUERY_INFORMATION = 0x0400

    ACCESS = (
        PROCESS_CREATE_THREAD
        | PROCESS_VM_OPERATION
        | PROCESS_VM_READ
        | PROCESS_VM_WRITE
        | PROCESS_QUERY_INFORMATION
    )

    MEM_COMMIT = 0x1000
    MEM_RESERVE = 0x2000
    MEM_RELEASE = 0x8000
    PAGE_READWRITE = 0x04
    INFINITE = 0xFFFFFFFF
    TH32CS_SNAPPROCESS = 0x00000002
    MAX_PATH = 260

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * MAX_PATH),
        ]

    class THREADENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD),
            ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", ctypes.c_long),
            ("tpDeltaPri", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
        ]

    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.VirtualAllocEx.argtypes = [
        wintypes.HANDLE,
        wintypes.LPVOID,
        ctypes.c_size_t,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    kernel32.VirtualAllocEx.restype = wintypes.LPVOID
    kernel32.VirtualFreeEx.argtypes = [wintypes.HANDLE, wintypes.LPVOID, ctypes.c_size_t, wintypes.DWORD]
    kernel32.WriteProcessMemory.argtypes = [
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.LPCVOID,
        ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    kernel32.GetProcAddress.restype = wintypes.LPVOID
    kernel32.GetProcAddress.argtypes = [wintypes.HMODULE, wintypes.LPCSTR]
    kernel32.CreateRemoteThread.restype = wintypes.HANDLE
    kernel32.CreateRemoteThread.argtypes = [
        wintypes.HANDLE,
        wintypes.LPVOID,
        ctypes.c_size_t,
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.GetExitCodeThread.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    # Resuming a CREATE_SUSPENDED process needs the thread handle that CreateProcess returned,
    # but subprocess.Popen does not expose it. Instead every thread is suspended by walking the
    # thread list and calling SuspendThread/ResumeThread on each -- which works on any process,
    # including one that was started without CREATE_SUSPENDED.
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(THREADENTRY32)]
    kernel32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(THREADENTRY32)]
    kernel32.SuspendThread.argtypes = [wintypes.HANDLE]
    kernel32.SuspendThread.restype = wintypes.DWORD
    kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel32.ResumeThread.restype = wintypes.DWORD
    kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenThread.restype = wintypes.HANDLE


class InjectError(RuntimeError):
    pass


@dataclass(slots=True)
class ProcessInfo:
    pid: int
    name: str


def _last_error() -> str:
    code = ctypes.get_last_error()
    return f"{code}: {ctypes.FormatError(code)}"


def list_processes() -> list[ProcessInfo]:
    if not IS_WINDOWS:
        return []
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == wintypes.HANDLE(-1).value:
        raise InjectError(f"CreateToolhelp32Snapshot failed: {_last_error()}")
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    out: list[ProcessInfo] = []
    try:
        if kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            while True:
                out.append(ProcessInfo(pid=entry.th32ProcessID, name=entry.szExeFile))
                if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                    break
    finally:
        kernel32.CloseHandle(snapshot)
    return out


def find_qq_pids() -> list[int]:
    """QQ NT's main process is ``QQ.exe``; Linux/macOS names are accepted too."""
    names = {"qq.exe", "qq"}
    pids = [proc.pid for proc in list_processes() if proc.name.lower() in names]
    return sorted(pid for pid in pids if pid > 0)


def is_process_alive(pid: int) -> bool:
    if not IS_WINDOWS:
        return False
    handle = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION, False, pid)
    if not handle:
        return False
    kernel32.CloseHandle(handle)
    return True


def inject_dll(pid: int, dll_path: Path) -> int:
    """Load ``dll_path`` into ``pid``; returns the remote module handle."""
    if not IS_WINDOWS:
        raise InjectError("injection is only implemented on Windows")
    source = Path(dll_path)
    if not source.exists():
        raise InjectError(f"payload not found: {source}")
    if ctypes.sizeof(ctypes.c_void_p) * 8 != 64:
        raise InjectError("QSICK must run as 64-bit Python to inject into 64-bit QQ")

    handle = kernel32.OpenProcess(ACCESS, False, pid)
    if not handle:
        raise InjectError(
            f"OpenProcess({pid}) failed ({_last_error()}); "
            "run QSICK elevated if QQ is elevated"
        )

    path_bytes = str(source.resolve()).encode("utf-16-le") + b"\x00\x00"
    remote = kernel32.VirtualAllocEx(handle, None, len(path_bytes), MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE)
    if not remote:
        kernel32.CloseHandle(handle)
        raise InjectError(f"VirtualAllocEx failed: {_last_error()}")

    thread = None
    try:
        written = ctypes.c_size_t(0)
        if not kernel32.WriteProcessMemory(handle, remote, path_bytes, len(path_bytes), ctypes.byref(written)):
            raise InjectError(f"WriteProcessMemory failed: {_last_error()}")

        k32 = kernel32.GetModuleHandleW("kernel32.dll")
        load_library = kernel32.GetProcAddress(k32, b"LoadLibraryW")
        if not load_library:
            raise InjectError("GetProcAddress(LoadLibraryW) failed")

        thread_id = wintypes.DWORD(0)
        thread = kernel32.CreateRemoteThread(handle, None, 0, load_library, remote, 0, ctypes.byref(thread_id))
        if not thread:
            raise InjectError(
                f"CreateRemoteThread failed: {_last_error()}; the target may be protected"
            )
        if kernel32.WaitForSingleObject(thread, 10_000) != 0:
            raise InjectError("injection thread did not finish within 10s")
        exit_code = wintypes.DWORD(0)
        kernel32.GetExitCodeThread(thread, ctypes.byref(exit_code))
        if exit_code.value == 0:
            raise InjectError(
                "LoadLibraryW returned NULL in the target; "
                "the payload's dependencies are probably missing (VC++ runtime?)"
            )
        log.info("payload injected", pid=pid, module_handle=hex(exit_code.value))
        return int(exit_code.value)
    finally:
        if thread:
            kernel32.CloseHandle(thread)
        kernel32.VirtualFreeEx(handle, remote, 0, MEM_RELEASE)
        kernel32.CloseHandle(handle)


def suspend_process(pid: int) -> list[int]:
    """Suspend every thread of ``pid``; returns the thread ids that were suspended.

    Why this instead of CREATE_SUSPENDED: launching a suspended QQ.exe makes it exit with
    0x80000003 about 12 s later (a startup/sandbox check fails), whether or not a payload is
    injected -- so the process must be allowed to start normally and then be frozen.

    Freezing after start is what closes the timing gap. Measured: QQ builds its session shell at
    about +214 ms into startup, before our napi hooks ever run, which is why every session lookup
    from the injected script returned undefined. Getting the DLL in first gives it a chance to
    run before that window.

    Threads are enumerated rather than taken from CreateProcess because subprocess.Popen does not
    expose the primary thread handle.
    """
    if not IS_WINDOWS:
        raise InjectError("process suspension is only implemented on Windows")

    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
    if snapshot == wintypes.HANDLE(-1).value or not snapshot:
        raise InjectError(f"CreateToolhelp32Snapshot(threads) failed: {_last_error()}")

    suspended: list[int] = []
    try:
        entry = THREADENTRY32()
        entry.dwSize = ctypes.sizeof(THREADENTRY32)
        if not kernel32.Thread32First(snapshot, ctypes.byref(entry)):
            return suspended
        while True:
            if entry.th32OwnerProcessID == pid:
                handle = kernel32.OpenThread(THREAD_SUSPEND_RESUME, False, entry.th32ThreadID)
                if handle:
                    if kernel32.SuspendThread(handle) != 0xFFFFFFFF:
                        suspended.append(int(entry.th32ThreadID))
                    kernel32.CloseHandle(handle)
            if not kernel32.Thread32Next(snapshot, ctypes.byref(entry)):
                break
    finally:
        kernel32.CloseHandle(snapshot)
    return suspended


def resume_process(thread_ids: list[int]) -> None:
    """Resume threads previously suspended by :func:`suspend_process`."""
    if not IS_WINDOWS:
        return
    for thread_id in thread_ids:
        handle = kernel32.OpenThread(THREAD_SUSPEND_RESUME, False, thread_id)
        if not handle:
            continue
        try:
            kernel32.ResumeThread(handle)
        finally:
            kernel32.CloseHandle(handle)


def manual_map_dll(pid: int, dll_path: Path) -> None:
    raise NotImplementedError(
        "manual mapping is not implemented in the Python injector. "
        "Rationale: it needs PE section mapping, relocation, TLS callbacks and "
        "exception-table handling inside the target, which cannot be done safely "
        "from ctypes. Build it into the payload or a native loader."
    )


def pipe_paths(pid: int, prefix: str = r"\\.\pipe\qsick") -> tuple[str, str]:
    base = prefix.rstrip("\\/")
    return f"{base}.{pid}.ctl", f"{base}.{pid}.evt"


def wait_for_pipes(pid: int, prefix: str = r"\\.\pipe\qsick", timeout: float = 8.0) -> bool:
    ctl, _ = pipe_paths(pid, prefix)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if os.path.exists(ctl):
            return True
        time.sleep(0.1)
    return False


def ensure_hooked(pid: int, dll_path: Path, prefix: str = r"\\.\pipe\qsick") -> int:
    """Attach if the payload is already resident, otherwise inject and wait."""
    if wait_for_pipes(pid, prefix, timeout=0.3):
        log.info("payload already resident; attaching", pid=pid)
        return pid
    log.info("injecting payload", pid=pid, dll=str(dll_path))
    inject_dll(pid, dll_path)
    if not wait_for_pipes(pid, prefix, timeout=8.0):
        raise InjectError("payload loaded but its pipes never appeared")
    return pid

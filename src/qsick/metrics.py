"""CPU and memory sampling for the dashboard, straight from the Windows APIs.

## Why not psutil

The project already talks to kernel32 through ctypes -- `job_guard` builds its own structures and `headless` opens
process handles -- and four numbers do not justify a native dependency in an otherwise pure-Python package.

## Nothing here raises

A dashboard that blanks out because one syscall failed is worse than one showing a gap, so every entry point
swallows its errors and returns a neutral value. On a non-Windows box (CI, a dev laptop) the whole module degrades
to `available() == False` and the UI simply omits the charts.

## What the numbers mean

`cpu` is **system-wide**: idle time as a fraction of total across all cores. That is what "CPU 占用" reads as on a
dashboard, and it is the number that actually tells you whether the machine is busy -- QSICK's own share of it is
almost always too small to draw.

## Which memory number

"内存" means different things, and picking the wrong one made the pie disagree with the task manager by a factor of
two and a half. `GetProcessMemoryInfo` offers only the **total working set**, which counts every shared page once per
process -- QQ runs eight Chromium processes sharing the same DLLs, so their sum comes out far above what the machine
actually spends. The figure the task manager shows is the **private working set**, and
`NtQuerySystemInformation(SystemProcessInformation)` carries it as `WorkingSetPrivateSize`, for every process in one
call. That is what this module reports.

Memory is split the way the pie chart wants it: QSICK's own private bytes, one slice per QQ main process, everything
else that is resident, and free.
"""
from __future__ import annotations

import ctypes
import os
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from typing import Any

#: The process name QQ NT runs under. Its renderer/GPU children share the name, so the tree has to be walked to
#: tell a main process from a helper.
QQ_PROCESS_NAMES = ("QQ.exe", "QQ")

MEM_COMMIT = 0x1000
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_COMMAND_LINE_INFORMATION = 60
SYSTEM_PROCESS_INFORMATION_CLASS = 5
TH32CS_SNAPPROCESS = 0x00000002
MAX_PATH = 260
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


def available() -> bool:
    """Whether the platform calls this module needs are there."""
    return sys.platform == "win32"


# ---------------------------------------------------------------- structures


class FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]


class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", wintypes.DWORD),
        ("dwMemoryLoad", wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]




class UNICODE_STRING(ctypes.Structure):
    _fields_ = [
        ("Length", wintypes.USHORT),
        ("MaximumLength", wintypes.USHORT),
        ("Buffer", ctypes.c_void_p),
    ]


class SYSTEM_PROCESS_INFORMATION(ctypes.Structure):
    """The head of `SYSTEM_PROCESS_INFORMATION` — everything up to the two numbers this module reads.

    Only the leading fields are declared, and the buffer is walked by `NextEntryOffset` rather than by
    ``sizeof``: the real structure is longer and its tail has changed between Windows versions, so declaring all of
    it would be a version-dependent guess. Everything above `WorkingSetSize` has been stable since Windows 7.
    """
    _fields_ = [
        ("NextEntryOffset", wintypes.ULONG),
        ("NumberOfThreads", wintypes.ULONG),
        ("WorkingSetPrivateSize", ctypes.c_longlong),
        ("HardFaultCount", wintypes.ULONG),
        ("NumberOfThreadsHighWatermark", wintypes.ULONG),
        ("CycleTime", ctypes.c_ulonglong),
        ("CreateTime", ctypes.c_longlong),
        ("UserTime", ctypes.c_longlong),
        ("KernelTime", ctypes.c_longlong),
        ("ImageName", UNICODE_STRING),
        ("BasePriority", ctypes.c_long),
        ("UniqueProcessId", ctypes.c_void_p),
        ("InheritedFromUniqueProcessId", ctypes.c_void_p),
        ("HandleCount", wintypes.ULONG),
        ("SessionId", wintypes.ULONG),
        ("UniqueProcessKey", ctypes.c_void_p),
        ("PeakVirtualSize", ctypes.c_size_t),
        ("VirtualSize", ctypes.c_size_t),
        ("PageFaultCount", wintypes.ULONG),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
    ]


class PROCESSENTRY32(ctypes.Structure):
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
        ("szExeFile", ctypes.c_char * MAX_PATH),
    ]


# ---------------------------------------------------------------- primitives


def _filetime_to_int(value: FILETIME) -> int:
    return (value.dwHighDateTime << 32) | value.dwLowDateTime


def system_times() -> tuple[int, int, int] | None:
    """``(idle, kernel, user)`` in 100-ns units, or None when the call is unavailable.

    `kernel` includes idle time -- that is how `GetSystemTimes` defines it, and forgetting it makes the CPU figure
    come out far too high.
    """
    if not available():
        return None
    try:
        idle, kernel, user = FILETIME(), FILETIME(), FILETIME()
        if not ctypes.windll.kernel32.GetSystemTimes(
            ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
        ):
            return None
        return _filetime_to_int(idle), _filetime_to_int(kernel), _filetime_to_int(user)
    except Exception:  # noqa: BLE001
        return None


def cpu_percent(previous: tuple[int, int, int] | None) -> tuple[float, tuple[int, int, int] | None]:
    """System CPU usage since `previous`, as a percentage of all cores.

    Returns ``(percent, current)`` so the caller keeps the baseline for the next call. A first sample has no
    baseline and reports 0 rather than inventing a number.
    """
    current = system_times()
    if current is None:
        return 0.0, None
    if previous is None:
        return 0.0, current
    idle_delta = current[0] - previous[0]
    kernel_delta = current[1] - previous[1]
    user_delta = current[2] - previous[2]
    total = kernel_delta + user_delta
    if total <= 0:
        return 0.0, current
    busy = total - idle_delta
    return max(0.0, min(100.0, busy * 100.0 / total)), current


def memory_status() -> tuple[int, int]:
    """``(total, available)`` physical bytes, or ``(0, 0)``."""
    if not available():
        return 0, 0
    try:
        status = MEMORYSTATUSEX()
        status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return 0, 0
        return int(status.ullTotalPhys), int(status.ullAvailPhys)
    except Exception:  # noqa: BLE001
        return 0, 0


def _system_processes() -> list[dict[str, Any]]:
    """Every process with its private and total working set, from one `NtQuerySystemInformation` call.

    One syscall for the whole table, rather than opening a handle per process: this runs every few seconds for the
    dashboard, and the per-process route cost eight opens plus eight closes each time.
    """
    if not available():
        return []
    query = ctypes.windll.ntdll.NtQuerySystemInformation
    size = 512 * 1024
    buffer = None
    for _ in range(8):
        buffer = ctypes.create_string_buffer(size)
        needed = wintypes.ULONG(0)
        status = query(SYSTEM_PROCESS_INFORMATION_CLASS, buffer, size, ctypes.byref(needed))
        if status == 0:
            break
        # NTSTATUS comes back signed through ctypes, so STATUS_INFO_LENGTH_MISMATCH reads as -1073741820.
        # Comparing against 0xC0000004 directly therefore never matched, and the first undersized buffer ended the
        # function with an empty list -- the pie then reported 0 MB for everything.
        if (status & 0xFFFFFFFF) != 0xC0000004:
            return []
        size = max(size * 2, int(needed.value))
    else:
        return []

    found: list[dict[str, Any]] = []
    offset = 0
    guard = 0
    while guard < 20000:
        guard += 1
        entry = SYSTEM_PROCESS_INFORMATION.from_buffer(buffer, offset)
        name = ""
        if entry.ImageName.Buffer and entry.ImageName.Length:
            try:
                name = ctypes.wstring_at(entry.ImageName.Buffer, entry.ImageName.Length // 2)
            except (ValueError, OSError):
                name = ""
        found.append({
            "pid": int(entry.UniqueProcessId or 0),
            "ppid": int(entry.InheritedFromUniqueProcessId or 0),
            "name": name,
            "private": int(entry.WorkingSetPrivateSize),
            "working_set": int(entry.WorkingSetSize),
        })
        if not entry.NextEntryOffset:
            break
        offset += entry.NextEntryOffset
    return found


def process_memory(pid: int) -> dict[str, int]:
    """One process's memory under both metrics, or zeros when it is gone.

    ``private`` is what the task manager's 内存 column shows; ``working_set`` is the total, shared pages included.
    """
    for entry in _system_processes():
        if entry["pid"] == int(pid):
            return {"private": entry["private"], "working_set": entry["working_set"]}
    return {"private": 0, "working_set": 0}


def enumerate_processes() -> list[dict[str, Any]]:
    """``[{pid, ppid, name}]`` for every process the caller is allowed to see."""
    return [{"pid": row["pid"], "ppid": row["ppid"], "name": row["name"]}
            for row in _system_processes()]


def process_command_line(pid: int) -> str:
    """The command line of a running process, or ``""``.

    `NtQueryInformationProcess` with ``ProcessCommandLineInformation`` rather than WMI: this runs on every sample,
    and a WMI query costs about a second. The returned buffer is a UNICODE_STRING whose text sits two pointers in.
    """
    if not available() or not pid:
        return ""
    handle = None
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return ""
        query = ctypes.windll.ntdll.NtQueryInformationProcess
        size = wintypes.ULONG(0)
        query(handle, PROCESS_COMMAND_LINE_INFORMATION, None, 0, ctypes.byref(size))
        if not size.value:
            return ""
        buffer = ctypes.create_string_buffer(size.value)
        status = query(handle, PROCESS_COMMAND_LINE_INFORMATION, buffer, size.value, ctypes.byref(size))
        if status != 0:
            return ""
        length = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ushort))[0]
        # UNICODE_STRING is Length(2) MaximumLength(2) padding(4) Buffer(8); the text follows the structure.
        offset = ctypes.sizeof(ctypes.c_void_p) * 2
        return ctypes.wstring_at(ctypes.addressof(buffer) + offset, length // 2)
    except Exception:  # noqa: BLE001
        return ""
    finally:
        if handle:
            try:
                ctypes.windll.kernel32.CloseHandle(handle)
            except Exception:  # noqa: BLE001
                pass


def session_of(command_line: str) -> str:
    """The session id a QQ command line was launched for, or ``""``.

    QSICK launches every pipe session with ``--user-data-dir=...\\userdata\\<tmp_id>``, so the last path segment is
    the session id. That is how a QQ process is tied back to the account logged into it.
    """
    marker = "--user-data-dir="
    start = command_line.find(marker)
    if start < 0:
        return ""
    value = command_line[start + len(marker):].strip().strip('"')
    tail = value.replace("/", "\\").rstrip("\\").rsplit("\\", 1)
    name = tail[-1] if tail else ""
    return name if name and " " not in name else ""


def qq_process_tree(entries: list[dict[str, Any]]) -> dict[int, list[int]]:
    """``{main_pid: [child pids...]}`` for the QQ processes in `entries`."""
    qq_pids = {item["pid"] for item in entries if item["name"] in QQ_PROCESS_NAMES}
    children: dict[int, list[int]] = {}
    for item in entries:
        if item["pid"] not in qq_pids or not item["ppid"]:
            continue
        children.setdefault(item["ppid"], []).append(item["pid"])
    return children


def qq_main_processes(entries: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """The top-level QQ processes, each with its session id and the pids of its helpers.

    QQ NT spawns its renderer, GPU, utility and crashpad helpers under the same image name, so "every process called
    QQ.exe" would draw eight slices for one account. A main process is the one whose parent is *not* itself a QQ
    process -- helpers are always spawned by QQ, the entry point is not.

    The tree is carried along because a slice labelled "QQ (426MB)" while QQ actually holds 1.1GB across eight
    processes is a lie of omission: the helpers belong to their main process, not to "other".
    """
    entries = entries if entries is not None else enumerate_processes()
    children = qq_process_tree(entries)
    qq_pids = {item["pid"] for item in entries if item["name"] in QQ_PROCESS_NAMES}
    mains = [
        item for item in entries
        if item["name"] in QQ_PROCESS_NAMES and item["ppid"] not in qq_pids
    ]
    out: list[dict[str, Any]] = []
    for item in mains:
        helpers: list[int] = []
        pending = list(children.get(item["pid"], []))
        while pending:
            child = pending.pop()
            helpers.append(child)
            pending.extend(children.get(child, []))
        out.append({**item, "session": session_of(process_command_line(item["pid"])),
                    "helpers": helpers})
    return out


# ---------------------------------------------------------------- one sample


@dataclass(slots=True)
class Sample:
    ts: float
    cpu: float
    total_memory: int
    available_memory: int
    self_bytes: int
    qq: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": round(self.ts, 3),
            "cpu": round(self.cpu, 2),
            "memory": self.memory_breakdown(),
        }

    def memory_breakdown(self) -> dict[str, Any]:
        """The pie chart's slices, in bytes.

        `other` is everything resident that is neither us nor QQ -- derived by subtraction rather than by summing
        every process, because the sum of working sets double-counts shared pages and can exceed physical RAM.
        """
        qq_total = sum(item["bytes"] for item in self.qq)
        used = self.total_memory - self.available_memory
        other = max(0, used - self.self_bytes - qq_total)
        return {
            "total": self.total_memory,
            "available": self.available_memory,
            "self": self.self_bytes,
            "qq": [dict(item) for item in self.qq],
            "qq_total": qq_total,
            "other": other,
        }


def sample(previous_times: tuple[int, int, int] | None = None,
           uins: dict[int, str] | None = None) -> tuple[Sample, tuple[int, int, int] | None]:
    """One dashboard sample plus the CPU baseline to pass back in next time.

    `uins` maps a pid (or a session id) to the account logged in as it, for labelling the pie slices.
    """
    percent, times = cpu_percent(previous_times)
    total, avail = memory_status()
    entries = _system_processes()
    by_pid = {entry["pid"]: entry for entry in entries}
    mains = qq_main_processes(entries)
    qq: list[dict[str, Any]] = []
    for item in mains:
        # The whole tree, not just the entry point: a renderer's hundreds of megabytes are QQ's, and folding them
        # into "other" would make the pie answer a question nobody asked.
        pids = [item["pid"]] + list(item.get("helpers") or [])
        private = sum(by_pid.get(pid, {}).get("private", 0) for pid in pids)
        qq.append({
            "pid": item["pid"],
            "name": item["name"],
            "session": item.get("session", ""),
            "processes": len(pids),
            "bytes": private,
            "uin": (uins or {}).get(item["pid"], "") or (uins or {}).get(item.get("session", ""), ""),
        })
    qq.sort(key=lambda entry: entry["bytes"], reverse=True)
    me = by_pid.get(os.getpid(), {})
    return Sample(
        ts=time.time(),
        cpu=percent,
        total_memory=total,
        available_memory=avail,
        # Private bytes, so "QSICK 侧" means what the task manager would call QSICK's own memory.
        self_bytes=int(me.get("private", 0)),
        qq=qq,
    ), times


# ---------------------------------------------------------------- history


@dataclass(slots=True)
class History:
    """A ring of samples, trimmed by age rather than count.

    Trimming by age is what the chart actually promises ("last 30 minutes"); trimming by count would silently show
    less than that whenever the interval moved.
    """

    window_seconds: float = 1800.0
    capacity: int = 720
    samples: list[Sample] = field(default_factory=list)
    _times: tuple[int, int, int] | None = None

    def record(self, uins: dict[int, str] | None = None) -> Sample:
        taken, self._times = sample(self._times, uins)
        self.samples.append(taken)
        self._trim()
        return taken

    def _trim(self) -> None:
        cutoff = time.time() - self.window_seconds
        # Keep one sample older than the cutoff: a line that starts exactly at "now - 30 min" needs a point there
        # to have a segment to draw.
        keep_from = 0
        for index, item in enumerate(self.samples):
            if item.ts >= cutoff:
                keep_from = max(0, index - 1)
                break
        else:
            keep_from = max(0, len(self.samples) - 1)
        if keep_from:
            del self.samples[:keep_from]
        if len(self.samples) > self.capacity:
            del self.samples[: len(self.samples) - self.capacity]

    def cpu_points(self) -> list[list[float]]:
        return [[round(item.ts, 1), round(item.cpu, 2)] for item in self.samples]

    def latest(self) -> Sample | None:
        return self.samples[-1] if self.samples else None


def fit_curve(points: list[list[float]], *, degree: int = 2, resolution: int = 40) -> list[list[float]]:
    """Least-squares polynomial fit of the series, sampled evenly across the window.

    A moving average would follow the noise; a fit is what "变化拟合曲线" asks for -- the trend underneath. Degree 2
    keeps it from bending into the gaps, and the result is clamped to the value range so a fit cannot draw 0-100
    percentages outside 0-100.
    """
    if len(points) < degree + 2:
        return []
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    # Shift x to seconds-from-start for conditioning: raw epoch values at degree 2 lose all precision in float64.
    base = xs[0]
    scaled = [x - base for x in xs]
    span = max(scaled) or 1.0
    norm = [value / span for value in scaled]

    # Normal equations for a tiny polynomial; no numpy in this project's dependency set.
    size = degree + 1
    matrix = [[sum(value ** (row + col) for value in norm) for col in range(size)] for row in range(size)]
    vector = [sum(y * (value ** row) for value, y in zip(norm, ys)) for row in range(size)]
    try:
        coefficients = _solve(matrix, vector)
    except ZeroDivisionError:
        return []
    if coefficients is None:
        return []

    low, high = min(ys), max(ys)
    out: list[list[float]] = []
    for step in range(resolution + 1):
        portion = step / resolution
        value = sum(coefficients[power] * (portion ** power) for power in range(size))
        out.append([round(xs[0] + portion * span, 1), round(max(low, min(high, value)), 2)])
    return out


def _solve(matrix: list[list[float]], vector: list[float]) -> list[float] | None:
    """Gaussian elimination with partial pivoting. Returns None when the system is singular."""
    size = len(vector)
    augmented = [row[:] + [vector[index]] for index, row in enumerate(matrix)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) < 1e-12:
            return None
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        lead = augmented[column][column]
        for row in range(size):
            if row == column:
                continue
            factor = augmented[row][column] / lead
            if factor:
                for index in range(column, size + 1):
                    augmented[row][index] -= factor * augmented[column][index]
    return [augmented[index][size] / augmented[index][index] for index in range(size)]

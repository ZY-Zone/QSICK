"""在 QQ.exe 冻结状态下注入自有 DLL。

## 为什么要"冻结—注入—恢复"

正常的 ``CreateProcess`` 里，等我们看到窗口时 Electron 主进程早就启动了，也已经
和原生后端完成了握手。要**替换**那个后端，DLL 必须在外面的程序跑起来之前就在场。

``CREATE_SUSPENDED`` 创建出来的进程停在加载器断点处：镜像已映射、``ntdll`` 已初始化，
但 ``QQ.exe`` 自己的第一条指令还没执行。此时注入，DLL 完成挂接，然后才 ``ResumeThread``。

## 关键尺寸

``LoadLibraryW`` 通过线程退出码回传模块句柄，而退出码是 32 位——它**会被截断**。
实测 QQ.exe 里 DLL 落在 ``0x7ffe2dd00000``，回传的却是 ``0x2dd00000``。所以基址一律
用 ``EnumProcessModulesEx`` 取，不要相信那个返回值。

## 不改客户端任何文件

注入完全在内存里进行：分配一块内存放 DLL 路径、起一个远程线程调 ``LoadLibraryW``。
``QQ.exe``、``wrapper.node``、``package.json`` 一个字节都不动。
"""

from __future__ import annotations

import ctypes
import os
import struct
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .. import log as logmod

log = logmod.get_logger("Transport.Inject")

IS_WINDOWS = os.name == "nt"

CREATE_SUSPENDED = 0x00000004
CREATE_NEW_PROCESS_GROUP = 0x00000200
MEM_COMMIT = 0x1000
MEM_RESERVE = 0x2000
MEM_RELEASE = 0x8000
PAGE_READWRITE = 0x04
PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_TERMINATE = 0x0001
SYNCHRONIZE = 0x00100000
#: 注入 + 之后读标志位所需的全部权限。
PROCESS_ALL_NEEDED = 0x1F0FFF
INFINITE = 0xFFFFFFFF
LIST_MODULES_ALL = 0x03

#: DLL 在 DllMain 里写下的握手值；读到它才算真的挂上了。
ATTACH_MAGIC = 0xC0DE0001

if IS_WINDOWS:  # pragma: no cover - only meaningful on Windows
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _psapi = ctypes.WinDLL("psapi", use_last_error=True)

    class STARTUPINFOW(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("lpReserved", wintypes.LPWSTR),
            ("lpDesktop", wintypes.LPWSTR),
            ("lpTitle", wintypes.LPWSTR),
            ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
            ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
            ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
            ("dwFillAttribute", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("wShowWindow", wintypes.WORD),
            ("cbReserved2", wintypes.WORD),
            ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
            ("hStdInput", wintypes.HANDLE),
            ("hStdOutput", wintypes.HANDLE),
            ("hStdError", wintypes.HANDLE),
        ]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("hProcess", wintypes.HANDLE),
            ("hThread", wintypes.HANDLE),
            ("dwProcessId", wintypes.DWORD),
            ("dwThreadId", wintypes.DWORD),
        ]

    class MODULEINFO(ctypes.Structure):
        _fields_ = [
            ("lpBaseOfDll", ctypes.c_void_p),
            ("SizeOfImage", wintypes.DWORD),
            ("EntryPoint", ctypes.c_void_p),
        ]

    _kernel32.CreateProcessW.argtypes = [
        wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
        wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
        ctypes.POINTER(STARTUPINFOW), ctypes.POINTER(PROCESS_INFORMATION),
    ]
    _kernel32.CreateProcessW.restype = wintypes.BOOL
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.VirtualAllocEx.restype = ctypes.c_void_p
    _kernel32.VirtualAllocEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t,
                                         wintypes.DWORD, wintypes.DWORD]
    _kernel32.VirtualFreeEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t,
                                        wintypes.DWORD]
    _kernel32.WriteProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                                             ctypes.c_void_p, ctypes.c_size_t,
                                             ctypes.POINTER(ctypes.c_size_t)]
    _kernel32.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                                            ctypes.c_void_p, ctypes.c_size_t,
                                            ctypes.POINTER(ctypes.c_size_t)]
    _kernel32.CreateRemoteThread.restype = wintypes.HANDLE
    _kernel32.CreateRemoteThread.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                                             ctypes.c_size_t, ctypes.c_void_p,
                                             ctypes.c_void_p, wintypes.DWORD,
                                             ctypes.c_void_p]
    _kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _kernel32.GetExitCodeThread.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
    _kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.GetModuleHandleW.restype = ctypes.c_void_p
    _kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    _kernel32.GetProcAddress.restype = ctypes.c_void_p
    _kernel32.GetProcAddress.argtypes = [ctypes.c_void_p, ctypes.c_char_p]

    _psapi.EnumProcessModulesEx.argtypes = [wintypes.HANDLE,
                                            ctypes.POINTER(ctypes.c_void_p),
                                            wintypes.DWORD,
                                            ctypes.POINTER(wintypes.DWORD),
                                            wintypes.DWORD]
    _psapi.GetModuleBaseNameW.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                                          wintypes.LPWSTR, wintypes.DWORD]
    _psapi.GetModuleInformation.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                                            ctypes.POINTER(MODULEINFO), wintypes.DWORD]


class InjectError(RuntimeError):
    """注入失败。消息面向用户，不带堆栈。"""


@dataclass
class Injection:
    """一次成功的注入。持有进程/线程句柄，调用方负责 :meth:`close`。"""

    pid: int
    hprocess: int
    hthread: int
    module_base: int | None = None
    resumed: bool = False
    # Set when injection could not confirm the module while the target was suspended. The caller
    # re-checks after resuming; see the note at the module enumeration step.
    pending_verification: bool = False

    def close(self) -> None:
        """放掉句柄（不终止进程——那是调用方的事）。"""
        if not IS_WINDOWS:
            return
        for handle in (self.hthread, self.hprocess):
            if handle:
                _kernel32.CloseHandle(wintypes.HANDLE(handle))
        self.hthread = 0
        self.hprocess = 0


def open_process(pid: int) -> int | None:
    """拿到能注入的进程句柄；打不开返回 None。

    刚 ``CreateProcess`` 完的一小段时间里 ``OpenProcess`` 可能失败（进程对象还没
    完全建立），调用方负责重试。
    """
    if not IS_WINDOWS:
        return None
    handle = _kernel32.OpenProcess(PROCESS_ALL_NEEDED, False, pid)
    return int(handle) if handle else None


def export_rva(dll: Path, name: str) -> int | None:
    """手工解析 PE 导出表取 RVA。

    不用外部依赖，也不加载 DLL——只需要读文件。注意**按序号表跳一次**：名称表是
    按字母序排的，函数表按序号排，拿名称下标直接索引函数表是常见错误。

    不是合法 PE（文件太短、magic 不对、结构越界）一律返回 ``None``：这个函数只
    用来读我们自己 DLL 里的握手值，读不到应当让调用方报告"注入没生效"，
    而不是抛一个 ``struct.error`` 把真正的原因盖住。
    """
    try:
        data = Path(dll).read_bytes()
        pe = struct.unpack_from("<I", data, 0x3C)[0]
        if data[pe:pe + 4] != b"PE\0\0":
            return None
        nsec = struct.unpack_from("<H", data, pe + 6)[0]
        opt_size = struct.unpack_from("<H", data, pe + 20)[0]
        opt = pe + 24
        magic = struct.unpack_from("<H", data, opt)[0]
        dd = opt + (112 if magic == 0x20B else 96)
        exp_rva = struct.unpack_from("<I", data, dd)[0]
        if not exp_rva:
            return None
    except (OSError, struct.error, IndexError, ValueError):
        return None

    sections = []
    sec = opt + opt_size
    for i in range(nsec):
        off = sec + i * 40
        vsz, va, rsz, ra = struct.unpack_from("<IIII", data, off + 8)
        sections.append((va, max(vsz, rsz), ra))

    def to_off(rva: int) -> int:
        for va, vsz, ra in sections:
            if va <= rva < va + vsz:
                return ra + (rva - va)
        raise ValueError(f"rva {rva:#x} not mapped")

    try:
        eoff = to_off(exp_rva)
        n_names = struct.unpack_from("<I", data, eoff + 24)[0]
        addr_names = struct.unpack_from("<I", data, eoff + 32)[0]
        addr_ords = struct.unpack_from("<I", data, eoff + 36)[0]
        addr_funcs = struct.unpack_from("<I", data, eoff + 28)[0]
        names_off = to_off(addr_names)
        ords_off = to_off(addr_ords)
        funcs_off = to_off(addr_funcs)

        for i in range(n_names):
            nrva = struct.unpack_from("<I", data, names_off + i * 4)[0]
            noff = to_off(nrva)
            end = data.index(b"\0", noff)
            if data[noff:end].decode("latin-1") != name:
                continue
            ordinal = struct.unpack_from("<H", data, ords_off + i * 2)[0]
            return struct.unpack_from("<I", data, funcs_off + ordinal * 4)[0]
    except (struct.error, IndexError, ValueError):
        return None
    return None


def find_module(hprocess: int, wanted: str) -> tuple[int, int] | None:
    """在目标进程里按名字找模块，返回 ``(基址, 映像大小)``。

    用它而不是 ``LoadLibraryW`` 的返回值：后者经线程退出码回传，只有 32 位，
    高位会被截掉（实测 ``0x7ffe2dd00000`` 回来变成 ``0x2dd00000``）。
    """
    if not IS_WINDOWS:
        return None
    handles = (ctypes.c_void_p * 1024)()
    needed = wintypes.DWORD(0)
    if not _psapi.EnumProcessModulesEx(wintypes.HANDLE(hprocess), handles,
                                       ctypes.sizeof(handles),
                                       ctypes.byref(needed), LIST_MODULES_ALL):
        return None
    buf = ctypes.create_unicode_buffer(512)
    count = needed.value // ctypes.sizeof(ctypes.c_void_p)
    for i in range(min(count, 1024)):
        if not _psapi.GetModuleBaseNameW(wintypes.HANDLE(hprocess), handles[i], buf, 512):
            continue
        if buf.value.lower() != wanted.lower():
            continue
        info = MODULEINFO()
        if _psapi.GetModuleInformation(wintypes.HANDLE(hprocess), handles[i],
                                       ctypes.byref(info), ctypes.sizeof(info)):
            return int(info.lpBaseOfDll or 0), int(info.SizeOfImage)
        return int(handles[i] or 0), 0
    return None


def read_u32(hprocess: int, address: int) -> int | None:
    """从目标进程读一个 32 位值；读不到返回 None。"""
    if not IS_WINDOWS:
        return None
    buf = ctypes.c_uint32(0)
    got = ctypes.c_size_t(0)
    ok = _kernel32.ReadProcessMemory(wintypes.HANDLE(hprocess),
                                     ctypes.c_void_p(address), ctypes.byref(buf), 4,
                                     ctypes.byref(got))
    if ok and got.value == 4:
        return int(buf.value)
    return None


def launch_suspended(exe: Path, arguments: str = "", workdir: Path | None = None,
                     extra_env: dict[str, str] | None = None) -> Injection:
    """以挂起状态启动 ``exe``，返回可继续操作的对象。

    调用方必须先 :func:`inject` 再 :meth:`Injection.resume`，否则这个进程会一直停着。

    .. note:: **不传自定义环境块。**

       实测（见 ``tests/test_pipe_inject.py`` 的记录）：在 CPython 3.13 上只要给
       ``lpEnvironment`` 递一个非 NULL 的块，``CreateProcessW`` 就返回
       ``ERROR_INVALID_PARAMETER (87)``，无论用 ``LPWSTR``、``POINTER(c_wchar)``、
       ``c_void_p`` 还是原始字节缓冲区，也无论有没有 ``CREATE_SUSPENDED``。
       传 ``None`` 一切正常。

       所以这里不再尝试传环境，改用**更可靠**的通道：把注入参数写进 DLL 旁边的
       JSON，DLL 在 ``DllMain`` 里自己读。这样既不依赖 ctypes 的参数编排，也不
       受子进程环境继承的影响——子进程本来就会继承我们的环境，只是我们自己不去
       覆写它。
    """
    if not IS_WINDOWS:
        raise InjectError("注入只在 Windows 上可用")

    si = STARTUPINFOW()
    si.cb = ctypes.sizeof(si)
    pi = PROCESS_INFORMATION()

    command = f'"{exe}"'
    if arguments:
        command += " " + arguments
    cmd_buf = ctypes.create_unicode_buffer(command)

    creation = CREATE_SUSPENDED | CREATE_NEW_PROCESS_GROUP
    ok = _kernel32.CreateProcessW(
        str(exe), cmd_buf, None, None, False, creation,
        None,
        str(workdir or exe.parent), ctypes.byref(si), ctypes.byref(pi),
    )
    if not ok:
        raise InjectError(f"挂起启动失败：CreateProcessW 错误 {ctypes.get_last_error()}")

    log.info("已挂起启动", pid=pi.dwProcessId, exe=str(exe))
    return Injection(pid=int(pi.dwProcessId), hprocess=int(pi.hProcess or 0),
                     hthread=int(pi.hThread or 0))


def inject(injection: Injection, dll: Path, timeout_ms: int = 15000) -> int:
    """把 ``dll`` 注入到挂起的进程里，返回它的真实基址。

    注入后立刻读 DLL 写的握手值：读到 magic 才算成功。只看 ``LoadLibraryW`` 有没有
    返回是不够的——路径写错时它也会返回 0，而句柄截断又会让人误判成别的地址。
    """
    if not IS_WINDOWS:
        raise InjectError("注入只在 Windows 上可用")
    dll = Path(dll)
    if not dll.is_file():
        raise InjectError(f"DLL 不存在：{dll}")

    hprocess = wintypes.HANDLE(injection.hprocess)

    # 1. 在目标里分配内存并写入 DLL 路径（UTF-16，带结尾 NUL）
    payload = (str(dll) + "\0").encode("utf-16-le")
    remote = _kernel32.VirtualAllocEx(hprocess, None, len(payload),
                                      MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE)
    if not remote:
        raise InjectError(f"VirtualAllocEx 失败：{ctypes.get_last_error()}")
    try:
        written = ctypes.c_size_t(0)
        if not _kernel32.WriteProcessMemory(hprocess, ctypes.c_void_p(remote),
                                            payload, len(payload),
                                            ctypes.byref(written)):
            raise InjectError(f"WriteProcessMemory 失败：{ctypes.get_last_error()}")

        # 2. 远程线程调 LoadLibraryW
        k32 = _kernel32.GetModuleHandleW("kernel32.dll")
        load_library = _kernel32.GetProcAddress(k32, b"LoadLibraryW")
        if not load_library:
            raise InjectError("拿不到 LoadLibraryW 地址")

        thread = _kernel32.CreateRemoteThread(hprocess, None, 0,
                                              ctypes.c_void_p(load_library),
                                              ctypes.c_void_p(remote), 0, None)
        if not thread:
            raise InjectError(f"CreateRemoteThread 失败：{ctypes.get_last_error()}")
        try:
            _kernel32.WaitForSingleObject(wintypes.HANDLE(thread), timeout_ms)
            code = wintypes.DWORD(0)
            _kernel32.GetExitCodeThread(wintypes.HANDLE(thread), ctypes.byref(code))
            reported = int(code.value)
        finally:
            _kernel32.CloseHandle(wintypes.HANDLE(thread))

        if not reported:
            raise InjectError(
                "LoadLibraryW 返回 0：目标进程里加载 DLL 失败"
                "（位数不匹配，或依赖的 DLL 缺失）")

        # 3. 用模块枚举拿真实基址，并确认 DllMain 跑过
        #
        # This runs while the caller may still have the process frozen -- suspend, inject, resume
        # is the intended order -- and EnumProcessModulesEx is unreliable in that state. Measured:
        # LoadLibraryW returned success and the remote thread ran, yet the enumeration reported
        # the module missing for 17 s straight, and the instance came up with no DLL at all. The
        # cause is that the loader finishes registering a module on the target's own thread, and
        # a suspended process gives it no chance to do so.
        #
        # So this no longer treats a transient miss as failure. It reports what it can and leaves
        # the module base at 0 with a flag; the caller re-checks after resuming. Failing here
        # would turn a timing artefact into a dead instance.
        found = None
        deadline = time.time() + 2.0
        while time.time() < deadline:
            found = find_module(injection.hprocess, dll.name)
            if found:
                break
            time.sleep(0.02)
        if not found:
            # Still not visible. Do not raise: the DLL is loaded as far as the loader is
            # concerned (LoadLibraryW succeeded), and the caller resumes the process anyway.
            # Recording it lets the caller verify afterwards and log the truth.
            #
            # Returns 0 rather than the Injection object to keep one return type. 0 is already
            # the "unknown base" value used elsewhere, and the pending flag on the injection
            # object carries the rest.
            injection.module_base = 0
            injection.pending_verification = True
            log.warn("注入后暂时枚举不到模块，将在解冻后复核", dll=dll.name)
            return 0
        base, size = found
        injection.module_base = base
        log.info("DLL 已注入", dll=dll.name, base=hex(base), size=hex(size))

        magic = read_u32(injection.hprocess, base + (export_rva(dll, "g_qsickMagic") or 0))
        if magic != ATTACH_MAGIC:
            raise InjectError(
                f"DllMain 没有留下握手值（读到 {magic!r}，期望 {hex(ATTACH_MAGIC)}）")
        return base
    finally:
        _kernel32.VirtualFreeEx(hprocess, ctypes.c_void_p(remote), 0, MEM_RELEASE)


def resume(injection: Injection) -> None:
    """放开挂起的进程，让它真正开始跑。"""
    if not IS_WINDOWS:
        raise InjectError("注入只在 Windows 上可用")
    if injection.resumed:
        return
    resumed = _kernel32.ResumeThread(wintypes.HANDLE(injection.hthread))
    if resumed == 0xFFFFFFFF:
        raise InjectError(f"ResumeThread 失败：{ctypes.get_last_error()}")
    injection.resumed = True
    log.info("已恢复运行", pid=injection.pid)


def launch_and_inject(exe: Path, dll: Path, arguments: str = "",
                      workdir: Path | None = None,
                      extra_env: dict[str, str] | None = None,
                      resume_after: bool = True,
                      before_resume: Callable[["Injection"], None] | None = None) -> Injection:
    """把"挂起 → 注入 → 恢复"三步串起来。

    ``resume_after=False`` 留给测试和需要先做别的准备的调用方；正常路径一律恢复。

    ``before_resume`` 在注入之后、进程恢复之前调用，拿到的是 :class:`Injection`。这是唯一
    能在"子进程尚未创建任何一个"时对它做设置的窗口——把进程挂进 Job Object 必须赶在这里：
    QQ 启动后几毫秒内就会派生渲染进程，晚一步那些子进程就在 Job 之外，关闭 QSICK 时不会被
    一起收掉。
    """
    injection = launch_suspended(exe, arguments, workdir, extra_env)
    try:
        inject(injection, dll)
        if before_resume is not None:
            before_resume(injection)
        if resume_after:
            resume(injection)
    except Exception:
        # 注入失败时不能留一个永远挂起的孤儿进程。
        try:
            _kernel32.TerminateProcess(wintypes.HANDLE(injection.hprocess), 1)
        except Exception:  # noqa: BLE001 - 清理路径不该掩盖原始异常
            pass
        injection.close()
        raise
    return injection

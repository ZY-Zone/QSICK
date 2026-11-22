"""Client for the control pipe the injected DLL opens inside QQ.exe.

This is the other half of the api surface work: the DLL can now evaluate JavaScript
on QQ's JS thread, and it exposes that over ``\\\\.\\pipe\\qsick-<instance>``. This
module is the Python side of that conversation.

Protocol is one request per connection, UTF-8 JSON both ways:

    {"id":"1","op":"ping"}                       -> {"id":"1","ok":true,"result":"pong"}
    {"id":"2","op":"eval","code":"1+1"}          -> {"id":"2","ok":true,"result":"2"}
    {"id":"3","op":"eval","code":"...","raw":true}  -> result is raw JSON from JS

``raw`` exists because the JS side returns a string; when the caller wants to inspect
a module's key list they need the structure back, not a quoted string.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PIPE_PREFIX = r"\\.\pipe\qsick-"
DEFAULT_TIMEOUT = 15.0


class PipeNotReady(RuntimeError):
    """Raised when the instance's control pipe is not accepting connections."""


class PipeError(RuntimeError):
    """Raised when the DLL answered with an error."""


@dataclass
class EvalResult:
    ok: bool
    value: Any
    raw: str


class PipeClient:
    """Talks to one pipe instance's control channel."""

    def __init__(self, instance: str, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.instance = instance
        self.timeout = timeout
        self.pipe_path = PIPE_PREFIX + instance
        self._counter = 0

    # -- low level ---------------------------------------------------------
    def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                    ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                    wintypes.HANDLE]
        k32.WaitNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
        k32.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                 ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        k32.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                  ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]

        GENERIC_READ_WRITE = 0xC0000000
        OPEN_EXISTING = 3
        INVALID = ctypes.c_void_p(-1).value

        # A pipe created by the DLL may be mid-handshake; wait briefly rather than
        # failing on the first miss.
        k32.WaitNamedPipeW(self.pipe_path, int(self.timeout * 1000))
        handle = k32.CreateFileW(self.pipe_path, GENERIC_READ_WRITE, 0, None,
                                 OPEN_EXISTING, 0, None)
        if not handle or handle == INVALID:
            raise PipeNotReady(
                f"无法连接 {self.pipe_path}（管道可能尚未启动；"
                f"检查实例是否已登录且 DLL 已注入）")

        try:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            written = wintypes.DWORD(0)
            if not k32.WriteFile(handle, body, len(body),
                                 ctypes.byref(written), None):
                raise PipeError(f"写入管道失败: {ctypes.get_last_error()}")

            buffer = ctypes.create_string_buffer(65600)
            read = wintypes.DWORD(0)
            if not k32.ReadFile(handle, buffer, len(buffer) - 1,
                                ctypes.byref(read), None):
                raise PipeError(f"读取管道失败: {ctypes.get_last_error()}")
            text = buffer.raw[:read.value].decode("utf-8", "replace")
        finally:
            k32.CloseHandle(handle)

        if not text:
            raise PipeError("管道返回了空响应")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise PipeError(f"响应不是合法 JSON: {text[:200]}") from exc

    # -- public API --------------------------------------------------------
    def ping(self) -> bool:
        answer = self._request({"id": self._next_id(), "op": "ping"})
        return bool(answer.get("ok")) and answer.get("result") == "pong"

    def wait_ready(self, timeout: float = 30.0, interval: float = 0.5) -> bool:
        """Polls until the pipe answers, or the timeout expires."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if self.ping():
                    return True
            except (PipeNotReady, PipeError):
                pass
            time.sleep(interval)
        return False

    def eval(self, code: str, raw: bool = False) -> EvalResult:
        """Runs ``code`` on QQ's JS thread and returns what it produced.

        ``raw`` asks the DLL to pass the JS string through unescaped, so structured
        results stay structured.
        """
        answer = self._request({
            "id": self._next_id(),
            "op": "eval",
            "code": code,
            "raw": raw,
        })
        if not answer.get("ok"):
            raise PipeError(str(answer.get("error") or "evaluation failed"))
        result = answer.get("result")
        if raw:
            return EvalResult(True, result, json.dumps(result, ensure_ascii=False))
        return EvalResult(True, result, str(result))

    def _next_id(self) -> str:
        self._counter += 1
        return str(self._counter)


def client_for_instance(instance_dir: Path | str) -> PipeClient:
    """Builds a client from an instance directory (``headlessClient/C1``)."""
    return PipeClient(Path(instance_dir).name)

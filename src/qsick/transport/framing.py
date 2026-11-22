"""Wire framing shared by the Python core and the native payload.

Layout (little endian, 32 bytes)::

    0   magic     u32   'QSK1'
    4   version   u8    1
    5   op        u8    see :class:`Op`
    6   flags     u16
    8   request   u32   correlation id
    12  status    i32   0 on success, negative/positive error codes otherwise
    16  value     u64   op-specific scalar (uin, sequence, ...)
    24  cmd_len   u16
    26  msg_len   u16
    28  body_len  u32

Payload follows the header as ``cmd | msg | body`` (utf-8, utf-8, bytes).
The header is deliberately *not* the same as any other hook's, so a stale
payload fails loudly at the magic check instead of silently mis-framing.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from enum import IntEnum

MAGIC = 0x314B5351  # b"QSK1"
VERSION = 1
HEADER = struct.Struct("<IBBHIiQHHI")
HEADER_SIZE = HEADER.size  # 32

MAX_CMD_BYTES = 4096
MAX_MSG_BYTES = 65535
MAX_BODY_BYTES = 16 * 1024 * 1024


class Op(IntEnum):
    HELLO = 1
    SEND = 2
    ACK = 3
    REPLY = 4
    EVENT = 5
    LOGIN = 6
    ERROR = 7
    PING = 8
    PONG = 9
    SHUTDOWN = 10


class ProtocolError(RuntimeError):
    pass


@dataclass(slots=True)
class Frame:
    op: int
    request: int = 0
    status: int = 0
    flags: int = 0
    value: int = 0
    cmd: str = ""
    msg: str = ""
    body: bytes = b""

    def to_bytes(self) -> bytes:
        cmd = self.cmd.encode("utf-8")
        msg = self.msg.encode("utf-8")
        if len(cmd) > MAX_CMD_BYTES:
            raise ProtocolError(f"cmd too long: {len(cmd)} > {MAX_CMD_BYTES}")
        if len(msg) > MAX_MSG_BYTES:
            raise ProtocolError(f"msg too long: {len(msg)} > {MAX_MSG_BYTES}")
        if len(self.body) > MAX_BODY_BYTES:
            raise ProtocolError(f"body too long: {len(self.body)} > {MAX_BODY_BYTES}")
        header = HEADER.pack(
            MAGIC,
            VERSION,
            self.op & 0xFF,
            self.flags & 0xFFFF,
            self.request & 0xFFFFFFFF,
            self.status,
            self.value & 0xFFFFFFFFFFFFFFFF,
            len(cmd),
            len(msg),
            len(self.body),
        )
        return b"".join((header, cmd, msg, self.body))


class FrameDecoder:
    """Incremental decoder: push arbitrary chunks, get complete frames back."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def push(self, data: bytes) -> list[Frame]:
        self._buf.extend(data)
        frames: list[Frame] = []
        while True:
            if len(self._buf) < HEADER_SIZE:
                return frames
            magic, version, op, flags, request, status, value, cmd_len, msg_len, body_len = HEADER.unpack_from(
                self._buf, 0
            )
            if magic != MAGIC:
                raise ProtocolError(f"bad magic 0x{magic:08x}")
            if version != VERSION:
                raise ProtocolError(f"unsupported payload version {version}")
            _check_lengths(cmd_len, msg_len, body_len)
            total = HEADER_SIZE + cmd_len + msg_len + body_len
            if len(self._buf) < total:
                return frames
            payload = bytes(self._buf[HEADER_SIZE:total])
            del self._buf[:total]
            cursor = 0
            cmd = payload[cursor : cursor + cmd_len].decode("utf-8", "replace")
            cursor += cmd_len
            msg = payload[cursor : cursor + msg_len].decode("utf-8", "replace")
            cursor += msg_len
            body = payload[cursor : cursor + body_len]
            frames.append(
                Frame(
                    op=op,
                    request=request,
                    status=status,
                    flags=flags,
                    value=value,
                    cmd=cmd,
                    msg=msg,
                    body=body,
                )
            )

    def reset(self) -> None:
        self._buf.clear()


def _check_lengths(cmd_len: int, msg_len: int, body_len: int) -> None:
    if cmd_len > MAX_CMD_BYTES:
        raise ProtocolError(f"cmd length {cmd_len} exceeds {MAX_CMD_BYTES}")
    if msg_len > MAX_MSG_BYTES:
        raise ProtocolError(f"msg length {msg_len} exceeds {MAX_MSG_BYTES}")
    if body_len > MAX_BODY_BYTES:
        raise ProtocolError(f"body length {body_len} exceeds {MAX_BODY_BYTES}")


def hello(value: int = 0) -> Frame:
    return Frame(op=Op.HELLO, value=value)


def event(cmd: str, body: bytes = b"", *, value: int = 0, request: int = 0) -> Frame:
    return Frame(op=Op.EVENT, cmd=cmd, body=body, value=value, request=request)


def reply(frame: Frame, status: int = 0, *, body: bytes = b"", msg: str = "") -> Frame:
    return Frame(op=Op.REPLY, request=frame.request, status=status, body=body, msg=msg)

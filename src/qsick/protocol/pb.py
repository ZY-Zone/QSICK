"""Minimal protobuf helpers for plugins that need to build OIDB bodies.

QSICK deliberately does not hard-code QQ's envelope field numbers: those belong
to the payload (or to a version-pinned plugin), not to a generic core. This
module gives you the primitives to build a body without pulling in a compiler.

>>> message({1: varint(202), 4: bytes_(b"\\x08\\x01")})
"""

from __future__ import annotations

from typing import Any, Iterable

WIRE_VARINT = 0
WIRE_64BIT = 1
WIRE_LEN = 2
WIRE_32BIT = 5


class ProtoError(ValueError):
    pass


def varint(value: int) -> bytes:
    if value < 0:
        value += 1 << 64
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if pos >= len(buf):
            raise ProtoError("truncated varint")
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise ProtoError("varint too long")


def tag(field: int, wire: int) -> bytes:
    return varint((field << 3) | wire)


def varint_field(field: int, value: int) -> bytes:
    return tag(field, WIRE_VARINT) + varint(int(value))


def bool_field(field: int, value: bool) -> bytes:
    return varint_field(field, 1 if value else 0)


def bytes_field(field: int, value: bytes) -> bytes:
    return tag(field, WIRE_LEN) + varint(len(value)) + value


def string_field(field: int, value: str) -> bytes:
    return bytes_field(field, value.encode("utf-8"))


def message_field(field: int, body: bytes) -> bytes:
    return bytes_field(field, body)


def fixed32_field(field: int, value: int) -> bytes:
    return tag(field, WIRE_32BIT) + int(value & 0xFFFFFFFF).to_bytes(4, "little")


def fixed64_field(field: int, value: int) -> bytes:
    return tag(field, WIRE_64BIT) + int(value & 0xFFFFFFFFFFFFFFFF).to_bytes(8, "little")


def message(fields: Iterable[tuple[int, bytes]] | dict[int, bytes]) -> bytes:
    items = fields.items() if isinstance(fields, dict) else fields
    return b"".join(payload for _, payload in sorted(items, key=lambda pair: pair[0]))


def decode(buf: bytes) -> dict[int, list[Any]]:
    """Schema-less decode: field -> list of ints / bytes (length-delimited)."""
    out: dict[int, list[Any]] = {}
    pos = 0
    while pos < len(buf):
        key, pos = read_varint(buf, pos)
        field, wire = key >> 3, key & 0x7
        if wire == WIRE_VARINT:
            value, pos = read_varint(buf, pos)
        elif wire == WIRE_LEN:
            length, pos = read_varint(buf, pos)
            if pos + length > len(buf):
                raise ProtoError("truncated length-delimited field")
            value = buf[pos : pos + length]
            pos += length
        elif wire == WIRE_32BIT:
            value = int.from_bytes(buf[pos : pos + 4], "little")
            pos += 4
        elif wire == WIRE_64BIT:
            value = int.from_bytes(buf[pos : pos + 8], "little")
            pos += 8
        else:
            raise ProtoError(f"unsupported wire type {wire}")
        out.setdefault(field, []).append(value)
    return out


def first(buf: bytes, field: int, default: Any = None) -> Any:
    values = decode(buf).get(field)
    return values[0] if values else default


def as_text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return "" if value is None else str(value)

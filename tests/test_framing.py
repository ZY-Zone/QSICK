from __future__ import annotations

import pytest

from qsick.transport.framing import (
    HEADER_SIZE,
    MAX_CMD_BYTES,
    Frame,
    FrameDecoder,
    Op,
    ProtocolError,
)


def test_roundtrip_preserves_all_fields():
    frame = Frame(
        op=Op.SEND,
        request=42,
        status=-39,
        flags=5,
        value=1234567890123,
        cmd="message.send",
        msg="hello",
        body=b"\x00\x01\x02",
    )
    decoded = FrameDecoder().push(frame.to_bytes())
    assert len(decoded) == 1
    out = decoded[0]
    assert (out.op, out.request, out.status, out.flags, out.value) == (Op.SEND, 42, -39, 5, 1234567890123)
    assert (out.cmd, out.msg, out.body) == ("message.send", "hello", b"\x00\x01\x02")


def test_header_is_32_bytes():
    assert HEADER_SIZE == 32
    assert len(Frame(op=Op.PING).to_bytes()) == HEADER_SIZE


def test_incremental_decode_survives_fragmentation():
    payload = Frame(op=Op.EVENT, cmd="message.group", body=b"{}").to_bytes()
    decoder = FrameDecoder()
    frames = []
    for index in range(0, len(payload), 3):
        frames.extend(decoder.push(payload[index : index + 3]))
    assert len(frames) == 1
    assert frames[0].cmd == "message.group"


def test_multiple_frames_in_one_chunk():
    chunk = b"".join(Frame(op=Op.EVENT, cmd=f"c{i}", value=i).to_bytes() for i in range(5))
    frames = FrameDecoder().push(chunk)
    assert [frame.cmd for frame in frames] == ["c0", "c1", "c2", "c3", "c4"]


def test_bad_magic_is_fatal():
    data = bytearray(Frame(op=Op.EVENT, cmd="x").to_bytes())
    data[0] ^= 0xFF
    with pytest.raises(ProtocolError):
        FrameDecoder().push(bytes(data))


def test_oversized_command_rejected_before_send():
    with pytest.raises(ProtocolError):
        Frame(op=Op.SEND, cmd="a" * (MAX_CMD_BYTES + 1)).to_bytes()


def test_decoder_rejects_absurd_body_length():
    frame = Frame(op=Op.SEND, cmd="x")
    data = bytearray(frame.to_bytes())
    data[28:32] = (64 * 1024 * 1024).to_bytes(4, "little")
    with pytest.raises(ProtocolError):
        FrameDecoder().push(bytes(data))

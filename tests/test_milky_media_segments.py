"""Milky media/mention segments must carry the fields the spec names.

The values already exist in the internal part (the codec puts them there from the native
element); the gap was that the encoder only emitted `resource_id` and `url`, so a client
saw no width, height, duration or file name, and `temp_url` (the spec name) was absent.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from protocols.milky import codec  # noqa: E402
from protocols.milky.events import convert  # noqa: E402


def encode(part):
    return codec.part_to_segment(part)


def test_image_carries_the_spec_fields():
    segment = encode({
        "kind": "image", "file_id": "uuid-1", "url": "https://cdn/a.png",
        "width": 640, "height": 480, "summary": "[图片]", "sub_type": 1,
    })
    assert segment == {
        "type": "image", "resource_id": "uuid-1", "temp_url": "https://cdn/a.png",
        "width": 640, "height": 480, "summary": "[图片]", "sub_type": "sticker",
    }


def test_image_without_resolved_url_does_not_invent_one():
    segment = encode({"kind": "image", "file_id": "uuid-1", "width": 10, "height": 20})
    assert segment == {"type": "image", "resource_id": "uuid-1", "width": 10, "height": 20}
    assert "temp_url" not in segment
    assert "summary" not in segment


@pytest.mark.parametrize("sub_type,expected", [
    (0, "normal"), (1, "sticker"), ("sticker", "sticker"), ("normal", "normal"),
    (7, None), ("weird", None),
])
def test_image_sub_type_is_mapped_or_omitted(sub_type, expected):
    segment = encode({"kind": "image", "file_id": "f", "sub_type": sub_type})
    assert segment.get("sub_type") == expected


@pytest.mark.parametrize("kind,fields", [
    ("record", {"duration": 5}),
    ("video", {"width": 1920, "height": 1080, "duration": 12}),
])
def test_record_and_video_keep_their_measurements(kind, fields):
    segment = encode({"kind": kind, "file_id": "r1", "url": "https://cdn/x", **fields})
    assert segment["type"] == kind
    assert segment["resource_id"] == "r1"
    assert segment["temp_url"] == "https://cdn/x"
    for key, value in fields.items():
        assert segment[key] == value


def test_file_segment_uses_file_id_name_and_size():
    """The spec names the file segment's fields differently from the other media types."""
    segment = encode({"kind": "file", "file_id": "f1", "file_name": "a.txt", "file_size": "12"})
    assert segment == {"type": "file", "file_id": "f1", "file_name": "a.txt", "file_size": "12"}
    assert "resource_id" not in segment


def test_mention_keeps_the_optional_name():
    assert encode({"kind": "at", "qq": "111", "name": "nick"}) == {
        "type": "mention", "user_id": 111, "name": "nick"}
    assert encode({"kind": "at", "qq": "111"}) == {"type": "mention", "user_id": 111}
    assert encode({"kind": "at", "qq": "all"}) == {"type": "mention_all"}


def test_inbound_segments_accept_the_spec_spellings():
    """Sending an image from a Milky client uses `temp_url`/`file_name`, not the internal names."""
    part = codec.segment_to_part({
        "type": "image", "resource_id": "uuid-1", "temp_url": "https://cdn/a.png",
        "width": 32, "height": 16, "summary": "[图片]", "sub_type": "sticker",
    })
    assert part["kind"] == "image"
    assert part["file_id"] == "uuid-1"
    assert part["url"] == "https://cdn/a.png"
    assert part["width"] == 32 and part["height"] == 16
    assert part["summary"] == "[图片]"
    assert part["sub_type"] == 1

    upload = codec.segment_to_part({
        "type": "file", "file_id": "f1", "file_name": "a.txt", "file_size": "12"})
    assert upload["kind"] == "file"
    assert upload["file_id"] == "f1"
    assert upload["file_name"] == "a.txt"
    assert upload["file_size"] == "12"

    mention = codec.segment_to_part({"type": "mention", "user_id": 5, "name": "n"})
    assert mention == {"kind": "at", "qq": "5", "name": "n"}


def test_round_trip_of_an_image_segment_keeps_its_metadata():
    original = {
        "type": "image", "resource_id": "uuid-1", "temp_url": "https://cdn/a.png",
        "width": 8, "height": 9, "summary": "[图片]", "sub_type": "normal",
    }
    again = codec.part_to_segment(codec.segment_to_part(original))
    assert again == original


def test_measurements_survive_the_whole_event_conversion():
    """The Milky encoder reads v11 segments, so the v11 segment must not drop them first."""
    import asyncio

    from qsick.config import RuntimeConfig
    from qsick.onebot.events import EventMapper
    from qsick.protocol.message import Message
    from qsick.storage import MemoryStore

    mapper = EventMapper(RuntimeConfig(data_dir=str(ROOT / "temp" / "segments-test")),
                         MemoryStore(), None, self_id=3905548034)
    message = Message.from_payload({
        "account": "3905548034", "message_id": "7712042647111528014", "seq": "249965",
        "time": "1795599853", "chat": {"type": "group", "id": 1057604000},
        "sender": {"uin": "3889042293", "uid": "u_x", "nickname": "x", "card": "", "role": "member"},
        "elements": [
            {"type": "image", "data": {"file_id": "uuid-1", "width": 640, "height": 480,
                                       "sub_type": 1, "summary": "[图片]", "url": "https://cdn/a.png"}},
            {"type": "video", "data": {"file_id": "v1", "width": 1920, "height": 1080, "duration": 12}},
            {"type": "record", "data": {"file_id": "r1", "duration": 5}},
        ],
        "native": {"msgSeq": "249965"},
    })
    event = asyncio.run(mapper.message(message, sent=False))

    image, video, record = event["message"]
    assert (image["data"]["width"], image["data"]["height"]) == (640, 480)
    assert video["data"]["duration"] == 12
    assert record["data"]["duration"] == 5

    segments = convert(event)["data"]["segments"]
    assert segments[0]["width"] == 640 and segments[0]["height"] == 480
    assert segments[0]["sub_type"] == "sticker"
    assert segments[1]["duration"] == 12
    assert segments[2]["duration"] == 5

from __future__ import annotations

import pytest

from qsick.onebot.actions import message_to_elements
from qsick.protocol import elements as E


def test_unknown_element_is_preserved_not_dropped():
    element = E.normalize({"type": "giphy", "data": {"id": "abc"}})
    assert element.type == "unknown"
    segment = E.to_onebot(element)
    assert segment["type"] == "unknown"
    assert segment["data"]["original_type"] == "giphy"
    assert segment["data"]["data"]["id"] == "abc"


def test_known_element_roundtrip():
    for segment in (
        {"type": "text", "data": {"text": "hi"}},
        {"type": "at", "data": {"qq": "10001"}},
        {"type": "face", "data": {"id": "14"}},
        {"type": "reply", "data": {"id": "9"}},
        {"type": "image", "data": {"file": "abc"}},
    ):
        element = E.from_onebot(segment)
        assert element.type == segment["type"]
        out = E.to_onebot(element)
        assert out["type"] == segment["type"]


def test_flash_file_keeps_identity_fields():
    element = E.normalize(
        {"type": "flash_file", "data": {"title": "打包", "file_set_id": "FS1", "scene_type": 2, "thumb": "u"}}
    )
    segment = E.to_onebot(element)
    # Emitted under NapCat's name, not QSICK's old one.
    #
    # `flashtransfer` is what `OB11MessageDataType.flashtransfer` declares, and a consumer switches on the
    # segment type string -- `flash_file` reached it as an unrecognised segment and was dropped silently.
    assert segment == {
        "type": "flashtransfer",
        "data": {"title": "打包", "file_set_id": "FS1", "scene_type": 2, "thumb": "u"},
    }


def test_flash_transfer_is_accepted_under_either_spelling():
    # Old payloads and hand-built Elements must keep working; only the emitted name is canonical.
    for spelling in ("flash_file", "flashtransfer"):
        element = E.normalize({"type": spelling, "data": {"file_set_id": "FS1"}})
        assert element.type == "flashtransfer", spelling
        assert E.to_onebot(element)["type"] == "flashtransfer", spelling
        assert E.to_onebot(E.Element(spelling, {"file_set_id": "FS1"}))["type"] == "flashtransfer", spelling


def test_cq_string_escapes_brackets():
    elements = [
        E.normalize({"type": "text", "data": {"text": "a[b]c"}}),
        E.normalize({"type": "at", "data": {"user_id": 10001}}),
    ]
    text = E.to_cq_string(elements)
    assert "a&#91;b&#93;c" in text
    assert "[CQ:at,qq=10001]" in text


async def test_resolve_media_fills_url_only_when_missing():
    filled = E.normalize({"type": "image", "data": {"file_id": "f1"}})
    kept = E.normalize({"type": "image", "data": {"file_id": "f2", "url": "https://x"}})

    async def resolver(element: E.Element) -> str:
        return f"https://cdn/{element.data['file_id']}"

    await E.resolve_media([filled, kept], resolver)
    assert filled.data["url"] == "https://cdn/f1"
    assert kept.data["url"] == "https://x"


async def test_resolve_media_survives_a_broken_resolver():
    element = E.normalize({"type": "image", "data": {"file_id": "f1"}})

    async def boom(_: E.Element) -> str:
        raise RuntimeError("rkey service down")

    await E.resolve_media([element], boom)
    assert "url" not in element.data


def test_cq_parser_handles_multiple_segments():
    elements = message_to_elements("hi [CQ:at,qq=10001] there")
    assert [item["type"] for item in elements] == ["text", "at", "text"]
    assert elements[1]["data"]["user_id"] == 10001


def test_unsupported_send_segment_is_a_clear_error():
    with pytest.raises(Exception) as excinfo:
        message_to_elements([{"type": "hologram", "data": {}}])
    assert "unsupported" in str(excinfo.value)


def test_extended_segment_types_roundtrip():
    cases = [
        {"type": "dice", "data": {"result": 3}},
        {"type": "rps", "data": {"result": 2}},
        {"type": "contact", "data": {"type": "group", "id": "30001"}},
        {"type": "location", "data": {"lat": 31.2, "lon": 121.4, "title": "here"}},
        {"type": "music", "data": {"type": "163", "id": "123"}},
        {"type": "miniapp", "data": {"data": '{"app":"x"}'}},
    ]
    for segment in cases:
        element = E.from_onebot(segment)
        assert element.type == segment["type"], segment
        out = E.to_onebot(element)
        assert out["type"] == segment["type"]


def test_extended_types_render_in_cq_and_preserve_unknown():
    elements = [E.normalize({"type": "dice", "data": {"result": 6}})]
    assert "[CQ:dice,result=6]" in E.to_cq_string(elements)
    weird = E.normalize({"type": "hologram", "data": {"x": 1}})
    assert E.to_onebot(weird)["type"] == "unknown"

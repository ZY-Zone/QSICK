"""输入框表情标记的解析。

标记有两条来源：``[CQ:ext_emotion,json=...]``（EXTemotions 规范附录 B 的兜底
形态，也是选择器插入的形态）和保留兼容的 ``[CQ:face,id=N]``。两者最大的区别是
**后者什么都没声明**——早先这里替它硬塞了静态格式，结果动态表情被当静态发出去，
正是服务端把 424 重映射成 168 的根源。
"""

from __future__ import annotations

import pytest

from qsick.protocol.extemotion import (
    EmotionMarkupError,
    decode_marker,
    encode_marker,
    split_segments,
)
from qsick.webui.chat_api import _chat_parts, _encode_chat_parts


def kinds(segments):
    return [(item["type"], item["data"].get("id") or item["data"].get("text")) for item in segments]


SUPER = {
    "schema": "EXTemotions", "version": "1.0", "type": "super", "id": "364",
    "uid": "super:364", "moveable": True, "fileType": "lottie", "name": "超级赞",
    "pack": {"id": "2", "index": 1}, "qq": {"aniStickerType": 1},
}


def test_marker_round_trips_through_encode_and_decode():
    marker = encode_marker(SUPER)
    assert marker.startswith("[CQ:ext_emotion,json=")
    [segment] = split_segments(marker)
    assert segment["type"] == "emotion"
    assert segment["data"]["id"] == "364"
    assert segment["data"]["moveable"] is True
    assert segment["data"]["fileType"] == "lottie"


def test_marker_mixes_with_text_in_order():
    segments = split_segments("看" + encode_marker(SUPER) + "好")
    assert kinds(segments) == [("text", "看"), ("emotion", "364"), ("text", "好")]


def test_legacy_cq_face_declares_nothing():
    """旧写法只有 id；格式必须留给目录决定。"""
    [segment] = split_segments("[CQ:face,id=5]")
    assert segment["type"] == "emotion"
    assert segment["data"] == {"id": "5", "declared": False}


def test_plain_text_is_untouched():
    segments = split_segments("普通消息")
    assert kinds(segments) == [("text", "普通消息")]


@pytest.mark.parametrize("marker", [
    "[CQ:ext_emotion,json=not-base64!!]",
    "[CQ:ext_emotion,json=eyJpZCI6IiJ9]",          # id 为空
    "[CQ:face,id=abc]",                            # 非数字旧式 id
    "[CQ:face,id=0]",                              # 0 不是合法编号
])
def test_malformed_markers_are_rejected(marker):
    with pytest.raises(EmotionMarkupError):
        split_segments(marker)


def test_wrong_schema_is_refused():
    import base64
    import json

    blob = base64.urlsafe_b64encode(
        json.dumps({"schema": "SomethingElse", "id": "1"}).encode()).decode().rstrip("=")
    with pytest.raises(EmotionMarkupError):
        split_segments(f"[CQ:ext_emotion,json={blob}]")
    with pytest.raises(EmotionMarkupError):
        decode_marker("!!!not base64!!!")


def test_a_declared_marker_is_trusted_over_the_catalogue():
    marker = encode_marker(SUPER)
    parts = _chat_parts(marker)
    emotion = parts[0]["emotion"]
    assert emotion["type"] == "super"
    assert emotion["moveable"] is True
    assert emotion["fileType"] == "lottie"


def test_a_legacy_face_gets_its_semantics_from_the_catalogue():
    """14 是静态黄脸、364 是动效表情：都从索引查，不靠 id 区间猜。"""
    static_segment = _encode_chat_parts("[CQ:face,id=14]")[0]["data"]
    assert static_segment["format"] == "static"
    assert static_segment["file_type"] == "png"

    animated_segment = _encode_chat_parts("[CQ:face,id=364]")[0]["data"]
    assert animated_segment["format"] == "animated"
    assert animated_segment["file_type"] == "lottie"


def test_send_path_fills_the_sticker_triple_from_the_catalogue():
    data = _encode_chat_parts("[CQ:face,id=5]")[0]["data"]
    # 5 是"双身份"表情之一：目录里有动效资源，所以要按 AniSticke 发。
    assert data["pack_id"] == "1"
    assert data["sticker_id"] == "16"
    assert str(data["sticker_type"]) == "1"


def test_a_static_face_carries_no_sticker_triple():
    data = _encode_chat_parts("[CQ:face,id=14]")[0]["data"]
    assert "sticker_type" not in data


def test_text_stays_text():
    parts = _chat_parts("纯文本")
    assert parts == [{"kind": "text", "text": "纯文本"}]

def test_an_animated_face_without_a_sticker_triple_degrades_instead_of_failing():
    """419+ 那批拓展表情没有贴纸三元组，QQ 的动效通道发不了。

    它们必须仍然可发——降级成静态，而不是让整条消息失败。谎报成动态更糟：
    QQNT 会按 AniSticke 发，然后被服务端重映射成另一张画。
    """
    from qsick.protocol.extemotion import encode_marker

    emotion = {"schema": "EXTemotions", "version": "1.0", "type": "super",
               "id": "478", "uid": "super:478", "moveable": True, "fileType": "apng"}
    [segment] = _encode_chat_parts(encode_marker(emotion))
    data = segment["data"]
    assert data["id"] == "478"
    # 降级成静态：这是唯一能发出去且不撒谎的形态。
    assert data["format"] == "static"
    assert "sticker_type" not in data


def test_a_sendable_animated_face_keeps_its_motion():
    from qsick.protocol.extemotion import encode_marker

    emotion = {"schema": "EXTemotions", "version": "1.0", "type": "super",
               "id": "364", "uid": "super:364", "moveable": True, "fileType": "lottie"}
    [segment] = _encode_chat_parts(encode_marker(emotion))
    data = segment["data"]
    assert data["format"] == "animated"
    assert data["sticker_type"] == 1

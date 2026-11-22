"""协议注册表与消息处理器。

这两块是新增的跨界层：注册表决定"这台机器上有哪些协议"，处理器决定"消息怎么
从内部形状翻译成对端协议的形状"。它们出错的后果是静默的——表情照样发出去，
只是语义错了——所以要有测试盯着。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    # protocols/ 在项目根而不是 src 下，editable 安装不暴露它。
    sys.path.insert(0, str(ROOT))

from protocols import registry
from protocols.processor import DispatchError, MessageProcessor, processor_for


def test_registry_lists_both_chat_protocols_and_hides_the_store():
    ids = registry.chat_protocol_ids()
    assert "onebotv11" in ids
    assert "onebotv11-EXTemotions" in ids
    # maxStore 是存储协议，绝不能作为聊天协议被选中。
    assert registry.STORE_PROTOCOL not in ids


def test_every_registered_protocol_can_be_loaded():
    for identifier in registry.chat_protocol_ids():
        protocol = registry.load_protocol(identifier)
        assert protocol.name == identifier
        assert protocol.capabilities()


def test_store_protocol_is_refused_as_a_chat_protocol():
    with pytest.raises(registry.ProtocolError):
        registry.load_protocol("maxStore")


def test_unregistered_protocol_is_refused():
    with pytest.raises(registry.ProtocolError):
        registry.load_protocol("definitely-not-a-protocol")


def test_extemotions_keeps_what_plain_v11_drops():
    """这是两个协议的全部差别所在。"""
    parts = [{"kind": "emotion", "emotion": {
        "id": "392", "format": "animated", "fileType": "lottie",
        "pack_id": "1", "sticker_id": "38", "sticker_type": 3, "name": "龙年快乐"}}]

    plain = registry.load_protocol("onebotv11").encode_message(parts)
    rich = registry.load_protocol("onebotv11-EXTemotions").encode_message(parts)

    # 纯 v11 只留得住 id。
    assert plain[0]["data"] == {"id": "392"}
    # EXTemotions 把动态性与文件类型都带上了。
    data = rich[0]["data"]
    assert data["format"] == "animated"
    assert data["file_type"] == "lottie"
    assert data["sticker_type"] == 3
    assert data["name"] == "龙年快乐"


def test_extemotions_declares_richer_capabilities():
    plain = registry.load_protocol("onebotv11").capabilities()
    rich = registry.load_protocol("onebotv11-EXTemotions").capabilities()
    assert plain.get("emotion_rich_fields") is False
    assert rich.get("emotion_rich_fields") is True
    assert "super" in rich.get("emotion_types", [])


def test_roundtrip_preserves_emotion_semantics():
    protocol = registry.load_protocol("onebotv11-EXTemotions")
    parts = [{"kind": "text", "text": "看"},
             {"kind": "emotion", "emotion": {"id": "364", "format": "animated",
                                             "fileType": "lottie", "name": "超级赞"}}]
    back = protocol.decode_message(protocol.encode_message(parts))
    assert back[0] == {"kind": "text", "text": "看"}
    emotion = back[1]["emotion"]
    assert emotion["id"] == "364"
    assert emotion["moveable"] is True
    assert emotion["fileType"] == "lottie"


def test_processor_encodes_with_the_selected_protocol():
    parts = [{"kind": "emotion", "emotion": {"id": "14", "format": "static",
                                             "fileType": "png", "name": "微笑"}}]
    rich = MessageProcessor("onebotv11-EXTemotions").encode_outbound(parts)
    plain = MessageProcessor("onebotv11").encode_outbound(parts)
    assert rich[0]["data"]["format"] == "static"
    assert plain[0]["data"] == {"id": "14"}
    assert rich[0]["data"]["file_type"] == "png"


def test_processor_rejects_an_empty_message():
    processor = MessageProcessor("onebotv11")
    with pytest.raises(DispatchError):
        processor.encode_outbound([])


def test_processor_summarises_faces_by_name_not_id():
    processor = MessageProcessor("onebotv11-EXTemotions")
    parts = [{"kind": "text", "text": "好"},
             {"kind": "emotion", "emotion": {"id": "14", "name": "微笑"}}]
    assert processor.summarize(parts) == "好[微笑]"


def test_processor_falls_back_when_the_protocol_is_unknown():
    """一条配置写错协议名，不该让整个运行时起不来。"""
    processor = processor_for("no-such-protocol")
    assert processor.protocol_id == registry.default_protocol_id()


def test_processor_enriches_before_encoding():
    """只有 id 的表情要补全成语义完整的对象，否则会退化成"不知道是否动态"。"""
    def enrich(emotion):
        return {**emotion, "format": "animated", "fileType": "apng", "moveable": True}

    processor = MessageProcessor("onebotv11-EXTemotions", enrich=enrich)
    segments = processor.encode_outbound([{"kind": "emotion", "emotion": {"id": "9"}}])
    data = segments[0]["data"]
    assert data["format"] == "animated"
    assert data["file_type"] == "apng"


def test_processor_send_message_targets_the_right_action():
    captured = {}

    async def dispatch(action, params):
        captured["action"] = action
        captured["params"] = params
        return {"ok": True}

    import asyncio

    processor = MessageProcessor("onebotv11-EXTemotions", dispatch)
    asyncio.run(processor.send_message({"kind": "group", "id": "1057604000"},
                                       [{"kind": "text", "text": "hi"}]))
    assert captured["action"] == "send_group_msg"
    assert captured["params"]["group_id"] == 1057604000

    asyncio.run(processor.send_message({"kind": "private", "id": "123"},
                                       [{"kind": "text", "text": "hi"}]))
    assert captured["action"] == "send_private_msg"
    assert captured["params"]["user_id"] == 123


def test_processor_rejects_an_unsupported_target():
    processor = MessageProcessor("onebotv11")
    import asyncio

    with pytest.raises(DispatchError):
        asyncio.run(processor.send_message({"kind": "channel", "id": "1"},
                                           [{"kind": "text", "text": "x"}]))
    with pytest.raises(DispatchError):
        asyncio.run(processor.send_message({"kind": "group"}, [{"kind": "text", "text": "x"}]))


def test_unknown_segments_survive_the_round_trip():
    """不认识的段必须原样带过去，否则上层永远看不到"有东西来了但我不认"。"""
    protocol = registry.load_protocol("onebotv11-EXTemotions")
    segments = [{"type": "poke", "data": {"id": "7"}}]
    parts = protocol.decode_message(segments)
    assert parts[0]["kind"] == "unknown"
    assert parts[0]["type"] == "poke"
    assert protocol.encode_message(parts) == segments

def test_config_remembers_which_file_it_came_from(tmp_path):
    """保存设置必须写回启动时读的那份配置。

    早先 ``_write_setting`` 只认 ``QSICK_CONFIG`` / ``qsick.json``，于是用
    ``--config other.json`` 起的进程一旦在 WebUI 保存设置，端口就被写进了
    ``qsick.json``——正在用的配置没变，另一份被改坏，表现是"端口莫名其妙变了"。
    """
    import json

    from qsick.config import load_config

    source = tmp_path / "custom.json"
    source.write_text(json.dumps({"webui": {"port": 5788},
                                  "onebot": {"port": 3088}}), encoding="utf-8")
    config = load_config(str(source))
    assert config.webui.port == 5788
    assert config.onebot.port == 3088
    # 来源路径必须被记住，保存逻辑据此决定写到哪儿。
    assert config.source_path == str(source)


def test_the_real_config_is_not_the_default_one():
    """默认配置文件与"实际读到的文件"必须是同一个概念。

    没有 source_path 时保存会回落 qsick.json；有 source_path 时应以它为准。
    """
    from qsick.config import RuntimeConfig

    assert RuntimeConfig().source_path == ""

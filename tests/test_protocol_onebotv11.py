"""纯 OneBot v11 协议适配器的测试。

覆盖四件事：内部部件与 v11 段的往返、emotion 到 face 的降级、CQ 字符串的
解析，以及未知段必须透传而不能丢失。

关于 import：``protocols/`` 在项目根、不在 ``src`` 下，所以它不在 editable
install 暴露的路径里（只有 ``src`` 在里面）。下面这段引导把项目根塞进
``sys.path``，让测试能 ``import protocols``；项目本身从根目录启动时天然满足
同样的条件，不需要额外配置。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from protocols.base import ChatProtocol, PartError  # noqa: E402
from protocols.onebotv11 import OneBotV11Protocol, codec  # noqa: E402
from protocols.onebotv11.adapter import create, from_runtime  # noqa: E402


@pytest.fixture()
def proto() -> OneBotV11Protocol:
    return create()


# ------------------------------------------------------------------ 基本契约


def test_adapter_implements_the_base_contract(proto: OneBotV11Protocol) -> None:
    assert isinstance(proto, ChatProtocol)
    assert proto.name == "onebotv11"


def test_capabilities_mark_emotion_as_degraded(proto: OneBotV11Protocol) -> None:
    caps = proto.capabilities()
    assert caps["protocol"] == "onebotv11"
    # 纯 v11 表达不了动态表情，能力表必须如实说“要降级”，上层才有机会换协议。
    assert caps["parts"]["emotion"] is False
    assert caps["degraded"] == {"emotion": "face"}
    assert caps["emotion_rich_fields"] is False
    assert caps["unknown_segments"] == "passthrough"


# ----------------------------------------------------------------- 文本往返


def test_text_round_trip(proto: OneBotV11Protocol) -> None:
    parts = [{"kind": "text", "text": "你好，世界"}]
    segments = proto.encode_message(parts)
    assert segments == [{"type": "text", "data": {"text": "你好，世界"}}]
    assert proto.decode_message(segments) == parts


def test_text_with_cq_metacharacters_survives_array_form() -> None:
    # 数组形态不转义：文本就是文本，客户端不该做二次解析。
    parts = [{"kind": "text", "text": "[CQ:face,id=1] & 100%"}]
    assert codec.parts_to_segments(parts) == [{"type": "text", "data": {"text": "[CQ:face,id=1] & 100%"}}]


def test_empty_text_part_is_preserved_not_dropped(proto: OneBotV11Protocol) -> None:
    parts = [{"kind": "text", "text": ""}]
    assert proto.encode_message(parts) == [{"type": "text", "data": {"text": ""}}]
    assert proto.decode_message([{"type": "text", "data": {"text": ""}}]) == parts


# ---------------------------------------------------------------- face 往返


def test_face_segment_round_trip(proto: OneBotV11Protocol) -> None:
    parts = proto.decode_message([{"type": "face", "data": {"id": "14"}}])
    assert parts == [{"kind": "emotion", "emotion": {"id": "14"}}]
    assert proto.encode_message(parts) == [{"type": "face", "data": {"id": "14"}}]


def test_face_decode_does_not_invent_rich_fields(proto: OneBotV11Protocol) -> None:
    """v11 的 face 段没有“是否动态”，解码就不能假装有。

    补 format/animated 属于上层目录知识，编在协议层会让“协议说了什么”
    和“我们从目录里查到了什么”再也分不开。
    """
    [part] = proto.decode_message([{"type": "face", "data": {"id": "5"}}])
    assert set(part["emotion"]) == {"id"}


def test_face_kind_is_accepted_as_an_alias_for_emotion(proto: OneBotV11Protocol) -> None:
    # OneBot 客户端习惯写 kind="face"，内部统一成 emotion，避免两套同义结构。
    assert proto.encode_message([{"kind": "face", "id": 3}]) == [{"type": "face", "data": {"id": "3"}}]


def test_emotion_is_degraded_to_a_plain_face_segment(proto: OneBotV11Protocol) -> None:
    """EXTemotions 的富表情进纯 v11 时降级成 face。

    降级只允许丢协议表达不了的信息（动态标记、贴纸三元组），id 必须留住 ——
    否则上层无法知道到底发了哪个表情。
    """
    parts = [{
        "kind": "emotion",
        "emotion": {
            "id": "424",
            "format": "animated",
            "animated": True,
            "file_type": "lottie",
            "pack_id": "1",
            "sticker_id": "52",
            "sticker_type": 1,
            "text": "续标识",
        },
    }]
    assert proto.encode_message(parts) == [{"type": "face", "data": {"id": "424"}}]


def test_glyph_emotion_ids_stay_intact_when_degraded(proto: OneBotV11Protocol) -> None:
    # QQ 的字形 id 不是数字，降级不能把它强转成 int。
    segments = proto.encode_message([{"kind": "emotion", "emotion": {"id": "😊"}}])
    assert segments == [{"type": "face", "data": {"id": "😊"}}]


def test_emotion_without_id_is_rejected() -> None:
    # 空表情段发出去只会被 QQ 丢掉，不如在入口报错。
    with pytest.raises(PartError):
        codec.parts_to_segments([{"kind": "emotion", "emotion": {}}])


# --------------------------------------------------------------- CQ 字符串


def test_cq_string_parses_text_and_face() -> None:
    segments = codec.parse_cq_string("[CQ:face,id=14]你好[CQ:face,id=5]")
    assert segments == [
        {"type": "face", "data": {"id": "14"}},
        {"type": "text", "data": {"text": "你好"}},
        {"type": "face", "data": {"id": "5"}},
    ]


def test_cq_string_decodes_into_parts(proto: OneBotV11Protocol) -> None:
    parts = proto.decode_cq_string("早上好[CQ:face,id=14]")
    assert parts == [
        {"kind": "text", "text": "早上好"},
        {"kind": "emotion", "emotion": {"id": "14"}},
    ]


def test_cq_escaping_round_trips_through_string_form(proto: OneBotV11Protocol) -> None:
    """正文里的 ``[``、``]``、``&`` 必须在字符串形态里转义，否则再解析会变成段。"""
    original = [{"kind": "text", "text": "看[CQ:face,id=1]&好"}]
    raw = proto.encode_cq_string(original)
    assert raw == "看&#91;CQ:face,id=1&#93;&amp;好"
    assert proto.decode_cq_string(raw) == original


def test_cq_parameter_values_escape_commas() -> None:
    # 参数值里的逗号不转义就会被当成分隔符，把 file 截成两段。
    [segment] = codec.parse_cq_string("[CQ:image,file=a,b.png,url=https://x/y]")
    assert segment["data"] == {"file": "a", "b.png": "", "url": "https://x/y"}
    # 转义之后才是能往返的形态：整段值落在一个字段里。
    [escaped] = codec.parse_cq_string("[CQ:image,file=a&#44;b.png]")
    assert escaped["data"] == {"file": "a,b.png"}


def test_cq_string_without_markers_is_one_text_segment() -> None:
    assert codec.parse_cq_string("普通消息") == [{"type": "text", "data": {"text": "普通消息"}}]
    assert codec.parse_cq_string("") == []


def test_cq_rendering_is_reversible_for_face_and_text() -> None:
    parts = [{"kind": "text", "text": "hi"}, {"kind": "emotion", "emotion": {"id": "14"}}]
    assert codec.render_cq_string(parts) == "hi[CQ:face,id=14]"


# ------------------------------------------------------------- 未知段透传


def test_unknown_segment_is_passed_through_not_dropped(proto: OneBotV11Protocol) -> None:
    """v11 扩展段（poke/mface/markdown…）不许被静默丢弃。

    丢掉会让上游以为整条消息发全了；保留原始 type 与 data，至少能看见
    “有东西来了但我不解释它”。
    """
    original = {"type": "poke", "data": {"type": "1", "id": "12345"}}
    [part] = proto.decode_message([original])
    assert part == {"kind": "unknown", "type": "poke", "data": {"type": "1", "id": "12345"}}
    assert proto.encode_message([part]) == [original]


def test_unknown_cq_type_keeps_its_original_name() -> None:
    # 统一成 "unknown" 会让再渲染回去变成另一段，扩展段就失真了。
    [segment] = codec.parse_cq_string("[CQ:poke,id=7]")
    assert segment == {"type": "poke", "data": {"id": "7"}}
    assert codec.render_cq_string(codec.segments_to_parts([segment])) == "[CQ:poke,id=7]"


def test_mixed_known_and_unknown_segments_keep_order(proto: OneBotV11Protocol) -> None:
    segments = [
        {"type": "reply", "data": {"id": "9001"}},
        {"type": "text", "data": {"text": "收到"}},
        {"type": "mface", "data": {"emoji_id": "x", "summary": "[表情]"}},
    ]
    parts = proto.decode_message(segments)
    assert [part["kind"] for part in parts] == ["reply", "text", "unknown"]
    assert proto.encode_message(parts) == segments


# ------------------------------------------------------------ 其它部件种类


def test_at_and_media_parts_keep_their_fields(proto: OneBotV11Protocol) -> None:
    parts = [
        {"kind": "at", "qq": "all"},
        {"kind": "image", "file": "abc.image", "url": "https://x/y.png", "name": "y.png"},
    ]
    segments = proto.encode_message(parts)
    assert segments == [
        {"type": "at", "data": {"qq": "all"}},
        {"type": "image", "data": {"file": "abc.image", "url": "https://x/y.png", "name": "y.png"}},
    ]
    assert proto.decode_message(segments) == parts


def test_at_segment_decodes_with_the_mention_target(proto: OneBotV11Protocol) -> None:
    [part] = proto.decode_message([{"type": "at", "data": {"qq": "10001"}}])
    assert part == {"kind": "at", "qq": "10001"}


def test_malformed_parts_are_rejected_rather_than_mangled() -> None:
    with pytest.raises(PartError):
        codec.parts_to_segments([{"kind": "text"}])  # 没有 text 字段
    with pytest.raises(PartError):
        codec.parts_to_segments([{"text": "hello"}])  # 没有 kind
    with pytest.raises(PartError):
        codec.parts_to_segments(["不是对象"])


# ---------------------------------------------------------------- 动作转换


def test_encode_action_wraps_the_request_envelope(proto: OneBotV11Protocol) -> None:
    request = proto.encode_action("get_login_info", {"echo": "e1"})
    assert request == {"action": "get_login_info", "params": {"echo": "e1"}}


def test_encode_action_converts_internal_parts_in_message(proto: OneBotV11Protocol) -> None:
    # 调用方手里拿的是内部部件，转成协议段的责任在这一层，不该外泄。
    request = proto.encode_action(
        "send_group_msg",
        {"group_id": 123, "message": [{"kind": "emotion", "emotion": {"id": "14"}}]},
    )
    assert request["params"]["message"] == [{"type": "face", "data": {"id": "14"}}]


def test_encode_action_leaves_cq_strings_and_segments_alone(proto: OneBotV11Protocol) -> None:
    cq = proto.encode_action("send_group_msg", {"group_id": 1, "message": "[CQ:face,id=14]"})
    assert cq["params"]["message"] == "[CQ:face,id=14]"
    segments = [{"type": "text", "data": {"text": "hi"}}]
    assert proto.encode_action("send_group_msg", {"message": segments})["params"]["message"] == segments


def test_encode_action_requires_a_name(proto: OneBotV11Protocol) -> None:
    with pytest.raises(ValueError):
        proto.encode_action("", {})


class _RecordingDispatcher:
    """记录调用参数的假 registry：验证薄封装只转格式，不复制动作逻辑。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def dispatch(self, name: str, params: dict | None, ctx: object) -> dict:
        self.calls.append((name, dict(params or {})))
        return {"status": "ok", "retcode": 0, "data": {"message_id": 1}}


async def test_send_message_routes_to_the_existing_action() -> None:
    dispatcher = _RecordingDispatcher()
    proto = OneBotV11Protocol(dispatcher=dispatcher, ctx=object())
    result = await proto.send_message({"type": "group", "id": 123456}, [{"kind": "text", "text": "hi"}])
    assert result["data"]["message_id"] == 1
    name, params = dispatcher.calls[0]
    assert name == "send_group_msg"
    assert params == {"group_id": 123456, "message": [{"type": "text", "data": {"text": "hi"}}]}


async def test_send_message_uses_the_private_action_for_private_chats() -> None:
    dispatcher = _RecordingDispatcher()
    proto = OneBotV11Protocol(dispatcher=dispatcher, ctx=object())
    await proto.send_message({"type": "private", "id": 777}, [{"kind": "text", "text": "hi"}])
    name, params = dispatcher.calls[0]
    assert name == "send_private_msg"
    assert params["user_id"] == 777


async def test_send_message_without_a_dispatcher_fails_loudly() -> None:
    # 明确报错好过悄悄不发：适配器只做转换时不该假装自己能发送。
    proto = OneBotV11Protocol()
    with pytest.raises(RuntimeError):
        await proto.send_message({"type": "group", "id": 1}, [{"kind": "text", "text": "hi"}])


async def test_send_message_rejects_an_unknown_chat_type() -> None:
    proto = OneBotV11Protocol(dispatcher=_RecordingDispatcher(), ctx=object())
    with pytest.raises(ValueError):
        await proto.send_message({"type": "channel", "id": 1}, [{"kind": "text", "text": "hi"}])


def test_from_runtime_reuses_the_existing_registry() -> None:
    """适配器必须复用已有 registry，而不是自带一套动作实现。"""
    dispatcher = _RecordingDispatcher()
    runtime = type("FakeRuntime", (), {"registry": dispatcher, "context": lambda self: {"ctx": 1}})()
    proto = from_runtime(runtime)
    assert proto.name == "onebotv11"
    assert proto._dispatcher is dispatcher


def test_from_runtime_rejects_an_object_without_a_registry() -> None:
    with pytest.raises(TypeError):
        from_runtime(object())

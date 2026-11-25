"""Milky 适配器：段编解码、事件转换、调用翻译、应答信封。

这份适配器的价值全在"复用"上——动作逻辑一行没写，靠的是把 Milky 的 65 个 API 名翻译成内部
已有的动作名。所以测试的重点是翻译的**正确性**与**诚实性**：

* 能执行的 API 必须翻到真实存在的内部动作；
* 不能执行的必须明确失败，不能翻过去让客户端以为成功了；
* 事件转不出来时抛异常（分流点会跳过这条连接），不能返回半个事件。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from qsick.protocol_bridge import load_protocol  # noqa: E402


@pytest.fixture(scope="module")
def milky():
    return load_protocol("milky")


class TestRegistration:
    def test_it_is_in_the_manifest(self):
        from qsick.protocol_bridge import protocol_names

        assert "milky" in protocol_names()

    def test_it_loads_and_declares_capabilities(self, milky):
        assert milky.name == "milky"
        caps = milky.capabilities()
        assert caps["protocol"] == "milky"
        assert caps["apis_supported"] > 0

    def test_the_store_protocol_is_still_not_a_chat_protocol(self):
        from qsick.protocol_bridge import protocol_names

        assert "maxStore" not in protocol_names()


class TestSegments:
    @pytest.mark.parametrize("segment,kind", [
        ({"type": "text", "text": "hi"}, "text"),
        ({"type": "mention", "user_id": 123}, "at"),
        ({"type": "mention_all"}, "at"),
        ({"type": "face", "face_id": 14}, "emotion"),
        ({"type": "reply", "message_id": 99}, "reply"),
        ({"type": "image", "resource_id": "f1"}, "image"),
        ({"type": "record", "resource_id": "f2"}, "record"),
        ({"type": "video", "resource_id": "f3"}, "video"),
        ({"type": "file", "resource_id": "f4"}, "file"),
    ])
    def test_every_incoming_tag_maps_to_a_known_kind(self, milky, segment, kind):
        parts = milky.decode_message([segment])
        assert parts[0]["kind"] == kind

    def test_mention_all_becomes_the_at_everyone_marker(self, milky):
        parts = milky.decode_message([{"type": "mention_all"}])
        assert parts[0]["qq"] == "all"

    def test_an_unknown_tag_is_preserved_not_dropped(self, milky):
        """丢弃会让上层以为消息完整了；透传至少让它看见"有东西我不认"。"""
        parts = milky.decode_message([{"type": "brand_new_thing", "x": 1}])
        assert parts[0]["kind"] == "unknown"
        assert parts[0]["type"] == "brand_new_thing"
        assert parts[0]["data"]["x"] == 1

    def test_a_malformed_segment_does_not_kill_the_message(self, milky):
        parts = milky.decode_message([{"type": "text", "text": "ok"}, "not-a-dict"])
        assert parts[0]["kind"] == "text"
        assert parts[1]["kind"] == "unknown"

    def test_a_64_bit_reply_id_survives(self, milky):
        # 超出 2^53 的消息 id 转成数字会被静默改值，接收端就会引用一个不存在的消息。
        big = 771069947330992833
        parts = milky.decode_message([{"type": "reply", "message_id": big}])
        assert parts[0]["id"] == str(big)
        back = milky.encode_message(parts)
        assert str(back[0]["message_id"]) == str(big)

    def test_text_round_trips(self, milky):
        segments = milky.encode_message([{"kind": "text", "text": "你好"}])
        assert segments == [{"type": "text", "text": "你好"}]


class TestActions:
    def test_a_supported_api_translates_to_the_internal_name(self, milky):
        name, params = milky.decode_action("send_group_message", {"group_id": 1, "message": []})
        assert name == "send_group_msg"
        assert params["group_id"] == 1

    def test_the_message_segments_are_converted_to_internal_parts(self, milky):
        _, params = milky.decode_action(
            "send_group_message", {"group_id": 1, "message": [{"type": "text", "text": "hi"}]})
        assert params["message"] == [{"kind": "text", "text": "hi"}]

    def test_an_unimplemented_api_fails_loudly(self, milky):
        """翻过去会让客户端收到成功、却什么也没发生——比明确的失败更坏。"""
        from protocols.milky.actions import UnknownAction

        with pytest.raises(UnknownAction) as caught:
            milky.decode_action("get_peer_pins", {})
        # 失败信息要说清是"QQ 侧没有接口"，不是"协议层不认识这个 API"。
        assert "置顶" in str(caught.value)

    def test_a_parameter_model_mismatch_says_so(self, milky):
        """Milky 用 notification_seq，内部用 OneBot 的 flag——两者没有换算关系。

        这一类与"根本没有接口"是两种缺口，失败信息要能区分，否则没法判断下一步做什么。
        """
        from protocols.milky.actions import UnknownAction

        with pytest.raises(UnknownAction) as caught:
            milky.decode_action("accept_group_request", {"group_id": 1, "notification_seq": 5})
        assert "flag" in str(caught.value)

    def test_an_api_outside_the_spec_is_refused(self, milky):
        from protocols.milky.actions import UnknownAction

        with pytest.raises(UnknownAction):
            milky.decode_action("definitely_not_an_api", {})

    def test_the_target_action_can_depend_on_the_parameters(self, milky):
        """Milky 用一个 API 覆盖三种会话，内部按会话分成两个动作。"""
        group, group_params = milky.decode_action(
            "get_history_messages", {"message_scene": "group", "peer_id": 7, "limit": 30})
        friend, friend_params = milky.decode_action(
            "get_history_messages", {"message_scene": "friend", "peer_id": 9, "limit": 10})
        assert group == "get_group_msg_history"
        assert group_params["group_id"] == 7
        assert group_params["count"] == 30
        assert friend == "get_friend_msg_history"
        assert friend_params["user_id"] == 9

    def test_parameters_are_renamed_where_the_vocabularies_differ(self, milky):
        _, params = milky.decode_action("upload_group_file", {
            "group_id": 1, "file_uri": "/a/b.zip", "file_name": "b.zip", "parent_folder_id": "/"})
        assert params["file"] == "/a/b.zip"
        assert params["name"] == "b.zip"
        assert params["folder_id"] == "/"

    def test_every_supported_api_reaches_a_real_action_in_this_repository(self):
        """翻译表里不能有指向不存在动作的条目。

        这一条是这张表唯一的硬约束：表写错的结果是运行时的 `unknown action`，而那看起来像
        内部坏了，不像协议层写错。
        """
        from protocols.milky.actions import DYNAMIC, SUPPORTED

        known = _registered_action_names()
        wanted = set(SUPPORTED.values()) | set(_dynamic_targets())
        missing = sorted(name for name in wanted if name not in known)
        assert missing == [], f"these actions do not exist: {missing}"

    def test_the_same_api_is_never_mapped_twice(self):
        from protocols.milky.actions import (
            DYNAMIC, LOCAL, NEEDS_TRANSLATION, NO_ACTION, SUPPORTED, UNSUPPORTED_BY_DESIGN,
        )

        groups = [set(SUPPORTED), set(DYNAMIC), set(NEEDS_TRANSLATION), set(NO_ACTION),
                  set(LOCAL), set(UNSUPPORTED_BY_DESIGN)]
        total = sum(len(group) for group in groups)
        union: set[str] = set()
        for group in groups:
            assert not (union & group), f"overlapping entries: {sorted(union & group)}"
            union |= group
        # 规范是 65 个 API；每一类都必须被归进去，一个不漏也不多。
        assert total == len(union) == 65, f"covered {len(union)} of 65"

    def test_the_design_level_refusal_is_a_separate_category(self):
        """`UNSUPPORTED_BY_DESIGN` 与 `NO_ACTION` 分开记，因为对调用方要说的话不同。

        前者是"本程序不提供这个能力"，后者是"QQ 侧没有这个接口"。混在一起，失败信息就只能说
        "不支持"，而那没法让人判断该不该再等。
        """
        from protocols.milky.actions import NO_ACTION, UNSUPPORTED_BY_DESIGN

        assert set(UNSUPPORTED_BY_DESIGN) == {"get_peer_pins", "set_peer_pin"}
        assert not (set(NO_ACTION) & set(UNSUPPORTED_BY_DESIGN))


class TestEvents:
    def test_a_group_message_becomes_message_receive(self, milky):
        event = milky.encode_event({
            "post_type": "message", "message_type": "group", "time": 1795000000,
            "self_id": "3905548034", "group_id": 2169010946, "user_id": 100,
            "message_seq": 7, "message_id": 99,
            "sender": {"user_id": 100, "nickname": "A", "card": "", "role": "member"},
            "message": [{"type": "text", "data": {"text": "hi"}}],
        })
        assert event["event_type"] == "message_receive"
        assert event["data"]["message_scene"] == "group"
        assert event["data"]["segments"] == [{"type": "text", "text": "hi"}]

    def test_a_private_message_reports_the_friend(self, milky):
        event = milky.encode_event({
            "post_type": "message", "message_type": "private", "time": 1, "self_id": "1",
            "target_id": 555, "user_id": 555, "message_seq": 1, "message_id": 2,
            "sender": {"user_id": 555, "nickname": "B"}, "message": [],
        })
        assert event["data"]["message_scene"] == "friend"
        assert event["data"]["friend"]["user_id"] == 555
        assert event["data"]["group"] is None

    def test_a_recall_becomes_message_recall(self, milky):
        event = milky.encode_event({
            "post_type": "notice", "notice_type": "group_recall", "time": 1, "self_id": "1",
            "group_id": 7, "message_id": 9, "operator_id": 100,
        })
        assert event["event_type"] == "message_recall"

    def test_a_disallowed_event_raises_rather_than_returning_half_an_event(self, milky):
        """分流点靠异常来决定"跳过这条连接"，返回半个事件会让对端收到坏数据。"""
        with pytest.raises(Exception):
            milky.encode_event({"post_type": "notice", "notice_type": "group_card"})

    def test_a_heartbeat_with_online_status_is_not_an_event(self, milky):
        with pytest.raises(Exception):
            milky.encode_event({
                "post_type": "meta_event", "meta_event_type": "heartbeat",
                "time": 1, "self_id": "1", "status": {"online": True},
            })

    def test_a_heartbeat_reporting_offline_becomes_bot_offline(self, milky):
        event = milky.encode_event({
            "post_type": "meta_event", "meta_event_type": "heartbeat",
            "time": 1, "self_id": "1", "status": {"online": False},
        })
        assert event["event_type"] == "bot_offline"


class TestReplyEnvelope:
    def test_success_carries_retcode_zero(self, milky):
        out = milky.format_reply({"status": "ok", "retcode": 0, "data": {"x": 1}})
        assert out["status"] == "ok"
        assert out["retcode"] == 0
        assert out["data"] == {"x": 1}

    def test_a_success_without_data_gets_an_empty_object(self, milky):
        # Milky 要求无输出的成功返回 `data: {}`，而不是 `null`。
        out = milky.format_reply({"status": "ok", "retcode": 0, "data": None})
        assert out["data"] == {}

    def test_a_bad_request_maps_to_minus_400(self, milky):
        out = milky.format_reply({"status": "failed", "retcode": 1400, "message": "bad"})
        assert out["retcode"] == -400
        assert out["status"] == "failed"

    def test_not_logged_in_maps_to_minus_403(self, milky):
        out = milky.format_reply({"status": "failed", "retcode": 1403})
        assert out["retcode"] == -403

    def test_an_unmapped_internal_code_does_not_leak_through(self, milky):
        """内部 retcode 是 OneBot 的取值范围，直接给出去对 Milky 客户端毫无意义。"""
        out = milky.format_reply({"status": "failed", "retcode": 1500})
        assert out["retcode"] == -500

    def test_a_failure_always_carries_a_message(self, milky):
        out = milky.format_reply({"status": "failed", "retcode": 1400})
        assert out["message"]


def _registered_action_names() -> set[str]:
    """The action names this repository actually registers.

    Read from the catalogue rather than by booting a runtime: building a runtime opens databases and starts
    transports, and a test that only needs a list of names should not do that.
    """
    import json

    catalog = ROOT / "src" / "qsick" / "onebot" / "actions" / "actions.json"
    data = json.loads(catalog.read_text(encoding="utf-8"))
    entries = data.get("actions") if isinstance(data, dict) else data
    names = {str(item.get("name") or "") for item in entries if isinstance(item, dict)}
    # 别名也算登记过：注册表把它们指向同一个处理函数。
    for item in entries:
        if isinstance(item, dict):
            alias = item.get("alias_of")
            if alias:
                names.add(str(alias))
    return {name for name in names if name}


def _dynamic_targets() -> set[str]:
    """The action names the parameter-driven resolvers can return.

    The resolvers decide at call time, so the only way to check them against the catalogue is to ask them with
    the shapes that select each branch.
    """
    from protocols.milky.actions import DYNAMIC

    probes = {
        "get_history_messages": [
            {"message_scene": "group", "peer_id": 1},
            {"message_scene": "friend", "peer_id": 1},
        ],
        "mark_message_as_read": [{"peer_id": 1, "message_seq": 2}],
        "get_resource_temp_url": [{"group_id": 1, "resource_id": "x"}, {"user_id": 1, "resource_id": "x"}],
    }
    targets: set[str] = set()
    for api, resolver in DYNAMIC.items():
        for params in probes.get(api, [{}]):
            name, _ = resolver(dict(params))
            targets.add(name)
    return targets

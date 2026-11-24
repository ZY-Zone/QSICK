"""OLPush 包推送：解码与事件映射。

这一层此前完全不存在，所以测试同时承担两个作用：证明解码正确，以及**记录依据**——每个字段号
都来自两份独立实现的交叉验证（NapCat 的 `packet/transformer/proto/message/*.ts` 与 Lagrange 的
`Internal/Packets/Notify/*.cs`）。测试里的合成帧是按这些字段号编出来的，所以一条断言失败通常
意味着"字段号被改错了"，而那正是最难靠肉眼发现的一类错误。

真正没被证明的是：QQ 发过来的帧是否就是这个形状。那需要真实抓包，见 docs/headless-events.md。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from qsick import olpush  # noqa: E402


# --------------------------------------------------------------- 编码小工具
#
# 手写而不是用 protobuf 库：测试要证明的是"按这些字段号编出来的帧能被解开"，自己编码才让字段号
# 在测试里显式可见。用库会把字段号藏进 schema，测试就不再是那份依据的复述了。

def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def _field(number: int, value) -> bytes:
    key = _varint((number << 3) | (0 if isinstance(value, int) else 2))
    if isinstance(value, int):
        return key + _varint(value)
    return key + _varint(len(value)) + value


def _message(content_head: bytes, body: bytes) -> bytes:
    """一帧 `PushMsg`：`{1: {2: contentHead, 3: {2: msgContent}}}`。"""
    return _field(1, _field(2, content_head) + _field(3, _field(2, body)))


def _head(type_: int, sub: int = 0) -> bytes:
    out = _field(1, type_)
    return out + (_field(2, sub) if sub else b"")


def _wrap_0x2dc(inner: bytes) -> bytes:
    """732 的额外装载：4 字节群号 + 1 字节 + 2 字节大端长度 + 正文。"""
    return (7).to_bytes(4, "big") + b"\x00" + len(inner).to_bytes(2, "big") + inner


def _mapper(now: int = 1) -> olpush.PushEventMapper:
    return olpush.PushEventMapper(
        {"u_admin": "200", "u_member": "100", "u_me": "999", "u_other": "300"}.get,
        self_uin="999", now=lambda: now,
    )


class TestFraming:
    def test_the_envelope_yields_type_subtype_and_body(self):
        frame = olpush.decode_push(_message(_head(33), _field(1, 7)))
        assert frame.type == 33
        assert frame.body == _field(1, 7)

    def test_a_truncated_varint_is_a_decode_error(self):
        with pytest.raises(olpush.DecodeError):
            olpush.decode_fields(b"\x80")

    def test_a_truncated_length_delimited_field_is_a_decode_error(self):
        with pytest.raises(olpush.DecodeError):
            olpush.decode_fields(b"\x0a\x05abc")

    def test_an_unknown_wire_type_is_refused(self):
        # wire type 3 (start group) is deprecated in proto3 and must not be guessed at.
        with pytest.raises(olpush.DecodeError):
            olpush.decode_fields(b"\x0b")


class TestMemberIncrease:
    def test_type_33_becomes_a_group_increase(self):
        change = (_field(1, 2169010946) + _field(3, b"u_member")
                  + _field(4, 130) + _field(5, b"u_admin"))
        events = _mapper().map_bytes(_message(_head(33), change))
        assert len(events) == 1
        event = events[0]
        assert event["post_type"] == "notice"
        assert event["notice_type"] == "group_increase"
        assert event["group_id"] == 2169010946
        assert event["user_id"] == 100
        assert event["operator_id"] == 200
        assert event["sub_type"] == "approve"

    def test_131_is_an_invite(self):
        change = _field(1, 7) + _field(3, b"u_member") + _field(4, 131) + _field(5, b"u_admin")
        events = _mapper().map_bytes(_message(_head(33), change))
        assert events[0]["sub_type"] == "invite"

    def test_an_unresolvable_member_produces_nothing(self):
        """解析不出号就不发：`user_id` 写 0 等于造一个不存在的用户。"""
        change = _field(1, 7) + _field(3, b"u_unknown") + _field(4, 130)
        assert _mapper().map_bytes(_message(_head(33), change)) == []


class TestMemberDecrease:
    def test_a_voluntary_leave(self):
        change = _field(1, 7) + _field(3, b"u_member") + _field(4, 130)
        events = _mapper().map_bytes(_message(_head(34), change))
        assert events[0]["sub_type"] == "leave"

    def test_a_kick(self):
        change = _field(1, 7) + _field(3, b"u_member") + _field(4, 131) + _field(5, b"u_admin")
        events = _mapper().map_bytes(_message(_head(34), change))
        assert events[0]["sub_type"] == "kick"
        assert events[0]["operator_id"] == 200

    def test_kick_self_is_distinguishable(self):
        change = _field(1, 7) + _field(3, b"u_me") + _field(4, 3)
        events = _mapper().map_bytes(_message(_head(34), change))
        assert events[0]["sub_type"] == "kick_me"

    def test_the_operator_can_be_an_embedded_message(self):
        """`operatorInfo` 在被踢时是内嵌的 protobuf，不是字符串。

        照字符串读会得到一串乱码，然后被当成 uid 解成 0 —— 一个静默的错账号。
        """
        embedded = _field(1, _field(1, b"u_admin"))
        change = _field(1, 7) + _field(3, b"u_member") + _field(4, 3) + _field(5, embedded)
        events = _mapper().map_bytes(_message(_head(34), change))
        assert events[0]["operator_id"] == 200

    def test_a_plain_string_operator_still_works(self):
        change = _field(1, 7) + _field(3, b"u_member") + _field(4, 131) + _field(5, b"u_admin")
        events = _mapper().map_bytes(_message(_head(34), change))
        assert events[0]["operator_id"] == 200


class TestOperatorDecoding:
    def test_a_protobuf_operator_is_read_as_protobuf(self):
        assert olpush.decode_operator(_field(1, _field(1, b"u_admin"))) == "u_admin"

    def test_a_plain_uid_is_read_as_a_string(self):
        assert olpush.decode_operator(b"u_admin") == "u_admin"

    def test_garbage_yields_nothing_rather_than_a_wrong_uid(self):
        assert olpush.decode_operator(b"\xff\xfe\x00bad") == ""

    def test_an_empty_field_yields_nothing(self):
        assert olpush.decode_operator(b"") == ""


class TestGroupAdmin:
    def test_extra_enable_means_promoted(self):
        body = _field(1, 7) + _field(4, _field(2, _field(1, b"u_member")))
        events = _mapper().map_bytes(_message(_head(44), body))
        assert events[0]["notice_type"] == "group_admin"
        assert events[0]["sub_type"] == "set"
        assert events[0]["user_id"] == 100

    def test_extra_disable_means_demoted(self):
        body = _field(1, 7) + _field(4, _field(1, _field(1, b"u_member")))
        events = _mapper().map_bytes(_message(_head(44), body))
        assert events[0]["sub_type"] == "unset"


class TestRequests:
    def test_a_group_join_request(self):
        body = _field(1, 7) + _field(2, 1) + _field(3, b"u_member") + _field(5, b"let me in")
        events = _mapper().map_bytes(_message(_head(84), body))
        assert events[0]["post_type"] == "request"
        assert events[0]["request_type"] == "group"
        assert events[0]["group_id"] == 7
        assert events[0]["user_id"] == 100
        assert events[0]["comment"] == "let me in"

    def test_a_friend_request(self):
        info = _field(2, b"u_member") + _field(10, b"hi") + _field(11, b"search")
        events = _mapper().map_bytes(_message(_head(528, sub=35), _field(1, info)))
        assert events[0]["request_type"] == "friend"
        assert events[0]["user_id"] == 100
        assert events[0]["comment"] == "hi"
        assert events[0]["via"] == "search"

    def test_a_group_invitation_through_0x20d(self):
        """`Event0x20D` 是 `{1: subType, 2: body}`，只有 subType 87 是群邀请。"""
        invite = _field(1, 7) + _field(5, b"u_admin")
        events = _mapper().map_bytes(_message(_head(525), _field(1, 87) + _field(2, invite)))
        assert events[0]["request_type"] == "group"
        assert events[0]["sub_type"] == "invite"

    def test_an_0x20d_with_another_subtype_is_ignored(self):
        invite = _field(1, 7) + _field(5, b"u_admin")
        assert _mapper().map_bytes(_message(_head(525), _field(1, 12) + _field(2, invite))) == []


class TestGroupNudge:
    def _payload(self, params: dict[str, str]) -> bytes:
        templ = b"".join(_field(7, _field(1, name.encode()) + _field(2, value.encode()))
                         for name, value in params.items())
        inner = _field(13, 19) + _field(4, 7) + _field(26, _field(1, 12) + templ)
        return _wrap_0x2dc(inner)

    def test_a_poke_carries_both_parties_and_the_wording(self):
        events = _mapper().map_bytes(_message(
            _head(732, sub=20),
            self._payload({"uin_str1": "100", "uin_str2": "200",
                           "action_str": "拍了拍", "suffix_str": "的头"})))
        event = events[0]
        assert event["notice_type"] == "notify"
        assert event["sub_type"] == "poke"
        assert event["user_id"] == 100
        assert event["target_id"] == 200
        assert event["display_action"] == "拍了拍"
        assert event["display_suffix"] == "的头"

    def test_the_binary_framing_is_required(self):
        """直接喂内层 protobuf 会被拒：732 的正文前面还有 7 字节装载。

        少了这层校验，一个被截断的帧会解出半个事件，而半年后才有人发现。
        """
        inner = _field(13, 19) + _field(4, 7)
        assert _mapper().map_bytes(_message(_head(732, sub=20), inner)) == []

    def test_a_wrong_inner_length_is_refused(self):
        inner = _field(13, 19) + _field(4, 7)
        broken = (7).to_bytes(4, "big") + b"\x00" + (999).to_bytes(2, "big") + inner
        assert _mapper().map_bytes(_message(_head(732, sub=20), broken)) == []


class TestGroupReaction:
    def _payload(self) -> bytes:
        reaction = _field(1, _field(1, _field(2, _field(1, 555))
                                    + _field(3, _field(1, b"14") + _field(3, 2)
                                             + _field(4, b"u_member") + _field(5, 1))))
        return _wrap_0x2dc(_field(13, 35) + _field(4, 7) + _field(44, reaction))

    def test_a_reaction_carries_the_seq_and_the_face(self):
        events = _mapper().map_bytes(_message(_head(732, sub=16), self._payload()))
        event = events[0]
        assert event["notice_type"] == "group_msg_emoji_like"
        assert event["message_seq"] == 555
        assert event["user_id"] == 100
        assert event["likes"] == [{"emoji_id": "14", "count": 2}]
        assert event["is_add"] is True

    def test_reaction_type_two_is_a_removal(self):
        reaction = _field(1, _field(1, _field(2, _field(1, 555))
                                    + _field(3, _field(1, b"14") + _field(4, b"u_member") + _field(5, 2))))
        wrapped = _wrap_0x2dc(_field(13, 35) + _field(4, 7) + _field(44, reaction))
        events = _mapper().map_bytes(_message(_head(732, sub=16), wrapped))
        assert events[0]["is_add"] is False


class TestWhatIsNotDuplicated:
    def test_a_group_recall_push_is_skipped(self):
        """撤回已经有灰条路径在发，包推送再来一次会让客户端收到两遍。"""
        wrapped = _wrap_0x2dc(_field(13, 17) + _field(4, 7))
        assert _mapper().map_bytes(_message(_head(732, sub=17), wrapped)) == []

    def test_a_friend_recall_push_is_skipped(self):
        assert _mapper().map_bytes(_message(_head(528, sub=138), _field(1, b""))) == []

    def test_an_unknown_type_produces_nothing(self):
        assert _mapper().map_bytes(_message(_head(9999), _field(1, 1))) == []

    def test_a_broken_frame_produces_nothing_instead_of_raising(self):
        assert _mapper().map_bytes(b"\xff\xff\xff") == []

    def test_a_group_message_push_produces_nothing_here(self):
        """82 是正常的群消息，走的是消息通道（灰条/`onRecvMsg`），不是这里。"""
        assert _mapper().map_bytes(_message(_head(olpush.TYPE_GROUP_MESSAGE), _field(1, 1))) == []

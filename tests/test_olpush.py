"""OLPush 包推送：解码与事件映射。

这一层此前完全不存在，所以测试同时承担两个作用：证明解码正确，以及**记录依据**——每个字段号
都来自两份独立实现的交叉验证（NapCat 的 `packet/transformer/proto/message/*.ts` 与 Lagrange 的
`Internal/Packets/Notify/*.cs`），以及 2026-11-24 在 9.9.33-52230 上的一次实抓（39 帧）。
测试里的合成帧按这些字段号编出来，所以一条断言失败通常意味着"字段号被改错了"，而那正是最难
靠肉眼发现的一类错误。

`TestCapturedShapes` 的形状来自实抓帧；`TestFeedPacketIdentity` 验证 `feed_packet` 在解码前
先从帧头补热 uid→uin 表（否则 fresh login 时本号入群事件会被丢弃、87 余波会被误报成邀请）。
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


def _frame(response_head: bytes, content_head: bytes, body: bytes) -> bytes:
    """一帧完整 `PushMsg`：`{1: {1: responseHead, 2: contentHead, 3: {2: msgContent}}}`。"""
    return _field(1, _field(1, response_head) + _field(2, content_head)
                  + _field(3, _field(2, body)))


def _response_head(*, from_uin: int = 0, from_uid: bytes = b"",
                   to_uin: int = 0, to_uid: bytes = b"") -> bytes:
    return (_field(1, from_uin) + _field(2, from_uid)
            + _field(5, to_uin) + _field(6, to_uid))


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


class TestFrameIdentity:
    """`frame_identity` 从 `ResponseHead` 里读本号与来源身份（`feed_packet` 用它预热映射表）。"""

    def test_the_response_head_carries_the_self_account(self):
        raw = _frame(
            _response_head(from_uin=3905548034, from_uid=b"u_sender",
                           to_uin=1111111111, to_uid=b"u_me"),
            _head(33),
            _field(1, 7),
        )
        identity = olpush.frame_identity(raw)
        assert identity["self_uin"] == "1111111111"
        assert identity["self_uid"] == "u_me"
        assert identity["from_uin"] == "3905548034"
        assert identity["from_uid"] == "u_sender"

    def test_a_group_id_in_the_from_field_is_not_a_user_uid(self):
        """群事件里 ResponseHead 的 1/2 号是群号，不能当 uid 记进用户映射表。"""
        raw = _frame(
            _response_head(from_uin=1135231274, from_uid=b"1135231274", to_uin=3905548034),
            _head(34),
            _field(1, 1135231274),
        )
        identity = olpush.frame_identity(raw)
        assert identity["self_uin"] == "3905548034"
        assert identity["from_uin"] == "1135231274"
        assert identity["from_uid"] == ""

    def test_a_broken_frame_returns_nothing(self):
        assert olpush.frame_identity(b"\xff\xff\xff") == {}


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
        """`operatorInfo` 是内嵌的 protobuf，不是字符串（实抓里 34 全部如此，含 131 他人被踢）。

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


class TestCapturedShapes:
    """实抓帧的形状。主来源是 2026-11-24 23:37:30–23:42:14 的 39 帧（QQ 9.9.33-52230）；
    少数形状来自 2026-11-22 的上一轮抓包，会单独标注"待复核"。

    号与 uid 已替换，字段号与取值是原样的。
    """

    def test_self_leaving_is_decrease_type_2_with_an_empty_operator(self):
        """上一轮抓包（文件已丢失）里本号自己退群是 4 号字段 = 2、operatorInfo 为空的
        `{2: {1: 0}}`。2026-11-24 的 39 帧没有复现出 2，映射先保留、结论待复核。"""
        change = (_field(1, 7) + _field(2, 1) + _field(3, b"u_me") + _field(4, 2)
                  + _field(5, _field(2, _field(1, 0))))
        events = _mapper().map_bytes(_message(_head(34), change))
        assert events[0]["sub_type"] == "leave"
        assert events[0]["user_id"] == 999
        assert events[0]["operator_id"] == 999

    def test_owner_dissolving_the_group_is_decrease_type_1(self):
        """2026-11-24 实抓：本号作为群主解散群时收到 34 / 4 号字段 = 1，
        memberUid 与 operatorInfo 里都是本号。参考实现都没有 1，会落进默认分支当成 kick。"""
        info = _field(1, _field(1, b"u_me") + _field(2, 6)
                      + _field(3, b"\xb4\xb4\xbd\xa8\xd5\xdf") + _field(4, 0))
        change = (_field(1, 7) + _field(2, 1) + _field(3, b"u_me") + _field(4, 1)
                  + _field(5, info))
        event = _mapper().map_bytes(_message(_head(34), change))[0]
        assert event["notice_type"] == "group_disband"
        assert event["group_id"] == 7
        assert event["operator_id"] == 999
        assert "user_id" not in event

    def test_a_kick_carries_the_operator_in_an_envelope(self):
        # 实抓原帧：5 = {1: {1: 操作人 uid, 2: 6, 3: <6 字节>, 4: 0}}；131 他人被踢也是这种内嵌。
        info = _field(1, _field(1, b"u_admin") + _field(2, 6) + _field(3, b"\xb4\xb4\xbd\xa8\xd5\xdf")
                      + _field(4, 0))
        change = _field(1, 7) + _field(2, 1) + _field(3, b"u_member") + _field(4, 131) + _field(5, info)
        events = _mapper().map_bytes(_message(_head(34), change))
        assert events[0]["sub_type"] == "kick"
        assert events[0]["operator_id"] == 200

    def test_disband_matches_the_graytip_channel(self):
        """129 来自参考实现（NapCat / SnowLuma），应是**非群主成员**收到的解散值；群主自己
        解散时实抓到的值是 1（见上一个用例）。两者都发 `group_disband`。"""
        change = _field(1, 7) + _field(3, b"u_me") + _field(4, 129) + _field(5, _field(1, _field(1, b"u_admin")))
        event = _mapper().map_bytes(_message(_head(34), change))[0]
        assert event["notice_type"] == "group_disband"
        assert event["group_id"] == 7
        assert event["operator_id"] == 200
        assert "user_id" not in event

    def test_type_85_self_join_becomes_group_increase(self):
        """2026-11-24 实抓：本号经管理员批准 / 接受邀请入群发 85，33 不发。
        原帧：{1 群, 2=1, 3 审批人 uid, 4=0, 5 bytes, 6=0}。"""
        body = (_field(1, 7) + _field(2, 1) + _field(3, b"u_admin")
                + _field(4, 0) + _field(6, 0))
        events = _mapper().map_bytes(_message(_head(olpush.TYPE_GROUP_SELF_JOINED), body))
        assert len(events) == 1
        event = events[0]
        assert event["notice_type"] == "group_increase"
        assert event["group_id"] == 7
        assert event["user_id"] == 999
        assert event["operator_id"] == 200
        assert event["sub_type"] == "approve"

    def test_type_85_also_records_the_self_join_for_echo_dedup(self):
        joins: dict[int, float] = {}
        clock = [100]

        def mapper():
            return olpush.PushEventMapper(
                {"u_admin": "200", "u_me": "999"}.get, self_uin="999",
                now=lambda: clock[0], self_joins=joins)

        join = (_field(1, 7) + _field(2, 1) + _field(3, b"u_admin")
                + _field(4, 0) + _field(6, 0))
        assert mapper().map_bytes(_message(_head(85), join))
        invite = _field(1, 7) + _field(2, 1) + _field(3, 4) + _field(4, 0) + _field(5, b"u_admin")
        clock[0] = 101
        assert mapper().map_bytes(_message(_head(87), invite)) == []

    def test_the_invite_echo_after_being_pulled_in_is_dropped(self):
        """群主直接拉本号进群：先到 33，2026-11-24 实抓里 266 ms 后到同群的 87。那条 87 不是
        待审批的邀请。"""
        joins: dict[int, float] = {}
        clock = [100]

        def mapper():
            return olpush.PushEventMapper(
                {"u_admin": "200", "u_me": "999"}.get, self_uin="999",
                now=lambda: clock[0], self_joins=joins)

        join = (_field(1, 7) + _field(2, 1) + _field(3, b"u_me") + _field(4, 131)
                + _field(5, b"u_admin") + _field(6, 6) + _field(8, 0))
        assert mapper().map_bytes(_message(_head(33), join))[0]["notice_type"] == "group_increase"
        invite = _field(1, 7) + _field(2, 1) + _field(3, 4) + _field(4, 0) + _field(5, b"u_admin")
        clock[0] = 101
        assert mapper().map_bytes(_message(_head(87), invite)) == []
        # 窗口外的 87 仍是一条真邀请。
        clock[0] = 100 + int(olpush.SELF_JOIN_INVITE_WINDOW) + 1
        assert mapper().map_bytes(_message(_head(87), invite))[0]["sub_type"] == "invite"

    def test_an_invite_for_a_group_not_just_joined_is_a_request(self):
        invite = _field(1, 7) + _field(3, 4) + _field(5, b"u_admin")
        events = _mapper().map_bytes(_message(_head(87), invite))
        assert events[0]["request_type"] == "group"
        assert events[0]["user_id"] == 200

    def test_a_join_request_matches_the_captured_frame(self):
        """2026-11-24 实抓：{1 群, 2=1, 3 申请人 uid, 4=30（未命名）, 5 附言, 6, 8}。
        `decode_group_join` 按 Lagrange 的字段表把 2 号读作 `join_type`（实抓值 1），4 号不解。"""
        body = (_field(1, 7) + _field(2, 1) + _field(3, b"u_member") + _field(4, 30)
                + _field(5, "这是一条测试申请原因".encode()) + _field(6, 0) + _field(8, 0))
        assert olpush.decode_group_join(body).join_type == 1
        events = _mapper().map_bytes(_message(_head(84), body))
        assert events[0]["comment"] == "这是一条测试申请原因"
        assert events[0]["user_id"] == 100

    def test_admin_frames_carry_the_direction_in_field_3_too(self):
        # 原帧：{1: 群, 2: 1, 3: 1/0, 4: {2|1: {1: uid, 2: 1/0}}}；以 4 内的分支为准。
        on = _field(1, 7) + _field(2, 1) + _field(3, 1) + _field(4, _field(2, _field(1, b"u_member") + _field(2, 1)))
        off = _field(1, 7) + _field(2, 1) + _field(3, 0) + _field(4, _field(1, _field(1, b"u_member") + _field(2, 0)))
        assert _mapper().map_bytes(_message(_head(44), on))[0]["sub_type"] == "set"
        assert _mapper().map_bytes(_message(_head(44), off))[0]["sub_type"] == "unset"


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


class TestFeedPacketIdentity:
    """`Runtime.feed_packet` 会先用帧头把 uid→uin 表补热，再解码。"""

    async def test_the_frame_head_warms_the_uid_table_before_decoding(self, runtime):
        """fresh login：本号 uid 还没进映射表时，33 帧头先补上这一条，
        33 才能产出事件、紧随的 87 余波才会被丢弃（否则会被误报成入群邀请）。"""
        self_uid = "u_uYo0gD1XgZ_8UNqhaw3V0g"
        head = _response_head(from_uin=1135231274, from_uid=b"1135231274",
                              to_uin=3905548034, to_uid=self_uid.encode())
        join = (_field(1, 7) + _field(2, 1) + _field(3, self_uid.encode())
                + _field(4, 131) + _field(5, b"u_admin"))
        assert await runtime.feed_packet(_frame(head, _head(33), join), account="3905548034") == 1
        assert runtime._resolve_uid(self_uid) == "3905548034"
        assert 7 in runtime._olpush_self_joins["3905548034"]

        invite = _field(1, 7) + _field(2, 1) + _field(3, 4) + _field(4, 0) + _field(5, b"u_admin")
        assert await runtime.feed_packet(_frame(head, _head(87), invite), account="3905548034") == 0

    async def test_a_joiner_uid_from_a_message_frame_is_learned_too(self, runtime):
        """消息帧（82）不走解码器，但它的 ResponseHead 带发送者 uid/uin —— 顺手学掉，
        后面别人入群/退群的事件才解得出来。"""
        raw = _frame(
            _response_head(from_uin=1111111111, from_uid=b"u_sender",
                           to_uin=3905548034, to_uid=b"u_me"),
            _head(olpush.TYPE_GROUP_MESSAGE),
            _field(1, b""),
        )
        await runtime.feed_packet(raw, account="3905548034")
        assert runtime._resolve_uid("u_sender") == "1111111111"

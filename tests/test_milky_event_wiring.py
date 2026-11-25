"""Pure event wiring tests: synthetic packets only, no runtime or QQ startup."""

import pytest

from protocols.milky.events import convert
from qsick.olpush import Frame, PushEventMapper


def _varint(value):
    out = bytearray()
    while value > 127:
        out.append((value & 127) | 128)
        value >>= 7
    out.append(value)
    return bytes(out)


def _pb(**fields):
    out = b""
    for key, value in fields.items():
        number = int(key.removeprefix("f"))
        if isinstance(value, int):
            out += _varint(number << 3) + _varint(value)
        else:
            value = value.encode() if isinstance(value, str) else value
            out += _varint((number << 3) | 2) + _varint(len(value)) + value
    return out


def _message(**extra):
    return {"post_type": "message", "message_type": "private", "time": 12,
            "self_id": 1, "user_id": 2, "target_id": 2, "message_seq": 7,
            "message": [], **extra}


def test_group_member_is_top_level_and_entity_metadata_survives():
    data = convert(_message(message_type="group", group_id=3, group_name="known",
                            group={"member_count": 20, "remark": "team"},
                            sender={"nickname": "N", "role": "admin", "card": "C"},
                            group_member={"title": "T", "level": 5}))["data"]
    assert data["peer_id"] == 3
    assert "member" not in data["group"]
    assert data["group"]["group_name"] == "known"
    assert data["group"]["member_count"] == 20
    assert data["group"]["remark"] == "team"
    assert data["group_member"] == {
        "user_id": 2, "nickname": "N", "sex": "unknown", "group_id": 3,
        "card": "C", "title": "T", "level": 5, "role": "admin", "join_time": 0,
        "last_sent_time": 0, "shut_up_end_time": None,
    }


def test_friend_entity_keeps_details_and_peer_identity_for_sent_message():
    data = convert(_message(post_type="message_sent", user_id=1,
                            friend={"nickname": "Friend", "sex": "female", "qid": "abc",
                                    "remark": "R", "category": {"category_id": 4, "category_name": "Work"}}))["data"]
    assert data["sender_id"] == 1
    assert data["friend"] == {"user_id": 2, "nickname": "Friend", "sex": "female",
                              "qid": "abc", "remark": "R",
                              "category": {"category_id": 4, "category_name": "Work"}}


@pytest.mark.parametrize("marker", [{"message_scene": "temp"}, {"sub_type": "group"},
                                     {"message_type": "temp"}])
def test_explicit_temp_scene_is_not_misreported_as_friend(marker):
    data = convert(_message(group_id=3, **marker))["data"]
    assert data["message_scene"] == "temp"
    assert data["peer_id"] == 2
    assert data["friend"] is None
    assert data["group"]["group_id"] == 3
    assert "group_member" not in data


def test_temp_group_is_nullable_and_group_id_alone_does_not_infer_temp():
    assert convert(_message(message_scene="temp"))["data"]["group"] is None
    assert convert(_message(group_id=3))["data"]["message_scene"] == "friend"


@pytest.mark.parametrize("sequence", [{"message_seq": 17}, {"msg_seq": "17"},
                                      {"message_seq": 17, "msg_seq": "18"}])
def test_recall_uses_real_sequence_and_distinguishes_sender_operator(sequence):
    data = convert({"post_type": "notice", "notice_type": "group_recall", "group_id": 3,
                    "message_id": "999999999999999999", "user_id": 2,
                    "operator_id": 4, "display_suffix": "suffix", **sequence})["data"]
    assert data == {"message_scene": "group", "peer_id": 3, "message_seq": 17,
                    "sender_id": 2, "operator_id": 4, "display_suffix": "suffix"}


def test_recall_missing_sequence_never_uses_message_id_or_operator_as_sender():
    data = convert({"post_type": "notice", "notice_type": "group_recall", "group_id": 3,
                    "message_id": 123, "operator_id": 4})["data"]
    assert data["message_seq"] == 0  # Explicitly incomplete, not a fabricated locator.
    assert data["sender_id"] == 0
    assert data["operator_id"] == 4


def test_sent_friend_does_not_use_bot_metadata():
    data = convert(_message(post_type="message_sent", user_id=1,
                            sender={"user_id": 1, "nickname": "Bot"}))["data"]
    assert data["friend"]["user_id"] == 2
    assert data["friend"]["nickname"] == ""


def test_unknown_recall_peer_and_operator_are_not_replaced_with_sender():
    data = convert({"post_type": "notice", "notice_type": "friend_recall",
                    "peer_id": 0, "user_id": 1, "operator_id": 0, "msg_seq": "8"})["data"]
    assert data["peer_id"] == 0
    assert data["operator_id"] == 0
    assert data["sender_id"] == 1


def test_temp_recall_keeps_peer_and_explicit_sender():
    data = convert({"post_type": "notice", "notice_type": "friend_recall",
                    "message_scene": "temp", "group_id": 3, "target_id": 2,
                    "sender_id": 1, "operator_id": 1, "message_seq": 8})["data"]
    assert data["message_scene"] == "temp"
    assert data["peer_id"] == 2
    assert data["sender_id"] == 1


def test_friend_request_packet_preserves_initiator_uid():
    mapper = PushEventMapper(lambda uid: {"u_friend": 2}.get(uid), self_uin="1", now=lambda: 12)
    body = _pb(f1=_pb(f1="u_self", f2="u_friend", f10="hello", f11="search"))
    event, = mapper.map(Frame(type=528, sub_type=35, sequence=987, body=body))
    assert event["user_id_uid"] == "u_friend"
    assert "notification_seq" not in event
    assert convert(event)["data"] == {"initiator_id": 2, "initiator_uid": "u_friend",
                                       "comment": "hello", "via": "search"}


@pytest.mark.parametrize("enabled", [True, False])
def test_admin_packet_direction_is_preserved_without_fabricating_operator(enabled):
    mapper = PushEventMapper(lambda uid: 2, self_uin="1", now=lambda: 12)
    extra = _pb(**{("f2" if enabled else "f1"): _pb(f1="u_admin")})
    event, = mapper.map(Frame(type=44, sub_type=0, sequence=55, body=_pb(f1=3, f4=extra)))
    assert "operator_id" not in event
    data = convert(event)["data"]
    assert data["is_set"] is enabled
    assert data["operator_id"] == ""  # Known source gap; do not substitute admin or bot.


@pytest.mark.parametrize("kind,body", [
    (84, _pb(f1=3, f2=1, f3="u_member", f5="join")),
    (87, _pb(f1=3, f5="u_member", f6="u_self")),
])
def test_packet_envelope_sequence_is_not_an_approval_sequence(kind, body):
    mapper = PushEventMapper(lambda uid: 2, self_uin="1", now=lambda: 12)
    event, = mapper.map(Frame(type=kind, sub_type=0, sequence=999, body=body))
    assert "notification_seq" not in event and "invitation_seq" not in event
    data = convert(event)["data"]
    assert data["notification_seq" if kind == 84 else "invitation_seq"] == 0
    assert "target_user_id" not in data


def test_invitation_does_not_borrow_notification_sequence():
    event = {"post_type": "request", "request_type": "group", "sub_type": "invite",
             "group_id": 3, "user_id": 2, "notification_seq": 99}
    assert convert(event)["data"]["invitation_seq"] == 0
    assert convert(event | {"invitation_seq": 88})["data"]["invitation_seq"] == 88

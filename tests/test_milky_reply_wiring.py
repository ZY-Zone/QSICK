"""Offline success-shape regression tests using actual internal action field names."""
from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from protocols.milky.adapter import MilkyProtocol


def reply(api, data):
    return MilkyProtocol().format_api_reply(api, {"status": "ok", "data": data})


def test_impl_info_uses_spec_fields_without_guessing_qq_version():
    data = MilkyProtocol().local_reply("get_impl_info")["data"]
    assert data["milky_version"] == "1.3"
    assert data["qq_protocol_type"] == "windows"
    assert data["qq_protocol_version"] == ""


def test_send_receipt_decimal_strings_are_losslessly_converted():
    result = reply("send_group_message", {"message_seq": "9007199254740993", "time": "1795584803"})
    assert result["status"] == "ok"
    assert result["data"]["message_seq"] == 9007199254740993
    assert result["data"]["time"] == 1795584803


def test_login_and_legacy_call_compatibility():
    result = {"status": "ok", "data": {"user_id": 123, "nickname": "bot"}}
    adapter = MilkyProtocol()
    assert adapter.format_reply(result, "legacy echo")["data"] == result["data"]
    assert adapter.format_reply(result, api="get_login_info")["data"]["uin"] == 123
    assert adapter.format_api_reply("get_login_info", result)["data"]["uin"] == 123


@pytest.mark.parametrize("api,key,entity", [
    ("get_friend_list", "friends", {"user_id": 2, "nickname": "a", "remark": "b"}),
    ("get_group_list", "groups", {"group_id": 3, "group_name": "g"}),
    ("get_group_member_list", "members", {"group_id": 3, "user_id": 2, "nickname": "a", "level": "4"}),
])
def test_entity_lists_wrap_and_preserve_real_fields(api, key, entity):
    entity["extension"] = {"future": True}
    original = copy.deepcopy(entity)
    out = reply(api, [entity])
    assert out["status"] == "ok"
    assert out["data"][key][0]["extension"] == {"future": True}
    assert entity == original
    if key == "members":
        assert out["data"][key][0]["level"] == 4
    if key == "friends":
        assert "category" not in out["data"][key][0]


@pytest.mark.parametrize("api,key,entity", [
    ("get_friend_info", "friend", {"uin": 2, "nickname": "a", "qid": "real"}),
    ("get_group_info", "group", {"group_id": 3, "group_name": "g", "create_time": 100}),
    ("get_group_member_info", "member", {"group_id": 3, "user_id": 2, "nickname": "a", "special_title": "t"}),
])
def test_single_entity_wrappers_are_idempotent(api, key, entity):
    first = reply(api, entity)["data"]
    assert key in first
    assert reply(api, first)["data"] == first


def test_profile_keeps_all_existing_fields():
    profile = {"nickname": "a", "qid": "Q", "personal_note": "bio", "school": "S", "city": "C", "level": 7}
    data = reply("get_user_profile", profile)["data"]
    assert all(data[k] == v for k, v in profile.items())
    assert data["bio"] == "bio"


def test_group_files_follow_measured_normalize_file_entry_names():
    source = {"files": [{"group_id": 3, "file_id": "f", "file_name": "a", "upload_time": 10,
                         "uploader": 2, "download_times": 4, "dead_time": 0, "raw": {"native": True}}],
              "folders": [{"group_id": 3, "file_id": "d", "file_name": "folder", "upload_time": 11,
                           "modify_time": 12, "uploader": 5, "total_file_count": 6}], "future": 1}
    original = copy.deepcopy(source)
    data = reply("get_group_files", source)["data"]
    f, d = data["files"][0], data["folders"][0]
    assert (f["uploaded_time"], f["uploader_id"], f["downloaded_times"], f["expire_time"]) == (10, 2, 4, None)
    assert (d["folder_id"], d["folder_name"], d["created_time"], d["last_modified_time"], d["creator_id"], d["file_count"]) == ("d", "folder", 11, 12, 5, 6)
    assert data["future"] == 1 and f["raw"] == {"native": True}
    assert source == original


def test_url_token_and_custom_faces():
    assert reply("get_group_file_download_url", {"url": "https://example.test"})["data"]["download_url"] == "https://example.test"
    assert reply("get_resource_temp_url", {"url": "https://example.test"})["data"]["url"] == "https://example.test"
    assert reply("get_csrf_token", {"token": 123})["data"]["csrf_token"] == "123"
    assert reply("get_custom_face_url_list", ["u"])["data"] == {"urls": ["u"]}


@pytest.mark.parametrize("api,data", [
    ("get_friend_info", {}), ("get_group_list", None),
    ("create_group_folder", {"folder_name": "not an id"}),
    ("upload_group_file", {"file_id": ""}),
    ("send_group_message", {"message_id": 999}),
    ("send_private_message", {"message_seq": 9, "time": 0}),
])
def test_missing_entities_and_identifiers_are_explicit_failures(api, data):
    out = reply(api, data)
    assert out["status"] == "failed" and out["retcode"] == -500
    assert out["data"] is None and api in out["message"]


def test_send_only_accepts_real_sequence_and_time():
    out = reply("send_group_message", {"message_id": 999, "message_seq": 12, "time": 100})
    assert out["data"]["message_seq"] == 12
    assert out["data"]["time"] == 100


def test_internal_failures_and_no_output_successes():
    out = MilkyProtocol().format_api_reply("get_login_info", {"status": "failed", "retcode": 1403, "message": "offline"})
    assert out == {"status": "failed", "retcode": -403, "data": None, "message": "offline"}
    assert reply("set_nickname", None)["data"] == {}


def test_message_wrap_and_history_sort_without_id_as_sequence():
    message = {"message_id": 999, "message_seq": 12, "time": 100, "message_type": "group",
               "group_id": 3, "sender": {"user_id": 2}, "message": [{"type": "text", "data": {"text": "hello"}}]}
    data = reply("get_message", message)["data"]["message"]
    assert data["message_scene"] == "group" and data["peer_id"] == 3 and data["sender_id"] == 2
    assert data["segments"] == [{"type": "text", "data": {"text": "hello"}}]
    assert "group" not in data  # No invented default entity.
    history = reply("get_history_messages", {"messages": [message, {**message, "message_seq": 11}]})["data"]
    assert [m["message_seq"] for m in history["messages"]] == [11, 12]
    assert history["next_message_seq"] is None
    del message["message_seq"]
    assert reply("get_message", message)["status"] == "failed"


def test_forward_and_essence_missing_facts_are_not_fabricated():
    node = {"sender": {"nickname": "a"}, "time": 10, "message": []}
    assert reply("get_forwarded_messages", {"messages": [node]})["status"] == "failed"
    node.update(message_seq=1, avatar_url="https://example.test/avatar")
    out = reply("get_forwarded_messages", {"messages": [node]})
    assert out["data"]["messages"][0]["sender_name"] == "a"
    assert reply("get_group_essence_messages", {"messages": []})["status"] == "failed"
    assert reply("get_group_essence_messages", {"messages": [], "is_end": True})["data"]["is_end"] is True

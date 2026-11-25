"""Milky parameter wiring through real Python adapters, without QQ or network."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from protocols.milky.actions import UnknownAction, resolve
from qsick.onebot.actions.loader import build_registry, load_catalog


def dispatch(api, params):
    action, body = resolve(api, params)
    catalog = load_catalog()
    registry = build_registry({"actions": [entry for entry in catalog["actions"]
                                           if entry["name"] == action]})
    calls = []

    async def call(op, params=None, **kwargs):
        calls.append((op, dict(params or {})))
        return SimpleNamespace(ok=True, data={}, message="")

    reply = asyncio.run(registry.dispatch(action, body, SimpleNamespace(call=call, protocol="milky")))
    assert reply["status"] == "ok", reply
    assert calls
    return calls[-1]


@pytest.mark.parametrize("api,params,expected", [
    ("set_group_name", {"group_id": 1, "new_group_name": "new"}, {"group_name": "new"}),
    ("set_group_member_admin", {"group_id": 1, "user_id": 2, "is_set": False}, {"enable": False}),
    ("set_group_whole_mute", {"group_id": 1, "is_mute": False}, {"enable": False}),
    ("send_profile_like", {"user_id": 2, "count": 7}, {"times": 7}),
    ("rename_group_file", {"group_id": 1, "file_id": "f", "parent_folder_id": "d", "new_file_name": "new"},
     {"folder_id": "d", "file_name": "new"}),
    ("rename_group_folder", {"group_id": 1, "folder_id": "d", "new_folder_name": "new"},
     {"folder_id": "d", "folder_name": "new"}),
    ("set_group_member_mute", {"group_id": 1, "user_id": 2}, {"duration": 0}),
])
def test_real_adapter_receives_fields(api, params, expected):
    original = dict(params)
    _, received = dispatch(api, params)
    for field, value in expected.items():
        assert received[field] == value
    assert params == original


@pytest.mark.parametrize("folder,action", [(None, "get_group_root_files"), ("/", "get_group_root_files"),
                                           ("", "get_group_root_files"), ("child", "get_group_files_by_folder")])
def test_group_file_directory_selection(folder, action):
    params = {"group_id": 1}
    if folder is not None:
        params["parent_folder_id"] = folder
    name, body = resolve("get_group_files", params)
    assert name == action
    op, received = dispatch("get_group_files", params)
    if folder == "child":
        assert op == "group.files.folder"
        assert received["folder_id"] == "child"
    else:
        assert "folder_id" not in body


@pytest.mark.parametrize("is_set,action", [(True, "set_essence_msg"), (False, "delete_essence_msg")])
def test_essence_selects_real_operation_without_forging_message_id(is_set, action):
    name, body = resolve("set_group_essence_message", {"group_id": 1, "message_seq": 42, "is_set": is_set})
    assert name == action
    assert body == {"group_id": 1, "message_seq": 42}


def test_upload_parameter_translation_without_io():
    action, body = resolve("upload_group_file", {
        "group_id": 1, "file_uri": "file:///C:/test.txt", "file_name": "test.txt", "parent_folder_id": "d"})
    assert action == "upload_group_file"
    assert body == {"group_id": 1, "file": "file:///C:/test.txt", "name": "test.txt", "folder_id": "d"}


@pytest.mark.parametrize("api", ["get_history_messages", "get_resource_temp_url", "mark_message_as_read",
                                 "get_group_files", "set_group_essence_message"])
def test_every_dynamic_resolver_preserves_account(api):
    from protocols.milky.actions import DYNAMIC
    params = {"account": "selected-account", "group_id": 1, "peer_id": 1, "message_seq": 42}
    assert DYNAMIC[api](params)[1]["account"] == "selected-account"
    assert resolve(api, params)[1]["account"] == "selected-account"


def test_dynamic_reverse_names_remain_available():
    from protocols.milky.actions import api_for
    assert api_for("get_group_root_files") == "get_group_files"
    assert api_for("get_group_files_by_folder") == "get_group_files"
    assert api_for("set_essence_msg") == "set_group_essence_message"
    assert api_for("delete_essence_msg") == "set_group_essence_message"


def test_avatar_uses_standard_image_uri():
    assert resolve("set_group_avatar", {"group_id": 1, "image_uri": "file:///avatar.png"}) == (
        "set_group_portrait", {"group_id": 1, "file": "file:///avatar.png"})


def test_mark_read_does_not_relabel_sequence():
    _, body = resolve("mark_message_as_read", {"message_scene": "group", "peer_id": 1, "message_seq": 42})
    assert "message_id" not in body
    assert body["message_seq"] == 42


@pytest.mark.parametrize("api", ["accept_group_request", "reject_group_request", "accept_friend_request",
                                 "reject_friend_request", "accept_group_invitation", "reject_group_invitation",
                                 "persist_group_file"])
def test_unmapped_or_missing_capabilities_remain_rejected(api):
    with pytest.raises(UnknownAction):
        resolve(api, {"notification_seq": 42, "initiator_uid": "u_test"})

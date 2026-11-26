"""The message cache must record who sent a message, and a rejected save must save nothing.

Two bugs found against a live instance:

1. Every cached message recorded `sender_uin = 0`. The cache hands `Message.from_payload` the
   event's own `sender` object, which an OneBot event spells `{user_id, nickname, card, role}`,
   but the parser only looked for the internal `uin`. Measured on the live database: the `raw`
   column kept the nickname while the `sender_uin` column was 0 for all 11k rows.

2. Saving a network profile the page then rejected ("HTTP / WS / SSE paths must differ") had
   already written the profile to disk, so a refresh showed it -- and `apply_to_config` mutates
   `config.onebot` before validation, so the running config changed too.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for entry in (str(ROOT), str(ROOT / "src")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from qsick.protocol.message import Message  # noqa: E402


def cached_payload(sender, *, account="3905548034", chat_type="private", chat_id=1927676487):
    """The shape `runtime._cache_pipeline` builds from an OneBot event."""
    return {
        "message_id": 7712269338440489810,
        "seq": 1501,
        "time": 1795652634,
        "chat": {"type": chat_type, "id": chat_id},
        "sender": sender,
        "elements": [{"type": "text", "data": {"text": "hi"}}],
        "account": account,
    }


def test_an_onebot_sender_records_the_account_number():
    """`user_id` is what the event calls it; that is the field the cache sees."""
    message = Message.from_payload(cached_payload(
        {"user_id": 1410675509, "nickname": "某人", "card": "", "role": "member"}))
    assert message.sender_uin == 1410675509


def test_the_internal_spelling_still_works():
    message = Message.from_payload(cached_payload(
        {"uin": 3572474519, "uid": "u_x", "nickname": "洛", "card": "", "role": "member"}))
    assert message.sender_uin == 3572474519


def test_a_top_level_user_id_is_still_honoured():
    payload = cached_payload({})
    payload["user_id"] = 860417956
    assert Message.from_payload(payload).sender_uin == 860417956


def test_a_missing_sender_is_zero_not_a_crash():
    payload = cached_payload({})
    payload.pop("sender")
    assert Message.from_payload(payload).sender_uin == 0


def test_the_card_stands_in_for_a_missing_nickname():
    """A member with no nickname still has a `card`; showing nothing would hide who it was."""
    message = Message.from_payload(cached_payload(
        {"user_id": 1918530969, "nickname": "", "card": "L-ZY沂沨", "role": "member"}))
    assert message.sender_name == "L-ZY沂沨"


def test_both_private_directions_are_cacheable():
    """Self-sent and received private messages must both round-trip with a usable sender."""
    received = Message.from_payload(cached_payload({"user_id": 1410675509, "nickname": "某人"}))
    sent = Message.from_payload(cached_payload({"user_id": 3905548034, "nickname": "自己"}))
    assert received.sender_uin == 1410675509
    assert sent.sender_uin == 3905548034
    # chat_id is the peer in both cases, so only the sender distinguishes the direction.
    assert received.chat_id == sent.chat_id == 1927676487


# ------------------------------------------------------------------ profile saves


@pytest.fixture()
def webui(tmp_path, monkeypatch):
    """A WebUI app over a runtime whose config lives in a temp directory.

    `_active_accounts` is faked: profiles are only applied for logged-in accounts, so without one
    the mapping to `config.onebot` is skipped entirely and the path-collision check never runs --
    which is why an earlier version of these tests passed a save that the page rejects in practice.
    """
    from fastapi.testclient import TestClient

    from qsick.config import RuntimeConfig
    from qsick.runtime import Runtime
    from qsick.webui.app import create_app

    config = RuntimeConfig(data_dir=str(tmp_path))
    runtime = Runtime(config)
    runtime._serving = True
    monkeypatch.setattr(runtime, "_active_accounts", lambda: ["3905548034"])
    return TestClient(create_app(runtime)), runtime


def posted_profile(port: int, path: str) -> dict:
    return {"action": "upsert", "kind": "http", "protocol": "onebotv11",
            "instance": "3905548034", "host": "127.0.0.1", "port": port,
            "path": path, "enabled": True}


def test_a_rejected_save_persists_nothing(webui):
    """The reported bug: the dialog said it failed, the refresh showed the profile anyway."""
    from qsick import network_profiles as netprofiles

    client, runtime = webui
    # A ws_server whose path equals the http path is what the page rejects.
    client.post("/api/network/profiles", json=posted_profile(3077, "/same"))
    before = netprofiles.load(runtime.config)

    response = client.post("/api/network/profiles", json={
        "action": "upsert", "kind": "ws_server", "protocol": "onebotv11",
        "instance": "3905548034", "host": "127.0.0.1", "port": 3077,
        "path": "/same", "enabled": True})

    assert response.status_code == 400, response.text
    assert "重复" in response.json()["error"], response.json()["error"]
    after = netprofiles.load(runtime.config)
    # Compare identities, not counts: the built-in default profiles are in the store from the start,
    # so "no ws_server at all" would fail for reasons that have nothing to do with this save.
    assert [item.get("id") for item in after] == [item.get("id") for item in before], \
        f"a rejected save still wrote: {after}"


def test_a_rejected_save_leaves_the_running_config_untouched(webui):
    """`apply_to_config` mutates as it goes; a rejection must not leave that behind."""
    client, runtime = webui
    client.post("/api/network/profiles", json=posted_profile(3078, "/one"))
    host_before = runtime.config.onebot.host
    port_before = runtime.config.onebot.port

    response = client.post("/api/network/profiles", json={
        "action": "upsert", "kind": "ws_server", "protocol": "onebotv11",
        "instance": "3905548034", "host": "0.0.0.0", "port": 3078,
        "path": "/one", "enabled": True})

    assert response.status_code == 400, response.text
    assert runtime.config.onebot.host == host_before
    assert runtime.config.onebot.port == port_before

def test_the_sender_label_carries_both_name_and_number():
    """`昵称（QQ号）`: the name is readable, the number is the part a client can act on."""
    message = Message.from_payload(cached_payload(
        {"user_id": 3572474519, "nickname": "洛", "card": "", "role": "member"}))
    assert message.sender_label == "洛（3572474519）"


def test_the_sender_label_degrades_without_inventing_parts():
    """Whichever half is known is shown; neither is invented to fill the other."""
    named = Message.from_payload(cached_payload({"user_id": 0, "nickname": "洛"}))
    assert named.sender_label == "洛"
    numbered = Message.from_payload(cached_payload({"user_id": 1410675509, "nickname": ""}))
    assert numbered.sender_label == "1410675509"
    neither = Message.from_payload(cached_payload({}))
    assert neither.sender_label == "未知"


def test_the_messages_page_renders_the_label(webui):
    """The page must show it too, not just the object."""
    from qsick.protocol.message import Message

    client, runtime = webui
    runtime.cache_store.put(Message.from_payload(cached_payload(
        {"user_id": 3572474519, "nickname": "洛", "card": "", "role": "member"})))
    body = client.get("/messages").text
    assert "洛（3572474519）" in body, body[:400]

"""Standard-API behaviour that must not fabricate success. No native/runtime use."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from qsick.onebot.actions.base import ActionError, member_json
from qsick.onebot.actions.message.get_forward_msg import _handle as forward_handle
from qsick.onebot.actions.system.get_image import _handle as image_handle
from qsick.onebot.actions.system.get_record import _handle as record_handle
from qsick.protocol import elements as E
from qsick.transport.base import SendResult


def ok(data):
    return SendResult(ok=True, status=0, data=data)


def test_member_json_keeps_standard_optional_fields_without_inventing_them():
    full = member_json(1, {
        "uin": 2, "nickname": "n", "card": "c", "role": "admin", "join_time": 3, "last_sent_time": 4,
        "level": 5, "sex": "male", "age": 20, "area": "x", "unfriendly": False, "title": "t",
        "title_expire_time": 9, "card_changeable": True,
    })
    for key in ("sex", "age", "area", "unfriendly", "title", "title_expire_time", "card_changeable"):
        assert key in full
    assert full["level"] == "5"
    minimal = member_json(1, {"uin": 2})
    for key in ("sex", "age", "area", "title"):
        assert key not in minimal


def test_new_standard_segments_round_trip():
    for segment in (
        {"type": "share", "data": {"url": "u", "title": "t", "content": "c", "image": "i"}},
        {"type": "node", "data": {"user_id": 1, "nickname": "n", "content": []}},
        {"type": "shake", "data": {}},
    ):
        assert E.to_onebot(E.from_onebot(segment)) == segment


async def test_forward_action_requires_a_conversation_and_reports_empty_forwards():
    async def fake_call(op, params):
        assert op == "forward.get"
        return ok({"message": []})

    ctx = SimpleNamespace(call=fake_call, store=None)
    with pytest.raises(ActionError):
        await forward_handle(ctx, id="resid_abcdefgh")
    with pytest.raises(ActionError):
        await forward_handle(ctx, id="resid_abcdefgh", chat_type="group", chat_id=1)


async def test_forward_action_returns_standard_node_array():
    nodes = [{"type": "node", "data": {"user_id": 9, "nickname": "bob", "time": 5, "content": [{"type": "text", "data": {"text": "hi"}}]}}]

    async def fake_call(op, params):
        return ok({"message": nodes})

    result = await forward_handle(SimpleNamespace(call=fake_call, store=None), id="resid_abcdefgh", chat_type="group", chat_id=1)
    assert result["message"] == nodes
    assert result["messages"][0]["sender"]["user_id"] == 9


async def test_get_image_prefers_downloaded_path_and_refuses_transcode_silently():
    async def download(op, params):
        assert op == "media.download"
        assert params["message_id"] == "7" and params["element_id"] == "e"
        return ok({"file_path": "/tmp/a.png", "file_size": 3})

    ctx = SimpleNamespace(call=download)
    image = await image_handle(ctx, message_id=7, element_id="e", chat_type="group", chat_id=1)
    assert image["file"] == "/tmp/a.png" and image["file_path"] == "/tmp/a.png"

    ctx2 = SimpleNamespace(call=download)
    record = await record_handle(ctx2, message_id=7, element_id="e", chat_type="group", chat_id=1, out_format="mp3")
    assert record["file_path"] == "/tmp/a.png"
    assert "not implemented" in record["note"]

    with pytest.raises(ActionError):
        await record_handle(ctx2, message_id=7, element_id="e", chat_type="group", chat_id=1, out_format="exe")


async def test_get_image_without_element_falls_back_to_url_and_says_so():
    async def media(op, params):
        assert op == "media.url"
        return ok({"url": "https://example.invalid/x", "expires_in": 60})

    result = await image_handle(SimpleNamespace(call=media), file_id="abc")
    assert result["url"] == "https://example.invalid/x"
    assert "no local element was downloaded" in result["note"]

    with pytest.raises(ActionError):
        await image_handle(SimpleNamespace(call=media))

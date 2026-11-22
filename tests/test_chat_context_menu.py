"""Chat context-menu APIs and structured rendering. Isolated ASGI, no QQ."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from qsick.config import RuntimeConfig
from qsick.protocol.elements import Element
from qsick.protocol.message import Message
from qsick.storage import MemoryStore
from qsick.webui.app import create_app

ACCOUNT = "10001"


def runtime(tmp_path):
    config = RuntimeConfig(data_dir=str(tmp_path / "data"))
    config.webui.chat_enabled = True
    config.headless_root = str(tmp_path / "hosts")
    return SimpleNamespace(
        config=config, store=MemoryStore(), self_id=10001,
        transport=SimpleNamespace(name="headless"),
        invoke=AsyncMock(return_value={"status": "ok", "retcode": 0, "data": {}}),
        # media.download is a host action, not a OneBot one, so the route asks the
        # transport directly (that is also how 收藏 works).
        send=AsyncMock(return_value=SimpleNamespace(ok=True, data={}, message="")),
    )


@pytest.fixture
async def web(tmp_path):
    rt = runtime(tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(rt)), base_url="http://t") as client:
        client.rt = rt
        yield client


def put(rt, mid, *, chat_type="group", chat_id=500, text="hi", sender=20001, name="Alice", elements=None, time=None):
    message = Message(
        message_id=mid, seq=mid, time=mid if time is None else time,
        account=ACCOUNT, chat_type=chat_type, chat_id=chat_id,
        sender_uin=sender, sender_name=name,
        elements=elements if elements is not None else [Element("text", {"text": text})],
    )
    rt.store.put(message)
    return message


async def test_segments_expose_mentions_quotes_markdown_and_media(web):
    put(web.rt, 1, elements=[
        Element("reply", {"message_id": "900"}),
        Element("at", {"user_id": "20002", "name": "@Bob"}),
        Element("text", {"text": " 你好"}),
        Element("image", {"url": "https://example.invalid/i.png", "width": 640, "height": 480, "summary": "图"}),
        Element("mface", {"emoji_id": "e1", "summary": "表情包"}),
        Element("markdown", {"content": "# 标题\n- 项目"}),
    ])
    response = await web.get("/api/chat/history", params={
        "account": ACCOUNT, "chat_type": "group", "chat_id": "500"})
    segments = response.json()["messages"][0]["segments"]
    assert [segment["type"] for segment in segments] == [
        "reply", "at", "text", "image", "mface", "markdown"]
    assert segments[1]["name"] == "@Bob" and segments[1]["user_id"] == "20002"
    # Images go through the same-origin proxy: QQ's CDN sends no CORS headers,
    # and a cross-origin URL also leaks the user's IP to that host.
    assert segments[3]["url"].startswith("/api/chat/remote-media?url=")
    assert "example.invalid" in segments[3]["url"]
    assert segments[3]["width"] == 640
    assert segments[5]["content"].startswith("# 标题")


async def test_preview_keeps_mention_names_and_labels(web):
    put(web.rt, 1, elements=[
        Element("at", {"user_id": "20002", "name": "Bob"}),
        Element("text", {"text": " 看这个"}),
        Element("image", {"url": "u"}),
    ])
    items = (await web.get("/api/chat/conversations", params={"account": ACCOUNT})).json()["conversations"]
    assert items[0]["preview"] == "@Bob 看这个[图片]"


async def test_conversation_pin_orders_and_persists(web):
    put(web.rt, 10, chat_id=500, time=10)
    put(web.rt, 20, chat_id=600, time=20)
    first = (await web.get("/api/chat/conversations", params={"account": ACCOUNT})).json()["conversations"]
    assert [item["id"] for item in first] == ["600", "500"]
    pin = await web.post("/api/chat/pin", json={
        "account": ACCOUNT, "chat_type": "group", "chat_id": "500", "pinned": True})
    assert pin.json() == {"ok": True, "pinned": True}
    pinned = (await web.get("/api/chat/conversations", params={"account": ACCOUNT})).json()["conversations"]
    assert [item["id"] for item in pinned] == ["500", "600"]
    assert pinned[0]["pinned"] is True and pinned[1]["pinned"] is False
    from pathlib import Path
    state_file = Path(web.rt.config.headless_root) / "chat-ui-state.json"
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert state[f"{ACCOUNT}:group:500"] is True
    await web.post("/api/chat/pin", json={
        "account": ACCOUNT, "chat_type": "group", "chat_id": "500", "pinned": False})
    cleared = (await web.get("/api/chat/conversations", params={"account": ACCOUNT})).json()["conversations"]
    assert [item["id"] for item in cleared] == ["600", "500"]


async def test_hidden_conversation_returns_for_a_newer_message(web):
    put(web.rt, 10, chat_id=500, time=10)
    await web.post("/api/chat/hide", json={"account": ACCOUNT, "chat_type": "group", "chat_id": "500"})
    assert (await web.get("/api/chat/conversations", params={"account": ACCOUNT})).json()["conversations"] == []
    put(web.rt, 30, chat_id=500, time=30)
    back = (await web.get("/api/chat/conversations", params={"account": ACCOUNT})).json()["conversations"]
    assert [item["id"] for item in back] == ["500"]


async def test_delete_message_removes_only_local_record(web):
    put(web.rt, 1)
    put(web.rt, 2)
    response = await web.post("/api/chat/delete-message", json={"account": ACCOUNT, "message_id": "1"})
    assert response.json() == {"ok": True, "deleted": True}
    history = (await web.get("/api/chat/history", params={
        "account": ACCOUNT, "chat_type": "group", "chat_id": "500"})).json()["messages"]
    assert [item["message_id"] for item in history] == ["2"]
    web.rt.invoke.assert_not_awaited()
    missing = await web.post("/api/chat/delete-message", json={"account": ACCOUNT, "message_id": "9"})
    assert missing.json()["deleted"] is False


async def test_delete_conversation_removes_all_messages_and_ui_state(web):
    for mid in (1, 2, 3):
        put(web.rt, mid, chat_id=500)
    put(web.rt, 4, chat_id=600)
    await web.post("/api/chat/pin", json={"account": ACCOUNT, "chat_type": "group", "chat_id": "500", "pinned": True})
    response = await web.post("/api/chat/delete-conversation", json={
        "account": ACCOUNT, "chat_type": "group", "chat_id": "500"})
    assert response.json() == {"ok": True, "deleted": 3}
    remaining = (await web.get("/api/chat/conversations", params={"account": ACCOUNT})).json()["conversations"]
    assert [item["id"] for item in remaining] == ["600"]


async def test_recall_forwards_to_transport_and_reports_rejection(web):
    put(web.rt, 1)
    await web.post("/api/chat/recall", json={"account": ACCOUNT, "message_id": "1"})
    web.rt.invoke.assert_awaited_once_with("delete_msg", {"account": "10001", "message_id": 1})
    web.rt.invoke.return_value = {"status": "failed", "retcode": 1502, "message": "no cached reference"}
    response = await web.post("/api/chat/recall", json={"account": ACCOUNT, "message_id": "1"})
    assert response.status_code == 409
    assert "no cached reference" in response.json()["error"]


@pytest.mark.parametrize("path,body", [
    ("/api/chat/pin", {"account": ACCOUNT, "chat_type": "group", "chat_id": "500", "pinned": "yes"}),
    ("/api/chat/pin", {"account": ACCOUNT, "chat_type": "guild", "chat_id": "500", "pinned": True}),
    ("/api/chat/pin", {"account": "", "chat_type": "group", "chat_id": "500", "pinned": True}),
    ("/api/chat/delete-message", {"account": ACCOUNT, "message_id": "x"}),
    ("/api/chat/recall", {"account": "99999", "message_id": "1"}),
    ("/api/chat/hide", {"account": ACCOUNT, "chat_type": "group"}),
])
async def test_invalid_menu_requests_are_rejected(web, path, body):
    response = await web.post(path, json=body)
    assert response.status_code in (400, 409)
    web.rt.invoke.assert_not_awaited()


async def test_reply_segment_carries_quoted_content(web):
    put(web.rt, 1, elements=[Element("text", {"text": "原始消息"})], name="Alice")
    put(web.rt, 2, elements=[Element("reply", {"message_id": "1"}), Element("text", {"text": "回复"})], name="Bob")
    history = (await web.get("/api/chat/history", params={
        "account": ACCOUNT, "chat_type": "group", "chat_id": "500"})).json()["messages"]
    reply = history[1]["segments"][0]
    assert reply["type"] == "reply" and reply["message_id"] == "1"
    assert reply["quote"]["found"] is True
    assert reply["quote"]["sender"] == "Alice"
    assert reply["quote"]["text"] == "原始消息"


async def test_reply_to_missing_message_is_marked_not_found(web):
    put(web.rt, 5, elements=[Element("reply", {"message_id": "999"}), Element("text", {"text": "?"})])
    history = (await web.get("/api/chat/history", params={
        "account": ACCOUNT, "chat_type": "group", "chat_id": "500"})).json()["messages"]
    assert history[0]["segments"][0]["quote"] == {"found": False}


async def test_quote_can_reference_a_message_outside_the_returned_page(web):
    put(web.rt, 1, elements=[Element("text", {"text": "很旧的消息"})])
    for mid in range(2, 8):
        put(web.rt, mid, elements=[Element("text", {"text": "填" * 10})])
    put(web.rt, 9, elements=[Element("reply", {"message_id": "1"}), Element("text", {"text": "回复旧的"})])
    page = (await web.get("/api/chat/history", params={
        "account": ACCOUNT, "chat_type": "group", "chat_id": "500", "limit": "2"})).json()["messages"]
    quoted = page[-1]["segments"][0]["quote"]
    assert quoted["found"] is True and quoted["text"] == "很旧的消息"


async def test_market_face_segment_is_flagged_without_fabricated_url(web):
    # The old "parcel/item" template always answered 404, so the segment must not
    # invent a URL for a store face. Only what QQ reported may be forwarded.
    put(web.rt, 1, elements=[Element("mface", {"emoji_id": "abcdef123", "summary": "笑"})])
    segment = (await web.get("/api/chat/history", params={
        "account": ACCOUNT, "chat_type": "group", "chat_id": "500"})).json()["messages"][0]["segments"][0]
    assert segment["market_face"] is True
    assert segment["url"] == ""
    assert segment["emoji_id"] == "abcdef123"
    assert segment["summary"] == "笑"


async def test_market_face_forwards_the_url_qq_reported(web):
    put(web.rt, 1, elements=[Element("mface", {"emoji_id": "abc", "url": "https://p.qpic.cn/real/0"})])
    segment = (await web.get("/api/chat/history", params={
        "account": ACCOUNT, "chat_type": "group", "chat_id": "500"})).json()["messages"][0]["segments"][0]
    assert segment["url"].startswith("/api/chat/remote-media?url=")
    assert "p.qpic.cn" in segment["url"]


async def test_send_with_reply_to_uses_standard_reply_segment(web):
    await web.post("/api/chat/send-text", json={
        "account": ACCOUNT, "chat_type": "group", "chat_id": "500", "text": "回复你", "reply_to": "42"})
    params = web.rt.invoke.await_args.args[1]
    assert params["message"][0] == {"type": "reply", "data": {"id": "42"}}
    assert params["message"][1]["type"] == "text"


async def test_send_rejects_malformed_reply_to(web):
    response = await web.post("/api/chat/send-text", json={
        "account": ACCOUNT, "chat_type": "group", "chat_id": "500", "text": "x", "reply_to": "abc"})
    assert response.status_code == 400
    web.rt.invoke.assert_not_awaited()


async def test_reply_without_message_id_falls_back_to_seq_and_sender(web):
    """Third-party quotes often carry only seq + sender, never replayMsgId."""
    original = put(web.rt, 100, text="被引用的原话", sender=20001, name="Alice")
    original.seq = 4242
    web.rt.store.put(original)
    quoted = Message(message_id=101, seq=101, time=101, account=ACCOUNT, chat_type="group", chat_id=500,
                     sender_uin=20002, sender_name="Bob",
                     elements=[Element("reply", {"seq": "4242", "sender_uin": "20001"}),
                               Element("text", {"text": "回应"})])
    web.rt.store.put(quoted)
    segments = (await web.get("/api/chat/history", params={
        "account": ACCOUNT, "chat_type": "group", "chat_id": "500"})).json()["messages"][1]["segments"]
    assert segments[0]["quote"]["found"] is True
    assert segments[0]["quote"]["message_id"] == "100"
    assert segments[0]["quote"]["text"] == "被引用的原话"


async def test_seq_fallback_requires_a_matching_sender(web):
    message = put(web.rt, 200, text="别人的话", sender=30001, name="Carol")
    message.seq = 777
    web.rt.store.put(message)
    quoted = Message(message_id=201, seq=201, time=201, account=ACCOUNT, chat_type="group", chat_id=500,
                     sender_uin=20002, elements=[Element("reply", {"seq": "777", "sender_uin": "99999"}),
                                                 Element("text", {"text": "?"})])
    web.rt.store.put(quoted)
    segments = (await web.get("/api/chat/history", params={
        "account": ACCOUNT, "chat_type": "group", "chat_id": "500"})).json()["messages"][1]["segments"]
    # A seq match with the wrong sender must not be presented as the target.
    assert segments[0]["quote"] == {"found": False}


async def test_ambiguous_seq_without_sender_stays_unresolved(web):
    for mid in (301, 302):
        message = put(web.rt, mid, text="重复序号", sender=20001)
        message.seq = 555
        web.rt.store.put(message)
    quoted = Message(message_id=303, seq=303, time=303, account=ACCOUNT, chat_type="group", chat_id=500,
                     sender_uin=20002, elements=[Element("reply", {"seq": "555"}), Element("text", {"text": "?"})])
    web.rt.store.put(quoted)
    segments = (await web.get("/api/chat/history", params={
        "account": ACCOUNT, "chat_type": "group", "chat_id": "500"})).json()["messages"][2]["segments"]
    assert segments[0]["quote"] == {"found": False}


async def test_system_face_uses_the_builtin_index_not_a_placeholder(web):
    put(web.rt, 1, elements=[Element("face", {"id": "14"})])
    segment = (await web.get("/api/chat/history", params={
        "account": ACCOUNT, "chat_type": "group", "chat_id": "500"})).json()["messages"][0]["segments"][0]
    assert segment["type"] == "face"
    # Names must come from QQ's own config, not a hand-written table.
    assert segment["name"] == "微笑"
    items = (await web.get("/api/chat/conversations", params={"account": ACCOUNT})).json()["conversations"]
    assert items[0]["preview"] == "[微笑]"


async def test_unknown_face_id_degrades_without_inventing_a_name(web):
    put(web.rt, 1, elements=[Element("face", {"id": "99999"})])
    segment = (await web.get("/api/chat/history", params={
        "account": ACCOUNT, "chat_type": "group", "chat_id": "500"})).json()["messages"][0]["segments"][0]
    assert segment["name"] == "表情"


async def test_like_endpoint_forwards_ten_likes(web):
    await web.post("/api/chat/like", json={"account": ACCOUNT, "user_id": "20001", "times": 10})
    web.rt.invoke.assert_awaited_once_with("send_like", {"account": "10001", "user_id": 20001, "times": 10})


@pytest.mark.parametrize("body", [
    {"account": ACCOUNT, "user_id": "20001", "times": 0},
    {"account": ACCOUNT, "user_id": "20001", "times": 21},
    {"account": ACCOUNT, "user_id": "20001", "times": True},
    {"account": ACCOUNT, "user_id": "oops", "times": 10},
    {"account": "99999", "user_id": "20001", "times": 10},
])
async def test_invalid_like_requests_are_rejected(web, body):
    response = await web.post("/api/chat/like", json=body)
    assert response.status_code in (400, 409)
    web.rt.invoke.assert_not_awaited()


async def test_like_reports_native_rejection(web):
    web.rt.invoke.return_value = {"status": "failed", "retcode": 1502, "message": "no uid mapping"}
    response = await web.post("/api/chat/like", json={"account": ACCOUNT, "user_id": "20001", "times": 10})
    assert response.status_code == 409
    assert "no uid mapping" in response.json()["error"]


async def test_sending_a_picked_face_uses_a_real_face_segment(web):
    await web.post("/api/chat/send-text", json={
        "account": ACCOUNT, "chat_type": "group", "chat_id": "500", "text": "看这个[CQ:face,id=14]好吧"})
    segments = web.rt.invoke.await_args.args[1]["message"]
    assert [seg["type"] for seg in segments] == ["text", "face", "text"]
    assert segments[1]["data"]["id"] == "14"
    assert segments[0]["data"]["text"] == "看这个"


async def test_download_media_asks_host_and_persists_the_path(web, tmp_path):
    image = tmp_path / "pic.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)
    put(web.rt, 42, elements=[Element("image", {"element_id": "el1", "url": "/download?x"})])
    web.rt.send.return_value = SimpleNamespace(ok=True, data={"file_path": str(image)}, message="")
    response = await web.post("/api/chat/download-media", json={
        "account": ACCOUNT, "message_id": "42", "element_id": "el1",
        "chat_type": "group", "chat_id": "500"})
    body = response.json()
    assert body["ok"] is True and body["url"].startswith("/api/chat/media?path=")
    assert web.rt.store.get(42).elements[0].data["local_path"] == str(image)
    web.rt.send.assert_awaited_once()
    assert web.rt.send.await_args.args[0] == "media.download"


async def test_download_media_reports_failure_without_inventing_a_url(web):
    put(web.rt, 43, elements=[Element("image", {"element_id": "el2"})])
    web.rt.send.return_value = SimpleNamespace(ok=False, data={}, message="no cached reference")
    body = (await web.post("/api/chat/download-media", json={
        "account": ACCOUNT, "message_id": "43", "element_id": "el2",
        "chat_type": "group", "chat_id": "500"})).json()
    assert body["url"] == ""
    assert "no cached reference" in body["error"]
    # ok 与 error 不能互相矛盾：前端只在 ok === false 时报错，标成 true 会让
    # "没下到图"看起来像成功，界面上毫无反应。
    assert body["ok"] is False


async def test_download_media_requires_an_element_id(web):
    response = await web.post("/api/chat/download-media", json={
        "account": ACCOUNT, "message_id": "44", "chat_type": "group", "chat_id": "500"})
    assert response.status_code == 400
    web.rt.invoke.assert_not_awaited()


@pytest.mark.parametrize("face_id,expected", [
    ("0", "惊讶"), ("41", "发抖"), ("42", "爱情"), ("74", "太阳"),
    ("179", "doge"), ("392", "龙年快乐"),
])
async def test_face_names_match_the_qq_client(face_id, expected):
    """Regression: a hand-written table once mismatched 85 face names."""
    from qsick.webui.face_table import face_descriptor
    assert face_descriptor(face_id)["name"] == expected

async def test_download_media_passes_a_real_file_path_to_the_host(web, tmp_path):
    """宿主用 ``text(..., allowEmpty=false)`` 校验 file_path，空串会被直接拒绝。

    真实报错是 "file_path must be a non-empty string without NUL" —— 传空串等于
    让宿主无从决定落盘位置，于是整次下载失败。
    """
    web.rt.send.return_value = SimpleNamespace(
        ok=True, data={"file_path": str(tmp_path / "x.png")}, message="")
    await web.post("/api/chat/download-media", json={
        "account": ACCOUNT, "message_id": "77", "element_id": "el7",
        "chat_type": "group", "chat_id": "500"})
    params = web.rt.send.await_args.args[1]
    assert params["file_path"], "file_path must not be empty"
    assert "\0" not in params["file_path"]
    # 落在 data_dir 下的缓存目录里，同一元素重复下载覆盖同一份文件。
    assert "media-cache" in params["file_path"]


async def test_download_media_sanitises_the_cache_file_name(web):
    """元素 id 来自对端，不能直接当文件名用。"""
    web.rt.send.return_value = SimpleNamespace(
        ok=True, data={"file_path": "x.png"}, message="")
    await web.post("/api/chat/download-media", json={
        "account": ACCOUNT, "message_id": "88", "element_id": "../../evil",
        "chat_type": "group", "chat_id": "500"})
    params = web.rt.send.await_args.args[1]
    assert ".." not in params["file_path"]
    assert "evil" in params["file_path"]

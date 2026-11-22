"""Chat API tests: ASGI only, no runtime startup, network sends or config writes."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from qsick.config import RuntimeConfig
from qsick.protocol.elements import Element
from qsick.protocol.message import Message
from qsick.storage import MemoryStore, SqliteStore
from qsick.webui.app import create_app


@pytest.fixture(params=["memory", "sqlite"])
def chat_runtime(request, tmp_path):
    config = RuntimeConfig(data_dir=str(tmp_path / "unused-data"))
    config.webui.chat_enabled = True
    store = MemoryStore() if request.param == "memory" else SqliteStore(tmp_path / "chat.db", max_messages=0, max_days=0)
    runtime = SimpleNamespace(
        config=config, store=store, self_id=10001,
        invoke=AsyncMock(return_value={"status": "ok", "retcode": 0, "data": {"message_id": 42}}),
    )
    yield runtime
    store.close()


@pytest.fixture
async def web(chat_runtime):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(chat_runtime)), base_url="http://test") as client:
        yield client


def put(runtime, mid, *, account="10001", chat_type="group", chat_id=30001, sender=20001, name="Alice", text="hello", time=None):
    message = Message(
        message_id=mid, seq=mid, time=mid if time is None else time,
        account=account, chat_type=chat_type, chat_id=chat_id,
        sender_uin=sender, sender_name=name, elements=[Element("text", {"text": text})],
    )
    runtime.store.put(message)
    return message


def history_params(**extra):
    return {"account": "10001", "chat_type": "group", "chat_id": "30001", **extra}


async def test_conversations_are_account_scoped_and_not_limited_to_global_recent(web, chat_runtime):
    put(chat_runtime, 1, text="old account chat", time=10)
    put(chat_runtime, 2, chat_type="private", chat_id=20001, text="peer message", time=20)
    put(chat_runtime, 3, chat_type="private", chat_id=20001, sender=10001, name="My name", text="my reply", time=30)
    put(chat_runtime, 4, chat_type="private", chat_id=20002, sender=10001, name="My name", time=40)
    put(chat_runtime, 5, chat_type="private", chat_id=30001, sender=30001, name="", time=50)
    put(chat_runtime, 6, account="", text="unattributed must stay hidden")
    put(chat_runtime, 7, chat_type="unknown", text="unsupported")
    put(chat_runtime, 8, chat_id=0)
    for mid in range(100, 701):
        put(chat_runtime, mid, account="99999", text="another account", time=mid)

    response = await web.get("/api/chat/conversations", params={"account": "10001"})
    assert response.status_code == 200
    items = response.json()["conversations"]
    assert [(item["chat_type"], item["id"]) for item in items] == [
        ("private", "30001"), ("private", "20002"), ("private", "20001"), ("group", "30001"),
    ]
    assert items[0]["title"] == "QQ 30001"
    assert items[1]["title"] == "QQ 20002"
    assert items[2]["title"] == "Alice"
    assert items[2]["preview"] == "my reply"
    assert items[-1] == {
        "id": "30001", "chat_type": "group", "title": "群 30001",
        "preview": "old account chat", "last_time": 10, "sender": "Alice",
        "last_message_id": "1", "pinned": False,
    }
    history = await web.get("/api/chat/history", params=history_params())
    assert [message["message_id"] for message in history.json()["messages"]] == ["1"]
    chat_runtime.invoke.assert_not_awaited()


async def test_ordering_and_before_pagination_use_time_then_id(web, chat_runtime):
    # ID and insertion ordering intentionally disagree with message time.
    for mid, timestamp in [(90, 10), (5, 20), (7, 20), (2, 30), (3, 40)]:
        put(chat_runtime, mid, time=timestamp)
    put(chat_runtime, 100, account="20002", time=100)
    put(chat_runtime, 101, chat_type="private", time=101)
    put(chat_runtime, 102, chat_id=30002, time=102)
    first = (await web.get("/api/chat/history", params=history_params(limit=2))).json()
    assert [message["message_id"] for message in first["messages"]] == ["2", "3"]
    assert first["has_more"] is True and first["next_before"] == "2"
    second = (await web.get("/api/chat/history", params=history_params(limit=2, before=first["next_before"]))).json()
    assert [message["message_id"] for message in second["messages"]] == ["5", "7"]
    third = (await web.get("/api/chat/history", params=history_params(limit=2, before=second["next_before"]))).json()
    assert [message["message_id"] for message in third["messages"]] == ["90"]
    assert third["has_more"] is False and third["next_before"] is None
    for cursor in ["100", "101", "102", "999"]:
        response = await web.get("/api/chat/history", params=history_params(before=cursor))
        assert response.status_code == 404
    conversations = (await web.get("/api/chat/conversations", params={"account": "10001"})).json()
    group = next(item for item in conversations["conversations"] if item["chat_type"] == "group" and item["id"] == "30001")
    assert group["last_message_id"] == "3"


async def test_history_preserves_large_ids_plain_text_and_infers_outgoing(web, chat_runtime):
    mid = 9007199254740993
    put(chat_runtime, mid, sender=10001, name="", text="<b>hello & [world]</b>", time=1)
    message = put(chat_runtime, mid + 1, text="", time=2)
    message.elements = [Element("image", {"url": "https://example.invalid/private"})]
    chat_runtime.store.put(message)
    response = await web.get("/api/chat/history", params=history_params())
    first, second = response.json()["messages"]
    assert first["message_id"] == str(mid)
    assert first["sender_uin"] == "10001" and first["sender"] == "10001"
    assert first["text"] == "<b>hello & [world]</b>"
    assert first["time"] == 1 and first["outgoing"] is True
    assert first["segments"] == [{"type": "text", "text": "<b>hello & [world]</b>"}]
    assert second["text"] == "[图片]" and second["outgoing"] is False
    # Media arrives as a typed segment; the browser no longer re-parses labels.
    segment = second["segments"][0]
    assert segment["type"] == "image"
    # 图片一律经同源代理：跨域直连既会被 CORS 拦，也会把用户 IP 暴露给 CDN。
    assert segment["url"].startswith("/api/chat/remote-media?url=")
    assert "example.invalid" in segment["url"]
    assert segment["summary"] == "图片"
    assert segment["local_path"] == "" and segment["media_pending"] is True
    assert segment["sticker"] is False and segment["element_id"] == ""


async def test_private_history_and_empty_account_results(web, chat_runtime):
    put(chat_runtime, 1, chat_type="private", chat_id=20001)
    put(chat_runtime, 2, chat_type="group", chat_id=20001)
    response = await web.get("/api/chat/history", params=history_params(chat_type="private", chat_id="20001"))
    assert [item["message_id"] for item in response.json()["messages"]] == ["1"]
    response = await web.get("/api/chat/conversations", params={"account": "88888"})
    assert response.json() == {"conversations": []}
    response = await web.get("/api/chat/history", params=history_params(account="88888"))
    assert response.json() == {"messages": [], "has_more": False, "next_before": None}


@pytest.mark.parametrize("account", [None, "", " ", "0", "-1", "not-qq", "1.2", "１２３", "9" * 21])
async def test_missing_and_invalid_accounts_never_return_global_messages(web, chat_runtime, account):
    put(chat_runtime, 1)
    params = {} if account is None else {"account": account}
    assert (await web.get("/api/chat/conversations", params=params)).status_code == 400
    assert (await web.get("/api/chat/history", params={"chat_type": "group", "chat_id": "30001", **params})).status_code == 400
    payload = {"chat_type": "group", "chat_id": "30001", "text": "hello", **params}
    assert (await web.post("/api/chat/send-text", json=payload)).status_code == 400
    chat_runtime.invoke.assert_not_awaited()


@pytest.mark.parametrize("extra", [
    {"chat_type": ""}, {"chat_type": "guild"}, {"chat_id": ""}, {"chat_id": "-1"},
    {"limit": "0"}, {"limit": "201"}, {"limit": "abc"}, {"limit": "1.5"},
    {"before": ""}, {"before": "not-an-id"},
])
async def test_history_validates_filters_and_pagination(web, extra):
    response = await web.get("/api/chat/history", params=history_params(**extra))
    assert response.status_code == 400 and response.json()["ok"] is False


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/chat/conversations"), ("GET", "/api/chat/history"), ("POST", "/api/chat/send-text"),
])
async def test_new_endpoints_require_auth_and_chat_enabled(web, chat_runtime, method, path):
    chat_runtime.config.webui.access_token = "secret"
    chat_runtime.config.webui.chat_enabled = False
    assert (await web.request(method, path)).status_code == 401
    for kwargs in [
        {"headers": {"x-access-token": "secret"}},
        {"params": {"token": "secret"}},
        {"cookies": {"qsick_token": "secret"}},
    ]:
        response = await web.request(method, path, **kwargs)
        assert response.status_code == 403 and response.json()["error"] == "chat disabled"
    chat_runtime.invoke.assert_not_awaited()


async def test_default_history_limit_and_authenticated_access(web, chat_runtime):
    for mid in range(1, 206):
        put(chat_runtime, mid)
    chat_runtime.config.webui.access_token = "secret"
    response = await web.get(
        "/api/chat/history", params=history_params(), headers={"x-access-token": "secret"},
    )
    body = response.json()
    assert response.status_code == 200
    assert len(body["messages"]) == 100 and body["next_before"] == "106"
    assert body["messages"][-1]["message_id"] == "205"
    response = await web.get(
        "/api/chat/history", params=history_params(limit=200, token="secret"),
    )
    assert len(response.json()["messages"]) == 200


async def test_unknown_runtime_account_cannot_send(web, chat_runtime):
    chat_runtime.self_id = 0
    response = await web.post("/api/chat/send-text", json={**history_params(), "text": "hi"})
    assert response.status_code == 409
    chat_runtime.invoke.assert_not_awaited()


async def test_send_only_dispatches_current_account_group_text(web, chat_runtime):
    payload = {"account": "10001", "chat_type": "group", "chat_id": "30001", "text": "  literal [CQ:at,qq=all] & hi\n"}
    response = await web.post("/api/chat/send-text", json={**payload, "account": "10002"})
    assert response.status_code == 409 and "account" in response.json()["error"]
    response = await web.post("/api/chat/send-text", json={**payload, "chat_type": "private"})
    assert response.status_code == 501 and "unsupported" in response.json()["error"]
    chat_runtime.invoke.assert_not_awaited()
    response = await web.post("/api/chat/send-text", json=payload)
    assert response.json() == {"status": "ok", "retcode": 0, "data": {"message_id": 42}}
    chat_runtime.invoke.assert_awaited_once_with(
        "send_group_msg", {"account": "10001", "group_id": 30001, "message": [{"type": "text", "data": {"text": payload["text"]}}]},
    )


async def test_headless_private_send_uses_uid_resolving_transport(web, chat_runtime):
    chat_runtime.transport = SimpleNamespace(name="headless")
    response = await web.get("/api/chat/conversations", params={"account": "10001"})
    assert response.json()["send_private_supported"] is True
    result = await web.post("/api/chat/send-text", json={"account": "10001", "chat_type": "private", "chat_id": "20001", "text": "private test"})
    assert result.json()["status"] == "ok"
    chat_runtime.invoke.assert_awaited_once_with("send_private_msg", {"account": "10001", "user_id": 20001, "message": [{"type": "text", "data": {"text": "private test"}}]})


@pytest.mark.parametrize("payload", [
    None, [], "text", {},
    {"account": True}, {"account": 10001.0},
    {"chat_id": "oops"}, {"chat_id": {}}, {"chat_type": []},
    {"text": ""}, {"text": " \n"}, {"text": 123}, {"text": "x" * 10001},
])
async def test_send_rejects_invalid_json_payloads(web, chat_runtime, payload):
    valid = {"account": "10001", "chat_type": "group", "chat_id": "30001", "text": "hi"}
    body = {**valid, **payload} if isinstance(payload, dict) and payload else payload
    response = await web.post("/api/chat/send-text", json=body)
    assert response.status_code == 400
    chat_runtime.invoke.assert_not_awaited()


async def test_send_rejects_malformed_json_and_preserves_transport_failure(web, chat_runtime):
    response = await web.post("/api/chat/send-text", content=b"{bad", headers={"content-type": "application/json"})
    assert response.status_code == 400
    chat_runtime.invoke.assert_not_awaited()
    failure = {"status": "failed", "retcode": 1501, "message": "offline", "data": None}
    chat_runtime.invoke.return_value = failure
    response = await web.post("/api/chat/send-text", json={**history_params(), "text": "hi"})
    assert response.json() == failure


async def test_legacy_group_endpoints_keep_existing_shapes(web, chat_runtime):
    put(chat_runtime, 1)
    chat_runtime.invoke.return_value = {"data": []}
    groups = await web.get("/api/chat/groups", params={"account": "10001"})
    assert groups.json() == {"groups": [{"group_id": 30001, "group_name": "群 30001"}]}
    messages = await web.get("/api/chat/messages", params={"account": "10001", "group_id": 30001})
    assert messages.json()["messages"][0]["message_id"] == 1
    chat_runtime.invoke.reset_mock()
    response = await web.post("/api/chat/send", json={"group_id": 30001, "text": "legacy"})
    assert response.status_code == 200
    chat_runtime.invoke.assert_awaited_once_with(
        "send_group_msg", {"group_id": 30001, "message": [{"type": "text", "data": {"text": "legacy"}}]},
    )

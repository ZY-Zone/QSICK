"""Isolated identity wiring tests; never start QQ or a transport."""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from qsick.onebot.milky_bridge import BridgeError, MilkyBridge
from qsick.onebot.milky_server import register_milky_routes
from qsick.protocol.message import Message
from qsick.storage import MemoryStore


def runtime(*messages, accounts=("11",)):
    store = MemoryStore()
    for m in messages:
        store.put(m)
    calls = []
    async def invoke(name, params):
        calls.append((name, params))
        return {"status": "ok", "retcode": 0, "data": {"messages": []}}
    return SimpleNamespace(store=store, _active_accounts=lambda: accounts, invoke=invoke, calls=calls)


def msg(mid=9001, seq=7, account="11", peer=22, kind="group", native=None):
    return Message(mid, seq, 1234, kind, peer, 11, account=account, native=native or {})


def test_reverse_milky_call_uses_connection_account_not_body():
    import json
    from unittest.mock import AsyncMock, patch
    from qsick.onebot.reverse import ReverseWebSocketClient
    rt = runtime()
    rt.pipelines = object()
    client = ReverseWebSocketClient(rt, [("ws://example.invalid", "11", True, "milky", "p")])
    client._protocol_of = lambda account: "milky"
    client._send = AsyncMock()
    execute = AsyncMock(return_value={"status": "ok", "data": {}})
    with patch("qsick.onebot.milky_bridge.run_milky", execute):
        asyncio.run(client._on_message(object(), json.dumps({"action": "get_login_info",
            "params": {"account": "33"}, "echo": "e"}), "11"))
    assert execute.await_args.args[3]["account"] == "11"
    assert execute.await_args.args[4] == "e"


def test_sequence_is_resolved_not_used_as_id():
    rt = runtime(msg())
    action, params = asyncio.run(MilkyBridge(rt).prepare("recall_group_message", {"account": "11", "group_id": 22, "message_seq": 7}))
    assert action == "delete_msg"
    assert params == {"account": "11", "message_id": 9001}


def test_scope_and_account_miss_calls_history_not_mutation():
    rt = runtime(msg(account="33"), msg(mid=9002, peer=44))
    with pytest.raises(BridgeError, match="not found"):
        asyncio.run(MilkyBridge(rt).prepare("recall_group_message", {"account": "11", "group_id": 22, "message_seq": 7}))
    assert rt.calls == [("get_group_msg_history", {"account": "11", "group_id": 22, "message_seq": 7, "count": 20})]


def test_accounts_fail_closed_and_unique_profile_can_bind():
    rt = runtime(accounts=("11", "33"))
    bridge = MilkyBridge(rt)
    with pytest.raises(BridgeError, match="ambiguous"):
        bridge.account({})
    assert bridge.account({"account": "33"}) == "33"
    rt._network_profiles_for = lambda _: [{"protocol": "milky", "enabled": True, "kind": "http", "account": "11"}]
    assert bridge.account({}) == "11"


def test_essence_requires_real_random():
    rt = runtime(msg(native={"msgRandom": 314}))
    action, params = asyncio.run(MilkyBridge(rt).prepare("set_group_essence_message", {"account": "11", "group_id": 22, "message_seq": 7, "is_set": False}))
    assert action == "delete_essence_msg"
    assert params["msg_seq"] == 7 and params["msg_random"] == 314
    default_action, _ = asyncio.run(MilkyBridge(rt).prepare("set_group_essence_message", {"account": "11", "group_id": 22, "message_seq": 7}))
    assert default_action == "set_essence_msg"
    rt.store.put(msg())
    with pytest.raises(BridgeError, match="msg_random"):
        asyncio.run(MilkyBridge(rt).prepare("set_group_essence_message", {"account": "11", "group_id": 22, "message_seq": 7, "is_set": True}))


def test_forward_without_context_fails():
    with pytest.raises(BridgeError, match="context"):
        asyncio.run(MilkyBridge(runtime()).prepare("get_forwarded_messages", {"account": "11", "forward_id": "resource"}))


def test_forward_context_from_account_scoped_cache():
    from qsick.protocol.elements import Element
    message = msg()
    message.elements = [Element("forward", {"id": "resource"})]
    rt = runtime(message)
    action, params = asyncio.run(MilkyBridge(rt).prepare("get_forwarded_messages", {"account": "11", "forward_id": "resource"}))
    assert action == "get_forward_msg"
    assert params == {"account": "11", "id": "resource", "chat_type": "group", "chat_id": 22}


def test_account_instance_store_is_consulted():
    rt = runtime()
    instance_store = MemoryStore()
    instance_store.put(msg())
    rt.instances = SimpleNamespace(names=lambda: ["C1"], get=lambda _: SimpleNamespace(account="11", store=instance_store))
    action, params = asyncio.run(MilkyBridge(rt).prepare("recall_group_message", {"account": "11", "group_id": 22, "message_seq": 7}))
    assert action == "delete_msg" and params["message_id"] == 9001


def test_primary_cache_collision_fails_closed():
    rt = runtime(msg(account="33"))
    rt.cache_store = MemoryStore()
    rt.cache_store.put(msg())
    with pytest.raises(BridgeError, match="different conversation/account"):
        asyncio.run(MilkyBridge(rt).prepare("get_message", {"account": "11", "message_scene": "group", "peer_id": 22, "message_seq": 7}))
    assert not rt.calls


def test_native_history_resolution_still_invokes_actions():
    rt = runtime()
    async def invoke(name, params):
        rt.calls.append((name, params))
        return {"status": "ok", "data": {"messages": [{"message_id": 999, "message_seq": 7, "message_type": "private", "user_id": 22}]}}
    rt.invoke = invoke
    action, params = asyncio.run(MilkyBridge(rt).prepare("get_message", {"account": "11", "message_scene": "friend", "peer_id": 22, "message_seq": 7}))
    assert action == "get_msg" and params["message_id"] == 999
    assert rt.calls[0][0] == "get_friend_msg_history"


def test_send_enrich_uses_actual_receipt():
    rt = runtime(msg())
    async def invoke(name, params):
        rt.calls.append((name, params))
        return {"status": "ok", "data": {"message_id": 9001, "message_type": "group", "group_id": 22, "message_seq": 7, "time": 1234}}
    rt.invoke = invoke
    result = asyncio.run(MilkyBridge(rt).enrich("send_group_message", {"account": "11", "group_id": 22}, {"status": "ok", "data": {"message_id": 9001}}))
    assert result["data"]["message_seq"] == 7
    assert result["data"]["time"] == 1234
    assert rt.calls[0][0] == "get_msg"


def test_server_predecode_and_api_formatter_and_auth():
    rt = runtime(msg())
    seen = []
    adapter = SimpleNamespace(format_api_reply=lambda api, result: {"api": api, "result": result})
    def decode(*args):
        seen.append(args)
        raise AssertionError("resolved message APIs must precede decode")
    pipelines = SimpleNamespace(protocol=lambda _: adapter, decode_action=decode)
    app = FastAPI()
    register_milky_routes(app, rt, SimpleNamespace(pipelines=pipelines), lambda req: req.headers.get("authorization") == "Bearer test")
    with TestClient(app) as client:
        assert client.post("/MILKY/api/recall_group_message", json={}).status_code == 401
        response = client.post("/MILKY/api/recall_group_message", headers={"authorization": "Bearer test"}, json={"group_id": 22, "message_seq": 7})
    assert response.json()["api"] == "recall_group_message"
    assert rt.calls == [("delete_msg", {"account": "11", "message_id": 9001})]
    assert not seen

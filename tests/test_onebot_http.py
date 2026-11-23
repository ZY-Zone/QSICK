from __future__ import annotations

import httpx
import pytest
from starlette.testclient import TestClient

from qsick.config import RuntimeConfig
from qsick.onebot.server import create_app
from qsick.runtime import Runtime


async def _client(runtime) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=create_app(runtime))
    return httpx.AsyncClient(transport=transport, base_url="http://onebot")


async def test_post_action_ok(runtime):
    async with await _client(runtime) as client:
        response = await client.post(
            "/ONEBOTV11/", json={"action": "send_group_msg", "params": {"group_id": 30001, "message": "hi"}, "echo": 7}
        )
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["echo"] == 7
    assert payload["data"]["message_id"] > 0


async def test_get_action_ok(runtime):
    async with await _client(runtime) as client:
        response = await client.get("/ONEBOTV11/", params={"action": "get_login_info"})
    payload = response.json()
    assert payload["data"]["user_id"] == 10001


async def test_unknown_action_uses_http_200_with_retcode(runtime):
    async with await _client(runtime) as client:
        response = await client.post("/ONEBOTV11/", json={"action": "nope", "params": {}})
    assert response.status_code == 200
    assert response.json()["retcode"] == 1404


async def test_access_token_is_enforced(config: RuntimeConfig):
    config.onebot.access_token = "secret"
    rt = Runtime(config)
    try:
        async with await _client(rt) as client:
            denied = await client.post("/ONEBOTV11/", json={"action": "get_status", "params": {}})
            allowed = await client.post(
                "/ONEBOTV11/", json={"action": "get_status", "params": {}}, params={"access_token": "secret"}
            )
        assert denied.status_code == 401
        assert allowed.status_code == 200
        assert allowed.json()["status"] == "ok"
    finally:
        await rt.stop()


async def test_bearer_header_is_accepted(config: RuntimeConfig):
    config.onebot.access_token = "secret"
    rt = Runtime(config)
    try:
        async with await _client(rt) as client:
            response = await client.post(
                "/ONEBOTV11/",
                json={"action": "get_status", "params": {}},
                headers={"Authorization": "Bearer secret"},
            )
        assert response.status_code == 200
        assert response.json()["status"] == "ok"
    finally:
        await rt.stop()


async def test_wrong_token_length_is_rejected(config: RuntimeConfig):
    config.onebot.access_token = "secret"
    rt = Runtime(config)
    try:
        async with await _client(rt) as client:
            response = await client.post("/ONEBOTV11/", json={"action": "get_status", "params": {}}, params={"access_token": "secr"})
        assert response.status_code == 401
    finally:
        await rt.stop()


async def test_invalid_json_body(runtime):
    async with await _client(runtime) as client:
        response = await client.post("/ONEBOTV11/", content=b"not json", headers={"content-type": "application/json"})
    assert response.status_code == 400


def test_websocket_action_roundtrip(config: RuntimeConfig):
    rt = Runtime(config)
    app = create_app(rt)
    with TestClient(app) as client:
        with client.websocket_connect("/ONEBOTV11/ws") as ws:
            hello = ws.receive_json()
            assert hello["meta_event_type"] == "lifecycle"
            ws.send_json({"action": "get_login_info", "params": {}, "echo": "x"})
            reply = ws.receive_json()
            assert reply["status"] == "ok"
            assert reply["echo"] == "x"
            assert reply["data"]["user_id"] == 10001


def test_websocket_rejects_bad_token(config: RuntimeConfig):
    config.onebot.access_token = "secret"
    rt = Runtime(config)
    app = create_app(rt)
    with TestClient(app) as client:
        with pytest.raises(Exception):
            with client.websocket_connect("/ONEBOTV11/ws?access_token=wrong") as ws:
                ws.receive_json()

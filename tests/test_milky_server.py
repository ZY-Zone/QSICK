"""Milky routes: `POST {base}/api/{api}` and `{base}/event`, without a protocol prefix.

The listener's profile selects the protocol. These tests exercise routing and dispatch; the Milky
wire shapes themselves live in `test_protocol_milky.py`.
"""
from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
for entry in (str(ROOT), str(ROOT / "src")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from qsick.onebot.server import create_app  # noqa: E402


def app_for(runtime, protocol="milky", base="/"):
    runtime.config.onebot.http_protocol = protocol
    runtime.config.onebot.ws_protocol = protocol
    runtime.config.onebot.enable_http = True
    runtime.config.onebot.enable_ws = True
    runtime.config.onebot.enable_sse = True
    runtime.config.onebot.http_path = base
    runtime.config.onebot.sse_path = base if protocol != "milky" else (
        "/event" if base == "/" else base.rstrip("/") + "/event")
    runtime.config.onebot.ws_path = "/ws" if protocol != "milky" else runtime.config.onebot.sse_path
    return create_app(runtime)


def client_for(runtime, protocol="milky") -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_for(runtime, protocol)), base_url="http://onebot")


class TestTheApiRoute:
    async def test_a_known_api_answers_in_the_milky_envelope(self, runtime):
        async with client_for(runtime) as web:
            response = await web.post("/api/get_login_info", json={})
        assert response.status_code == 200
        assert {"status", "retcode", "data"} <= set(response.json())

    async def test_an_unimplemented_api_is_refused_not_silently_successful(self, runtime):
        async with client_for(runtime) as web:
            response = await web.post("/api/get_peer_pins", json={})
        body = response.json()
        assert body["status"] == "failed"
        assert body["retcode"] == -400
        assert "置顶" in body["message"]

    async def test_a_locally_answered_api_never_reaches_the_action_layer(self, runtime):
        async with client_for(runtime) as web:
            response = await web.post("/api/get_impl_info", json={})
        body = response.json()
        assert body["status"] == "ok"
        assert body["data"]["impl_name"] == "QSICK"
        assert body["data"]["protocol_name"] == "Milky"

    async def test_an_api_outside_the_spec_gets_minus_400(self, runtime):
        async with client_for(runtime) as web:
            response = await web.post("/api/not_a_real_api", json={})
        assert response.json()["retcode"] == -400

    async def test_a_body_that_is_not_an_object_is_refused(self, runtime):
        async with client_for(runtime) as web:
            response = await web.post("/api/get_login_info", content=b"[1,2,3]",
                                      headers={"Content-Type": "application/json"})
        assert response.status_code == 400
        assert response.json()["retcode"] == -400

    async def test_invalid_json_is_refused(self, runtime):
        async with client_for(runtime) as web:
            response = await web.post("/api/get_login_info", content=b"{not json",
                                      headers={"Content-Type": "application/json"})
        assert response.status_code == 400

    async def test_an_empty_body_is_allowed(self, runtime):
        async with client_for(runtime) as web:
            response = await web.post("/api/get_login_info", content=b"")
        assert response.status_code == 200

    async def test_the_call_really_goes_through_invoke(self, runtime):
        seen: list[str] = []
        original = runtime.invoke

        async def spy(action, params=None):
            seen.append(action)
            return await original(action, params)

        runtime.invoke = spy  # type: ignore[method-assign]
        try:
            async with client_for(runtime) as web:
                await web.post("/api/get_friend_list", json={})
        finally:
            runtime.invoke = original  # type: ignore[method-assign]
        assert seen == ["get_friend_list"], seen

    async def test_the_configured_base_applies_to_the_api_too(self, runtime):
        """The path field is not only an SSE setting: it prefixes Milky actions as well."""
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app_for(runtime, "milky", "/bot")),
                base_url="http://onebot") as web:
            response = await web.post("/bot/api/get_login_info", json={})
        assert response.status_code == 200
        assert response.json()["retcode"] == 0


class TestTheEventRoute:
    async def test_sse_frames_are_named_per_the_spec(self, runtime):
        source = (ROOT / "src" / "qsick" / "onebot" / "milky_server.py").read_text(encoding="utf-8")
        assert "event: {SSE_EVENT_NAME}" in source
        assert 'SSE_EVENT_NAME = "milky_event"' in source

    async def test_a_connection_on_the_event_path_speaks_milky(self, runtime):
        connection = runtime.hub.connection("sse", "/event", protocol="milky")
        assert connection.protocol == "milky"

    async def test_the_hub_routes_events_to_it_as_milky(self, runtime):
        connection = runtime.hub.connection("sse", "/event", protocol="milky")
        queue = runtime.hub.add_sse(connection)
        try:
            frames = runtime.hub._encode_by_protocol({
                "post_type": "message", "message_type": "group", "time": 1, "self_id": "1",
                "group_id": 7, "user_id": 100, "message_seq": 1, "message_id": 2,
                "sender": {"user_id": 100, "nickname": "A"},
                "message": [{"type": "text", "data": {"text": "hi"}}],
            })
        finally:
            runtime.hub.remove_sse(queue)
        assert "milky" in frames
        import json
        assert json.loads(frames["milky"][0])["event_type"] == "message_receive"


class TestTheV11RouteIsIndependent:
    async def test_the_v11_action_endpoint_still_answers(self, runtime):
        async with client_for(runtime, "onebotv11") as web:
            response = await web.post("/", json={"action": "get_login_info", "params": {}})
        assert response.status_code == 200
        assert "retcode" in response.json()


@pytest.mark.parametrize("path", ["/api/{api}", "/event"])
def test_both_milky_paths_are_registered(runtime, path):
    found = {getattr(route, "path", "") for route in app_for(runtime).routes}
    assert path in found, f"{path} is not mounted; mounted: {sorted(found)}"


class TestRoutes:
    def test_the_helper_normalizes_paths(self):
        from qsick.onebot.server import route_path

        assert route_path("/ws") == "/ws"
        assert route_path("ws") == "/ws"
        assert route_path("/") == "/"

    def test_v11_routes_have_no_protocol_prefix(self, runtime):
        paths = {getattr(route, "path", "") for route in app_for(runtime, "onebotv11").routes}
        for expected in ("/ws", "/", "/health", "/action"):
            assert expected in paths, f"{expected} missing from {sorted(paths)}"
        assert not any(path.startswith("/ONEBOTV11") for path in paths)

    def test_milky_routes_have_no_protocol_prefix(self, runtime):
        paths = {getattr(route, "path", "") for route in app_for(runtime).routes}
        assert "/api/{api}" in paths
        assert "/event" in paths
        assert not any(path.startswith("/MILKY") for path in paths)
        # Selecting Milky does not also mount the v11 action aliases.
        assert "/action" not in paths

    async def test_health_reports_the_selected_protocol(self, runtime):
        async with client_for(runtime, "onebotv11") as web:
            response = await web.get("/health")
        assert response.json()["protocol"] == "onebotv11"

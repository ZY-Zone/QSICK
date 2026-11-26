"""Milky 的 HTTP 端点：`POST /MILKY/api/{api}` 与 `GET /MILKY/event`。

测的是"路由是否真的把调用送到了分流点"，不是 Milky 规范本身——信封与事件形状的细节在
`test_protocol_milky.py` 里按适配器测。这里要防的是接线错：路径挂错、鉴权漏掉、或者调用
根本没经过 `runtime.invoke`（那样插件过滤与账号限制就被跳过了）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from qsick.onebot.server import create_app  # noqa: E402


def client_for(runtime) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(runtime)), base_url="http://onebot")


class TestTheApiRoute:
    async def test_a_known_api_answers_in_the_milky_envelope(self, runtime):
        async with client_for(runtime) as web:
            response = await web.post("/MILKY/api/get_login_info", json={})
        assert response.status_code == 200
        body = response.json()
        # Milky 的信封字段，一个都不能少。
        assert {"status", "retcode", "data"} <= set(body)

    async def test_an_unimplemented_api_is_refused_not_silently_successful(self, runtime):
        """客户端收到 200 + retcode 0 却什么都没发生，比明确的失败坏得多。

        用 `get_peer_pins`：QQ 侧没有读取置顶列表的接口，所以它连动作名都没有。
        """
        async with client_for(runtime) as web:
            response = await web.post("/MILKY/api/get_peer_pins", json={})
        body = response.json()
        assert body["status"] == "failed"
        assert body["retcode"] == -400
        assert "置顶" in body["message"]

    async def test_a_locally_answered_api_never_reaches_the_action_layer(self, runtime):
        """`get_impl_info` 问的是"你是哪个协议端"，答案在本进程里，去问 QQ 反而错了。"""
        async with client_for(runtime) as web:
            response = await web.post("/MILKY/api/get_impl_info", json={})
        body = response.json()
        assert body["status"] == "ok"
        assert body["data"]["impl_name"] == "QSICK"
        assert body["data"]["protocol_name"] == "Milky"

    async def test_an_api_outside_the_spec_gets_minus_400(self, runtime):
        async with client_for(runtime) as web:
            response = await web.post("/MILKY/api/not_a_real_api", json={})
        assert response.json()["retcode"] == -400

    async def test_a_body_that_is_not_an_object_is_refused(self, runtime):
        async with client_for(runtime) as web:
            response = await web.post("/MILKY/api/get_login_info", content=b"[1,2,3]",
                                      headers={"Content-Type": "application/json"})
        assert response.status_code == 400
        assert response.json()["retcode"] == -400

    async def test_invalid_json_is_refused(self, runtime):
        async with client_for(runtime) as web:
            response = await web.post("/MILKY/api/get_login_info", content=b"{not json",
                                      headers={"Content-Type": "application/json"})
        assert response.status_code == 400

    async def test_an_empty_body_is_allowed(self, runtime):
        # Milky 明确要求无参数也必须发 `{}`，但一个空 body 不该直接炸——容错比严格更有用。
        async with client_for(runtime) as web:
            response = await web.post("/MILKY/api/get_login_info", content=b"")
        assert response.status_code == 200

    async def test_the_call_really_goes_through_invoke(self, runtime):
        """绕开 invoke 就等于把 core 插件的出入限制关掉。

        这里直接盯一眼 `invoke` 有没有被调用：它是唯一带插件过滤与账号检查的入口。
        """
        seen: list[str] = []
        original = runtime.invoke

        async def spy(action, params=None):
            seen.append(action)
            return await original(action, params)

        runtime.invoke = spy  # type: ignore[method-assign]
        try:
            async with client_for(runtime) as web:
                await web.post("/MILKY/api/get_friend_list", json={})
        finally:
            runtime.invoke = original  # type: ignore[method-assign]
        assert seen == ["get_friend_list"], seen


class TestTheEventRoute:
    async def test_sse_frames_are_named_per_the_spec(self, runtime):
        """Milky 的 SSE 帧必须带 `event: milky_event`；裸 `data:` 是 v11 的装帧。"""
        source = (ROOT / "src" / "qsick" / "onebot" / "milky_server.py").read_text(encoding="utf-8")
        assert "event: {SSE_EVENT_NAME}" in source
        assert 'SSE_EVENT_NAME = "milky_event"' in source

    async def test_a_connection_on_the_event_path_speaks_milky_whatever_the_profiles_say(self, runtime):
        """`/event` 的协议由路径决定，不查配置。

        反查只可能查错：配置里可能一条 milky 都没有，而这个端点本身就是 Milky 的。
        """
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

        # One protocol maps to a list of frames: one internal event can be several wire events.
        assert json.loads(frames["milky"][0])["event_type"] == "message_receive"


class TestTheV11RouteIsUntouched:
    async def test_the_v11_action_endpoint_still_answers(self, runtime):
        """两种协议共用端口，v11 的 `/ONEBOTV11/` 不能被 Milky 的路由抢走。"""
        async with client_for(runtime) as web:
            response = await web.post("/ONEBOTV11/", json={"action": "get_login_info", "params": {}})
        assert response.status_code == 200
        # v11 的信封字段名是 `post_type` 那套事件之外的另一套：status/retcode/data。
        assert "retcode" in response.json()
        assert "echo" not in response.json() or response.json()["echo"] is None


@pytest.mark.parametrize("path", ["/MILKY/api/{api}", "/MILKY/event"])
def test_both_milky_paths_are_registered(runtime, path):
    # `/api/{api}` is the route template: the API name is a path parameter, so this is what appears in the
    # route table. Asserting the concrete URL would only test that the openapi route pattern is not a template.
    app = create_app(runtime)
    found = {getattr(route, "path", "") for route in app.routes}
    assert path in found, f"{path} is not mounted; mounted: {sorted(found)}"


class TestPrefixes:
    """每条协议一个路径前缀，路径本身就是协议声明。

    这是比分流点里的 profile 反查更可靠的一层：前缀是请求自己带的信息，不依赖配置里恰好只有
    一条匹配的记录。
    """

    def test_the_helper_builds_both_shape(self):
        from qsick.onebot.server import MILKY_PREFIX, ONEBOTV11_PREFIX, prefixed

        assert prefixed(ONEBOTV11_PREFIX, "/ws") == "/ONEBOTV11/ws"
        # 根路径要保住尾斜杠：`/ONEBOTV11` 与 `/ONEBOTV11/` 是两个不同的路径。
        assert prefixed(ONEBOTV11_PREFIX, "/") == "/ONEBOTV11/"
        assert prefixed(MILKY_PREFIX, "/event") == "/MILKY/event"

    def test_a_path_without_a_leading_slash_still_works(self):
        from qsick.onebot.server import ONEBOTV11_PREFIX, prefixed

        assert prefixed(ONEBOTV11_PREFIX, "ws") == "/ONEBOTV11/ws"

    def test_both_protocols_are_mounted_under_their_own_prefix(self, runtime):
        app = create_app(runtime)
        paths = {getattr(route, "path", "") for route in app.routes}
        # SSE is not a path of its own: OneBot v11 serves the event stream on the HTTP endpoint
        # itself (POST = action, GET = stream), so it is mounted at `/ONEBOTV11/` here rather than
        # at `/ONEBOTV11/sse`. `test_sse_path.py` pins that arrangement.
        for expected in ("/ONEBOTV11/ws", "/ONEBOTV11/", "/ONEBOTV11/health",
                         "/ONEBOTV11/action", "/MILKY/api/{api}", "/MILKY/event"):
            assert expected in paths, f"{expected} missing from {sorted(paths)}"

    def test_the_unprefixed_paths_are_gone(self, runtime):
        """`都改为` 是明确的：旧的裸路径不再保留别名。

        多一个别名就多一条会被误用的路径——客户端分不清自己连的是哪套协议。
        """
        app = create_app(runtime)
        paths = {getattr(route, "path", "") for route in app.routes}
        for legacy in ("/ws", "/sse", "/event", "/api/{api}"):
            assert legacy not in paths, f"{legacy} should not exist any more"

    async def test_the_v11_root_answers_at_its_prefix(self, runtime):
        async with client_for(runtime) as web:
            response = await web.post("/ONEBOTV11/", json={"action": "get_login_info", "params": {}})
        assert response.status_code == 200

    async def test_health_reports_which_protocol_it_belongs_to(self, runtime):
        async with client_for(runtime) as web:
            response = await web.get("/ONEBOTV11/health")
        assert response.json()["protocol"] == "onebotv11"

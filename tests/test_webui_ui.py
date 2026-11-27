from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from qsick.config import RuntimeConfig
from qsick.runtime import Runtime
from qsick.webui.app import create_app


async def make_runtime(tmp_path: Path, token: str = "") -> Runtime:
    config = RuntimeConfig(data_dir=str(tmp_path / "data"))
    config.transport.mode = "mock"
    config.webui.access_token = token
    config.plugins.dirs = [str(Path(__file__).resolve().parents[1] / "plugins")]
    runtime = Runtime(config)
    await runtime.start(serve=False)
    return runtime


def client(runtime: Runtime) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(runtime)), base_url="http://ui")


@pytest.fixture()
async def runtime(tmp_path: Path):
    instance = await make_runtime(tmp_path)
    try:
        yield instance
    finally:
        await instance.stop()


async def test_pages_render_with_navigation(runtime: Runtime):
    async with client(runtime) as web:
        home = await web.get("/")
        assert home.status_code == 200
        assert "总览" in home.text and "QSICK" in home.text
        assert "/static/logo.png" in home.text

        logo = await web.get("/static/logo.png")
        assert logo.status_code == 200
        assert logo.headers["content-type"].startswith("image/png")

        logs = await web.get("/logs")
        assert logs.status_code == 200 and "实时日志" in logs.text

        market = await web.get("/plugins/market")
        assert runtime.config.plugins.market_url in market.text



async def test_plugin_config_widgets(runtime: Runtime):
    async with client(runtime) as web:
        page = await web.get("/plugins/guard/config")
        assert page.status_code == 200
        assert '<select name="cfg_mode">' in page.text
        assert 'name="cfg_log_drops" value="true"' in page.text
        assert '<textarea name="cfg_groups"' in page.text

        saved = await web.post(
            "/plugins/guard/config",
            data={
                "enabled": "on",
                "cfg_mode": "whitelist",
                "cfg_scope": "both",
                "cfg_groups": '{"whitelist": [], "blacklist": []}',
                "cfg_users": '{"whitelist": [], "blacklist": []}',
                "cfg_always_allow_users": "[]",
            },
            follow_redirects=False,
        )
        assert saved.status_code == 303
        config = await web.get("/api/plugins/guard/config")
        assert config.json()["mode"] == "whitelist"
        assert config.json()["log_drops"] is False

async def test_favicon_and_theme_assets(runtime: Runtime):
    async with client(runtime) as web:
        icon = await web.get("/favicon.ico")
        assert icon.status_code == 200
        assert "svg" in icon.headers["content-type"]
        css = await web.get("/static/app.css")
        assert css.status_code == 200
        assert "color-scheme: light" in css.text
        assert "--sky: #0ea5e9" in css.text


async def test_action_detail_api(runtime: Runtime):
    async with client(runtime) as web:
        response = await web.get("/api/actions/send_group_msg")
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "send_group_msg"
    assert any(param["name"] == "group_id" for param in body["params"])


async def test_token_redirects_pages_to_login(tmp_path: Path):
    runtime = await make_runtime(tmp_path, token="s3cret")
    try:
        async with client(runtime) as web:
            page = await web.get("/", follow_redirects=False)
            assert page.status_code == 303
            assert page.headers["location"].startswith("/login")

            login = await web.get("/login")
            assert login.status_code == 200 and "访问 token" in login.text

            bad = await web.post("/login", data={"token": "nope", "next": "/"}, follow_redirects=False)
            assert bad.status_code == 401

            good = await web.post("/login", data={"token": "s3cret", "next": "/"}, follow_redirects=False)
            assert good.status_code == 303
            assert "qsick_token" in good.headers.get("set-cookie", "")

            authed = await web.get("/", cookies={"qsick_token": "s3cret"})
            assert authed.status_code == 200

            web.cookies.clear()
            api = await web.get("/api/actions")
            assert api.status_code == 401
            allowed = await web.get("/api/actions", cookies={"qsick_token": "s3cret"})
            assert allowed.status_code == 200
    finally:
        await runtime.stop()


async def test_last_error_is_reported_but_no_longer_a_dashboard_tile(runtime: Runtime):
    """`last_error` stays in the API; the tile is gone.

    The tile spent most of its life showing "—", it duplicated what the log page answers properly, and on the
    compressed overview it would have cost a slot in a two-row strip. The *data* is still worth having for
    consumers that poll /api/status, so only the presentation went.
    """
    from qsick import log as logmod

    logmod.get_logger("Test").error("dashboard failure", code=500)
    async with client(runtime) as web:
        status = await web.get("/api/status")
        assert status.json()["last_error"]["message"] == "dashboard failure"
        home = await web.get("/")
        assert "最后报错" not in home.text


async def test_status_and_metrics(runtime: Runtime):
    async with client(runtime) as web:
        status = await web.get("/api/status")
        assert status.status_code == 200
        body = status.json()
        assert body["actions"] >= 200
        assert "payload" in body or body["transport"] == "mock"

        metrics = await web.get("/metrics")
        assert metrics.status_code == 200
        assert "qsick_up 1" in metrics.text
        assert "qsick_actions" in metrics.text


async def test_messages_search_and_json(runtime: Runtime):
    import time as time_module

    from qsick.protocol.elements import normalize_all
    from qsick.protocol.message import Message

    message = Message(
        message_id=9001,
        seq=1,
        time=int(time_module.time()),
        chat_type="group",
        chat_id=30001,
        sender_uin=10001,
        sender_name="tester",
        elements=normalize_all([{"type": "text", "data": {"text": "needle in cache"}}]),
    )
    # The page reads the cache store: the cache records forwarded messages itself and does not depend on
    # the per-account instance stores.
    runtime.cache_store.put(message)
    async with client(runtime) as web:
        page = await web.get("/messages", params={"q": "needle"})
        assert page.status_code == 200 and "needle in cache" in page.text
        empty = await web.get("/messages", params={"q": "zzz-not-there"})
        assert empty.status_code == 200 and "没有匹配的消息" in empty.text

        payload = await web.get("/api/messages", params={"q": "needle"})
        assert payload.status_code == 200
        assert payload.json()["total"] == 1


async def test_log_clear_and_download(runtime: Runtime):
    async with client(runtime) as web:
        cleared = await web.post("/api/logs/clear")
        assert cleared.json()["ok"] is True
        download = await web.get("/api/logs/download")
        assert download.status_code == 200
        assert "attachment" in download.headers["content-disposition"]


async def test_settings_forms(runtime: Runtime):
    async with client(runtime) as web:
        response = await web.post("/settings/log", data={"level": "DEBUG"}, follow_redirects=False)
        assert response.status_code == 303
        assert runtime.config.log.level == "DEBUG"

        storage = await web.post(
            "/settings/storage", data={"max_messages": "1234", "max_days": "3"}, follow_redirects=False
        )
        assert storage.status_code == 303
        assert runtime.config.rotation.max_messages == 1234
        assert runtime.config.rotation.max_days == 3

        page = await web.get("/settings")
        assert page.status_code == 200 and "消息保留" in page.text


async def test_unknown_page_renders_404(runtime: Runtime):
    async with client(runtime) as web:
        response = await web.get("/definitely-not-a-page")
    assert response.status_code == 404
    assert "页面不存在" in response.text


async def test_gzip_and_security_headers(runtime: Runtime):
    async with client(runtime) as web:
        response = await web.get("/network", headers={"Accept-Encoding": "gzip"})
    assert response.status_code == 200
    assert response.headers.get("x-content-type-options") == "nosniff"
    assert response.headers.get("content-encoding") == "gzip"


async def test_network_page_and_save(tmp_path: Path, monkeypatch):
    import json

    config_path = tmp_path / "qsick.json"
    monkeypatch.setenv("QSICK_CONFIG", str(config_path))
    runtime = await make_runtime(tmp_path)
    try:
        async with client(runtime) as web:
            page = await web.get("/network")
            assert page.status_code == 200
            assert 'id="profile-modal"' in page.text
            assert 'data-tab="http"' in page.text
            assert 'data-tab="ws_client"' in page.text
            assert 'data-tab="ws_server"' in page.text

            response = await web.post(
                "/network",
                data={
                    "host": "127.0.0.1",
                    "port": "3001",
                    "enable_http": "on",
                    "enable_ws": "on",
                    "http_path": "/",
                    "ws_path": "/ws",
                    "sse_path": "/sse",
                    "reverse_ws_urls": "ws://127.0.0.1:8080/onebot\nwss://example.test/ws",
                    "reverse_reconnect_seconds": "2.5",
                    "webui_host": "127.0.0.1",
                    "webui_port": "5754",
                },
                follow_redirects=False,
            )
            assert response.status_code == 303
        saved = json.loads(config_path.read_text(encoding="utf-8"))
        assert saved["onebot"]["port"] == 3001
        assert saved["onebot"]["reverse_ws_urls"] == [
            "ws://127.0.0.1:8080/onebot",
            "wss://example.test/ws",
        ]
        assert runtime.config.onebot.reverse_reconnect_seconds == 2.5

        async with client(runtime) as web:
            bad = await web.post("/network", data={"port": "70000", "host": "x"}, follow_redirects=False)
            assert bad.status_code == 400
            assert "端口" in bad.text

            runtime.config.onebot.access_token = "secret"
            api = await web.get("/api/network")
            assert api.json()["onebot"]["access_token"] == "***"
    finally:
        await runtime.stop()


async def test_network_profiles_api(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("QSICK_CONFIG", str(tmp_path / "qsick.json"))
    runtime = await make_runtime(tmp_path)
    try:
        async with client(runtime) as web:
            created = await web.post(
                "/api/network/profiles",
                json={
                    "action": "upsert",
                    "kind": "ws_client",
                    "name": "Test Client",
                    "url": "ws://127.0.0.1:9000/ws",
                    "enabled": False,
                },
            )
            assert created.status_code == 200
            profile = created.json()["profiles"][-1]
            assert profile["name"] == "Test Client"

            page = await web.get("/network")
            assert "Test Client" in page.text

            toggled = await web.post(
                "/api/network/profiles",
                json={"action": "toggle", "id": profile["id"], "enabled": True},
            )
            assert toggled.status_code == 200

            removed = await web.post(
                "/api/network/profiles",
                json={"action": "delete", "id": profile["id"]},
            )
            assert removed.status_code == 200
            assert all(item["id"] != profile["id"] for item in removed.json()["profiles"])
    finally:
        await runtime.stop()


async def test_network_change_applies_without_recreating_app(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("QSICK_CONFIG", str(tmp_path / "qsick.json"))
    runtime = await make_runtime(tmp_path)
    app = create_app(runtime)
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://ui") as web:
            page = await web.get("/network")
            assert page.status_code == 200
            response = await web.post(
                "/api/network",
                json={"port": "3002", "host": "127.0.0.1", "reverse_ws_urls": []},
            )
            assert response.status_code == 200
            body = response.json()
            assert body["ok"] is True
            assert "applied" in body
            assert body["webui_port_restart_required"] is True
            assert runtime.config.onebot.port == 3002
    finally:
        await runtime.stop()


async def test_webui_token_change_is_live(tmp_path: Path):
    runtime = await make_runtime(tmp_path, token="")
    try:
        app = create_app(runtime)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://ui") as web:
            assert (await web.get("/")).status_code == 200
            runtime.config.webui.access_token = "live-token"
            assert (await web.get("/", follow_redirects=False)).status_code == 303
            assert (await web.get("/", cookies={"qsick_token": "live-token"})).status_code == 200
    finally:
        await runtime.stop()


async def test_the_chat_page_is_gone(tmp_path: Path, monkeypatch):
    """聊天栏已移除：页面、导航项、启停路由都不应再存在。

    ``/api/chat/*`` 下留了三个 group-only 老端点给外部调用方，所以这里只断言
    ``/chat`` 本身是 404，而不是整个前缀消失。
    """
    monkeypatch.setenv("QSICK_CONFIG", str(tmp_path / "qsick.json"))
    runtime = await make_runtime(tmp_path)
    try:
        async with client(runtime) as web:
            assert (await web.get("/chat", follow_redirects=False)).status_code == 404
            assert (await web.post("/chat/enable", follow_redirects=False)).status_code in (404, 405)
            home = await web.get("/")
            assert 'href="/chat"' not in home.text
            assert "待启用" not in home.text
    finally:
        await runtime.stop()


async def test_the_topbar_quit_button_shuts_the_runtime_down(runtime: Runtime):
    """红色「退出」关闭的是进程，不是会话：POST /shutdown 后 runtime.wait() 会返回。"""
    async with client(runtime) as web:
        page = await web.get("/")
        assert "quit-btn" in page.text
        assert "退出" in page.text

        reply = await web.post("/shutdown")
        assert reply.status_code == 200
        assert "QSICK 已退出" in reply.text

    # The page is flushed before the teardown starts: the stop event is set on a short timer.
    await asyncio.wait_for(runtime.wait(), timeout=5)

async def test_the_retained_chat_send_endpoint_is_still_gated(tmp_path: Path, monkeypatch):
    """保留的 ``/api/chat/send`` 仍受 ``webui.chat_enabled`` 控制。

    聊天栏没了，但这个端点能替登录的号发言，所以开关必须还在、默认仍关。
    """
    monkeypatch.setenv("QSICK_CONFIG", str(tmp_path / "qsick.json"))
    runtime = await make_runtime(tmp_path)
    try:
        async with client(runtime) as web:
            blocked = await web.post(
                "/api/chat/send", json={"group_id": 30001, "text": "hi"}, follow_redirects=False
            )
            assert blocked.status_code == 403

            runtime.config.webui.chat_enabled = True
            groups = await web.get("/api/chat/groups")
            assert groups.status_code == 200 and groups.json()["groups"]

            sent = await web.post("/api/chat/send", json={"group_id": 30001, "text": "hi"})
            assert sent.status_code == 200
            assert sent.json()["status"] == "ok"
    finally:
        await runtime.stop()

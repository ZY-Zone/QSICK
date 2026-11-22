from __future__ import annotations

import json
import shutil
from pathlib import Path

import httpx
import pytest

from qsick.config import RuntimeConfig
from qsick.runtime import Runtime
from qsick.webui.app import create_app

GUARD = Path(__file__).resolve().parents[1] / "plugins" / "guard"


def plugins_dir(tmp_path: Path) -> Path:
    target = tmp_path / "plugins"
    shutil.copytree(GUARD, target / "guard")
    return target


async def client(runtime: Runtime) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=create_app(runtime))
    return httpx.AsyncClient(transport=transport, base_url="http://webui")


@pytest.fixture()
async def plugin_runtime(tmp_path: Path):
    config = RuntimeConfig(data_dir=str(tmp_path / "data"))
    config.transport.mode = "mock"
    config.plugins.dirs = [str(plugins_dir(tmp_path))]
    config.plugins.watch = False
    runtime = Runtime(config)
    await runtime.start(serve=False)
    try:
        yield runtime
    finally:
        await runtime.stop()


async def test_plugin_list_shows_three_actions(plugin_runtime: Runtime):
    async with await client(plugin_runtime) as web:
        page = await web.get("/plugins")
    assert page.status_code == 200
    text = page.text
    assert "消息白名单" in text
    assert "配置" in text and "卸载" in text and "更新" in text
    assert "插件市场" in text


async def test_default_config_page_lists_fields_and_enable_toggle(plugin_runtime: Runtime):
    async with await client(plugin_runtime) as web:
        page = await web.get("/plugins/guard/config")
    assert page.status_code == 200
    text = page.text
    for key in ("mode", "scope", "groups", "users", "always_allow_users", "log_drops"):
        assert f"cfg_{key}" in text
    assert "启用该插件" in text


async def test_saving_config_writes_json_and_keeps_enabled(plugin_runtime: Runtime):
    async with await client(plugin_runtime) as web:
        response = await web.post(
            "/plugins/guard/config",
            data={
                "enabled": "on",
                "cfg_mode": "whitelist",
                "cfg_scope": "group",
                "cfg_groups": '{"whitelist": [30001], "blacklist": []}',
                "cfg_users": '{"whitelist": [], "blacklist": []}',
                "cfg_always_allow_users": "[]",
                "cfg_log_drops": "false",
            },
            follow_redirects=False,
        )
    assert response.status_code == 303
    path = plugin_runtime.plugins.config_path("guard")
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["mode"] == "whitelist"
    assert saved["scope"] == "group"
    assert saved["groups"]["whitelist"] == [30001]
    assert saved["log_drops"] is False
    assert "guard" in plugin_runtime.plugins._loaded


async def test_reset_plugin_config(plugin_runtime: Runtime):
    path = plugin_runtime.plugins.config_path("guard")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"mode": "whitelist"}', encoding="utf-8")
    async with await client(plugin_runtime) as web:
        page = await web.get("/plugins/guard/config")
        assert "恢复默认" in page.text
        response = await web.post("/plugins/guard/config/reset", follow_redirects=False)
    assert response.status_code == 303
    # The plugin may re-materialise its defaults on load; effective config must reset.
    assert plugin_runtime.plugins.plugin_config("guard")["mode"] == "blacklist"
    assert "guard" in plugin_runtime.plugins._loaded


async def test_disable_plugin_from_config_page(plugin_runtime: Runtime):
    async with await client(plugin_runtime) as web:
        await web.post(
            "/plugins/guard/config",
            data={"cfg_mode": "blacklist", "cfg_groups": "{}", "cfg_users": "{}"},
            follow_redirects=False,
        )
    assert "guard" not in plugin_runtime.plugins._loaded
    assert "guard" in plugin_runtime.plugins._disabled


async def test_update_reloads_plugin(plugin_runtime: Runtime):
    async with await client(plugin_runtime) as web:
        response = await web.post("/plugins/guard/update", follow_redirects=False)
    assert response.status_code == 303
    assert "guard" in plugin_runtime.plugins._loaded


async def test_uninstall_moves_plugin_to_trash(plugin_runtime: Runtime):
    source = Path(plugin_runtime.plugins.dirs[0]) / "guard"
    async with await client(plugin_runtime) as web:
        response = await web.post("/plugins/guard/uninstall", follow_redirects=False)
    assert response.status_code == 303
    assert not source.exists()
    trash = plugin_runtime.config.path("plugins", ".trash")
    assert list(trash.glob("guard-*")), "plugin directory was not moved to trash"
    assert "guard" not in plugin_runtime.plugins._loaded


async def test_market_page_shows_configured_url(plugin_runtime: Runtime):
    async with await client(plugin_runtime) as web:
        page = await web.get("/plugins/market")
    assert page.status_code == 200
    assert plugin_runtime.config.plugins.market_url in page.text
    assert "插件市场" in page.text


async def test_custom_config_page_and_api(tmp_path: Path):
    plugins = tmp_path / "plugins"
    custom = plugins / "customui"
    (custom / "ui").mkdir(parents=True)
    (custom / "basic.json").write_text(
        json.dumps(
            {
                "id": "customui",
                "name": "Custom UI",
                "version": "1.0.0",
                "api": 1,
                "entry": "plugin.py:CustomPlugin",
                "tier": "standard",
                "permissions": ["config.read", "config.write"],
                "config_ui": "custom",
                "config_page": "ui/config.html",
                "config_fields": [{"key": "greeting", "type": "str", "default": "hi"}],
            }
        ),
        encoding="utf-8",
    )
    (custom / "plugin.py").write_text("class CustomPlugin:\n    pass\n", encoding="utf-8")
    (custom / "ui" / "config.html").write_text("<html><body><h1>custom page</h1></body></html>", encoding="utf-8")

    config = RuntimeConfig(data_dir=str(tmp_path / "data"))
    config.transport.mode = "mock"
    config.plugins.dirs = [str(plugins)]
    config.plugins.watch = False
    runtime = Runtime(config)
    await runtime.start(serve=False)
    try:
        async with await client(runtime) as web:
            page = await web.get("/plugins/customui/config")
            assert page.status_code == 200
            assert "/plugins/customui/config/page" in page.text
            assert "iframe" in page.text

            raw = await web.get("/plugins/customui/config/page")
            assert raw.status_code == 200
            assert "custom page" in raw.text

            get_api = await web.get("/api/plugins/customui/config")
            assert get_api.json()["greeting"] == "hi"

            post_api = await web.post("/api/plugins/customui/config", json={"greeting": "hello"})
            assert post_api.json()["ok"] is True
            assert json.loads(Path(runtime.plugins.config_path("customui")).read_text("utf-8"))["greeting"] == "hello"
    finally:
        await runtime.stop()


async def test_custom_page_rejects_path_escape(tmp_path: Path):
    plugins = tmp_path / "plugins"
    custom = plugins / "evil"
    custom.mkdir(parents=True)
    (custom / "basic.json").write_text(
        json.dumps(
            {
                "id": "evil",
                "name": "Evil",
                "version": "1.0.0",
                "api": 1,
                "entry": "plugin.py:Evil",
                "tier": "sandbox",
                "config_ui": "custom",
                "config_page": "../../../etc/passwd",
            }
        ),
        encoding="utf-8",
    )
    (custom / "plugin.py").write_text("class Evil:\n    pass\n", encoding="utf-8")
    config = RuntimeConfig(data_dir=str(tmp_path / "data"))
    config.transport.mode = "mock"
    config.plugins.dirs = [str(plugins)]
    config.plugins.watch = False
    runtime = Runtime(config)
    await runtime.start(serve=False)
    try:
        async with await client(runtime) as web:
            response = await web.get("/plugins/evil/config/page")
        assert response.status_code in (400, 404)
    finally:
        await runtime.stop()

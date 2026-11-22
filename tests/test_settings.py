from __future__ import annotations

import json
from pathlib import Path

import httpx

from qsick.config import RuntimeConfig
from qsick.runtime import Runtime
from qsick.webui.app import create_app


async def _client(runtime: Runtime) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=create_app(runtime))
    return httpx.AsyncClient(transport=transport, base_url="http://webui")


def test_default_pipe_auth_is_random(tmp_path: Path):
    config = RuntimeConfig(data_dir=str(tmp_path))
    assert config.transport.pipe_auth == "random"


async def test_settings_page_renders(tmp_path: Path):
    runtime = Runtime(RuntimeConfig(data_dir=str(tmp_path / "data")))
    async with await _client(runtime) as client:
        response = await client.get("/settings")
    assert response.status_code == 200
    assert "管道鉴权" in response.text
    assert "随机一次性 token" in response.text


async def test_saving_fixed_mode_writes_config(tmp_path: Path, monkeypatch):
    config_path = tmp_path / "qsick.json"
    monkeypatch.setenv("QSICK_CONFIG", str(config_path))
    runtime = Runtime(RuntimeConfig(data_dir=str(tmp_path / "data")))
    async with await _client(runtime) as client:
        response = await client.post(
            "/settings/pipe-auth",
            data={"pipe_auth": "fixed", "pipe_token": "deadbeef"},
            follow_redirects=False,
        )
    assert response.status_code == 303
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["transport"]["pipe_auth"] == "fixed"
    assert saved["transport"]["pipe_token"] == "deadbeef"
    assert runtime.config.transport.pipe_auth == "fixed"


async def test_saving_fixed_mode_without_token_is_rejected(tmp_path: Path, monkeypatch):
    config_path = tmp_path / "qsick.json"
    monkeypatch.setenv("QSICK_CONFIG", str(config_path))
    runtime = Runtime(RuntimeConfig(data_dir=str(tmp_path / "data")))
    async with await _client(runtime) as client:
        response = await client.post(
            "/settings/pipe-auth",
            data={"pipe_auth": "fixed", "pipe_token": ""},
            follow_redirects=False,
        )
    assert response.status_code == 303
    assert "saved=0" in response.headers["location"]
    assert not config_path.exists() or runtime.config.transport.pipe_auth == "random"


async def test_saving_none_mode_is_allowed(tmp_path: Path, monkeypatch):
    config_path = tmp_path / "qsick.json"
    monkeypatch.setenv("QSICK_CONFIG", str(config_path))
    runtime = Runtime(RuntimeConfig(data_dir=str(tmp_path / "data")))
    async with await _client(runtime) as client:
        await client.post(
            "/settings/pipe-auth",
            data={"pipe_auth": "none", "pipe_token": ""},
            follow_redirects=False,
        )
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["transport"]["pipe_auth"] == "none"

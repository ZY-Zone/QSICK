"""Path rules for server profiles.

A configured path governs every transport on that listener. One URL may carry POST actions, GET
events and a WebSocket upgrade, so HTTP and WS sharing a path is supported rather than an error.
Only enabled transports are checked, and a different port is an independent listener.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for entry in (str(ROOT), str(ROOT / "src")):
    if entry not in sys.path:
        sys.path.insert(0, entry)


@pytest.fixture()
def webui(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from qsick.config import RuntimeConfig
    from qsick.runtime import Runtime
    from qsick.webui.app import create_app

    config = RuntimeConfig(data_dir=str(tmp_path))
    runtime = Runtime(config)
    runtime._serving = True
    monkeypatch.setattr(runtime, "_active_accounts", lambda: ["3905548034"])
    return TestClient(create_app(runtime)), runtime


def save(client, **fields):
    """Saving a server profile actually binds a listener, so the port must be one nobody uses."""
    payload = {"action": "upsert", "protocol": "onebotv11", "instance": "3905548034",
               "enabled": True, "host": "127.0.0.1", "port": 39217}
    payload.update(fields)
    return client.post("/api/network/profiles", json=payload)


def test_a_single_ws_server_at_root_is_accepted(webui):
    """The reported case: one path, typed once, at "/"."""
    client, _ = webui
    response = save(client, kind="ws_server", path="/")
    assert response.status_code == 200, response.text
    assert any(item.get("kind") == "ws_server" and item.get("path") == "/"
               for item in response.json()["profiles"])


def test_a_ws_server_and_an_http_server_may_each_own_a_path(webui):
    """Enabling both is fine as long as the paths differ -- that is the point of the check."""
    client, _ = webui
    assert save(client, kind="ws_server", path="/ws").status_code == 200
    assert save(client, kind="http", path="/http").status_code == 200


def test_two_enabled_transports_may_share_one_path(webui):
    """POST, GET and Upgrade distinguish the transports; the path belongs to all of them."""
    client, runtime = webui
    assert save(client, kind="http", path="/same").status_code == 200
    response = save(client, kind="ws_server", path="/same")
    assert response.status_code == 200, response.text
    assert runtime.config.onebot.http_path == "/same"
    assert runtime.config.onebot.ws_path == "/same"


def test_a_path_with_a_space_is_rejected_by_name(webui):
    """Malformed paths are still rejected, and the message names the offending field."""
    client, _ = webui
    response = save(client, kind="http", path="/bad path")
    assert response.status_code == 400, response.text
    assert "http_path" in response.json()["error"], response.json()["error"]

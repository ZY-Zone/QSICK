"""A lone listener at "/" must save; only *enabled* transports can collide.

The three transports share one listener, so a duplicate path among them is a genuine ambiguity.
A disabled transport contributes nothing, though -- and all `_apply_network` looked at was the
config's stored `http_path`/`sse_path`, which keep their defaults when nothing uses them.

Measured before the fix: creating a single `ws_server` with path `/` was rejected with
"HTTP / WS / SSE paths cannot repeat", because the unused `http_path` was still `/`. The path was
typed exactly once.
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
    payload = {"action": "upsert", "protocol": "onebotv11", "instance": "3905548034",
               "enabled": True, "host": "127.0.0.1", "port": 3099}
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


def test_two_enabled_transports_on_one_path_are_still_rejected(webui):
    """The rule that must survive: both enabled, same path, one listener -- genuinely ambiguous."""
    client, _ = webui
    assert save(client, kind="http", path="/same").status_code == 200
    response = save(client, kind="ws_server", path="/same")
    assert response.status_code == 400, response.text
    assert "重复" in response.json()["error"]


def test_the_error_names_the_path_that_collides(webui):
    """A message that says which value clashed, so the operator is not left guessing."""
    client, _ = webui
    assert save(client, kind="http", path="/dup").status_code == 200
    response = save(client, kind="ws_server", path="/dup")
    assert response.status_code == 400, response.text
    assert "/dup" in response.json()["error"], response.json()["error"]

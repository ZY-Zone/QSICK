"""SSE belongs on the HTTP path, and the checkbox has to mean something.

OneBot v11 puts the action endpoint and the event stream on one URL, separated by method: POST runs
an action, GET streams events. go-cqhttp and NapCat both do this. This project instead invented
`{path}/sse`, and on top of that the "提供 SSE 事件流" checkbox was stored and then never read --
`enable_sse = bool(http)` -- so SSE was on for every http profile no matter what the box said.

Two consequences worth pinning here:
  * the stream answers on the configured path, not on a second one nobody configured
  * GET on that path is the stream, so the query-string form of an action must not also claim it
    (it stays at `/action`, which is the standard address anyway)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for entry in (str(ROOT), str(ROOT / "src")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from qsick.config import RuntimeConfig  # noqa: E402
from qsick.network_profiles import apply_to_config  # noqa: E402


def profile(kind, path, *, port=39218, enabled=True, **extra):
    item = {"id": f"{kind}-1", "kind": kind, "protocol": "onebotv11", "instance": "3905548034",
            "host": "127.0.0.1", "port": port, "path": path, "enabled": enabled, "token": ""}
    item.update(extra)
    return item


def derive(profiles):
    config = RuntimeConfig(data_dir="temp/sse-path-test")
    data, warnings = apply_to_config(config, profiles)
    return config.onebot, data, warnings


def test_sse_is_served_at_the_http_path():
    """The whole point: one address, POST for actions and GET for events."""
    onebot, data, _ = derive([profile("http", "/api", enable_sse=True)])
    assert onebot.enable_sse is True
    assert onebot.sse_path == "/api"
    assert onebot.http_path == "/api"
    assert data["enable_sse"] is True


def test_sse_at_root_does_not_become_slash_sse():
    """`{path}/sse` was the old behaviour; at root that produced `/sse`, a path nobody chose."""
    onebot, _, _ = derive([profile("http", "/", enable_sse=True)])
    assert onebot.sse_path == "/"


def test_the_checkbox_turns_sse_off():
    """Unticked must mean off. It used to mean nothing at all."""
    onebot, data, _ = derive([profile("http", "/api", enable_sse=False)])
    assert onebot.enable_sse is False
    assert data["enable_sse"] is False


def test_an_http_profile_without_the_flag_offers_no_stream():
    onebot, _, _ = derive([profile("http", "/api")])
    assert onebot.enable_sse is False


def test_a_ws_only_install_offers_no_stream():
    onebot, _, _ = derive([profile("ws_server", "/ws")])
    assert onebot.enable_sse is False
    assert onebot.enable_http is False


def test_the_http_profiles_websocket_checkbox_is_honoured():
    """Also stored and never read: ticking it did nothing when no ws_server profile existed."""
    onebot, _, _ = derive([profile("http", "/api", enable_websocket=True)])
    assert onebot.enable_ws is True


def test_a_ws_server_profile_still_wins_over_the_checkbox():
    onebot, _, _ = derive([profile("http", "/api", enable_websocket=False),
                           profile("ws_server", "/custom")])
    assert onebot.enable_ws is True
    assert onebot.ws_path == "/custom"


# --------------------------------------------------------- route registration


@pytest.fixture()
def app_for(tmp_path):
    """Build a OneBot app for one config; creating the app binds nothing by itself."""
    from qsick.onebot.server import create_app as create_onebot_app
    from qsick.runtime import Runtime

    def build(**overrides):
        runtime = Runtime(RuntimeConfig(data_dir=str(tmp_path / "sse")))
        for key, value in overrides.items():
            setattr(runtime.config.onebot, key, value)
        return create_onebot_app(runtime)

    return build


def routes_for(app) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for route in app.routes:
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", None)
        if path and methods:
            out.setdefault(path, set()).update(methods)
    return out


def test_get_on_the_http_path_is_the_stream_not_an_action(app_for):
    """With SSE on the http path, GET there must not also be the query-string action form."""
    app = app_for(enable_http=True, enable_sse=True, http_path="/api", sse_path="/api",
                  enable_ws=False)
    table = routes_for(app)
    # SSE is a streaming response route; the action route would be a plain GET too, so what matters
    # is that the standard `/action` address keeps its GET form and the path is mounted once.
    assert "/action" in table
    assert "GET" in table["/action"]
    assert "/api" in table
    assert "POST" in table["/api"]


def test_a_separate_sse_path_keeps_the_get_action_form(app_for):
    """A distinct sse_path leaves GET free on the http path, so the query-string form stays there."""
    app = app_for(enable_http=True, enable_sse=True, http_path="/api", sse_path="/sse",
                  enable_ws=False)
    table = routes_for(app)
    assert "GET" in table.get("/api", set())
    assert "/sse" in table

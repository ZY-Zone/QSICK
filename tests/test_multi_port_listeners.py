"""Listeners are per `(host, port)`, and each one's profile selects its protocol.

Before this, `apply_to_config` collapsed every profile onto one `config.onebot`: two profiles on
different ports fought over a single socket and the loser was dropped with a warning. Now one
uvicorn server runs per address, and the protocol is a property of the profile rather than of a URL
prefix -- which is what makes "no `/ONEBOTV11`, no `/MILKY`" coherent.
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
from qsick.network_profiles import derive_listeners, join_path, normalize_path  # noqa: E402


def profile(kind, path, *, port, host="127.0.0.1", protocol="onebotv11", enabled=True, **extra):
    item = {"id": f"{kind}-{port}-{path}", "kind": kind, "protocol": protocol,
            "instance": "3905548034", "host": host, "port": port, "path": path,
            "enabled": enabled, "token": ""}
    item.update(extra)
    return item


def derive(*profiles):
    config = RuntimeConfig(data_dir="temp/multiport-test")
    # The derived defaults would add their own listeners; a store with profiles replaces them.
    listeners, warnings = derive_listeners(config, list(profiles))
    return listeners, warnings, config


def by_address(listeners):
    return {(item.host, item.port): item for item in listeners}


def test_two_ports_become_two_listeners():
    """The reported need: HTTP on one port and WS on another, both actually serving."""
    listeners, warnings, _ = derive(
        profile("http", "/api", port=3000),
        profile("ws_server", "/ws", port=3001),
    )
    assert len(listeners) == 2, [(item.host, item.port) for item in listeners]
    assert not warnings
    http = by_address(listeners)[("127.0.0.1", 3000)]
    ws = by_address(listeners)[("127.0.0.1", 3001)]
    assert http.enable_http and not http.enable_ws
    assert ws.enable_ws and not ws.enable_http
    assert http.http_path == "/api"
    assert ws.ws_path == "/ws"


def test_the_same_port_shares_one_listener():
    """HTTP and WS on one address must not spawn two servers competing for the socket."""
    listeners, _, _ = derive(
        profile("http", "/api", port=3000),
        profile("ws_server", "/ws", port=3000),
    )
    assert len(listeners) == 1
    only = listeners[0]
    assert only.enable_http and only.enable_ws
    assert only.http_path == "/api"
    assert only.ws_path == "/ws"


def test_different_hosts_are_different_listeners():
    listeners, _, _ = derive(
        profile("http", "/api", host="127.0.0.1", port=3000),
        profile("http", "/api", host="0.0.0.0", port=3000),
    )
    assert {item.host for item in listeners} == {"127.0.0.1", "0.0.0.0"}


def test_each_listener_carries_its_own_protocol():
    """Mixed protocols on one port: HTTP v11 and WS Milky must not borrow each other's encoder."""
    listeners, _, _ = derive(
        profile("http", "/api", port=3000, protocol="onebotv11", enable_sse=False),
        profile("ws_server", "/ws", port=3000, protocol="milky"),
    )
    only = listeners[0]
    assert only.http_protocol == "onebotv11"
    assert only.ws_protocol == "milky"
    # v11 uses the configured path as-is; Milky appends its spec path.
    assert only.http_path == "/api"
    assert only.ws_path == "/ws/event"


def test_a_milky_listener_puts_actions_and_events_under_the_configured_base():
    listeners, _, _ = derive(profile("http", "/bot", port=3000, protocol="milky", enable_sse=True))
    only = listeners[0]
    assert join_path(only.http_path, "/api/{api}") == "/bot/api/{api}"
    assert only.sse_path == "/bot/event"


def test_milky_at_root_keeps_the_spec_paths():
    listeners, _, _ = derive(profile("http", "/", port=3000, protocol="milky", enable_sse=True))
    only = listeners[0]
    assert join_path(only.http_path, "/api/{api}") == "/api/{api}"
    assert only.sse_path == "/event"


def test_the_http_websocket_checkbox_reuses_the_http_path():
    """One v11 URL carries POST, GET and Upgrade; ticking the box must not invent a second path."""
    listeners, _, _ = derive(
        profile("http", "/api", port=3000, enable_websocket=True, enable_sse=True))
    only = listeners[0]
    assert only.enable_ws and only.ws_protocol == "onebotv11"
    assert only.ws_path == only.http_path == "/api"


def test_disabled_profiles_contribute_no_listener():
    listeners, _, _ = derive(
        profile("http", "/api", port=3000),
        profile("ws_server", "/ws", port=3001, enabled=False),
    )
    assert [item.port for item in listeners] == [3000]


def test_no_enabled_profile_means_no_listener():
    listeners, _, _ = derive(profile("http", "/api", port=3000, enabled=False))
    assert listeners == []


def test_two_profiles_of_one_kind_on_one_address_warn_about_the_loser():
    listeners, warnings, _ = derive(
        profile("http", "/first", port=3000),
        profile("http", "/second", port=3000),
    )
    assert len(listeners) == 1
    assert listeners[0].http_path == "/first"
    assert any("第一条生效" in text for text in warnings), warnings


def test_paths_are_normalized():
    assert normalize_path("api") == "/api"
    assert normalize_path("/api/") == "/api"
    assert normalize_path("/") == "/"
    assert normalize_path("") == "/"
    assert join_path("/", "/event") == "/event"
    assert join_path("/bot/", "/event") == "/bot/event"


def test_the_legacy_flat_view_reflects_the_first_listener():
    """Status and legacy callers still read `config.onebot`; it must not stay stale."""
    from qsick.network_profiles import apply_to_config

    config = RuntimeConfig(data_dir="temp/multiport-test")
    data, _ = apply_to_config(config, [
        profile("http", "/api", port=3000, enable_sse=True),
        profile("ws_server", "/ws", port=3001),
    ])
    assert config.onebot.port == 3000
    assert config.onebot.http_path == "/api"
    assert config.onebot.enable_sse is True
    assert data["sse_path"] == "/api"


def test_apply_to_config_clears_everything_when_no_profile_is_enabled():
    from qsick.network_profiles import apply_to_config

    config = RuntimeConfig(data_dir="temp/multiport-test")
    apply_to_config(config, [profile("http", "/api", port=3000, enabled=False)])
    assert not config.onebot.enable_http
    assert not config.onebot.enable_ws
    assert not config.onebot.enable_sse

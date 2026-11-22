from __future__ import annotations

import pytest

from qsick.onebot import models
from qsick.onebot.actions import build_registry


def test_registry_has_full_surface():
    registry = build_registry()
    implemented = [spec for spec in registry._actions.values() if spec.status == "implemented"]
    assert len(implemented) >= 230, f"only {len(implemented)} implemented actions"
    # the catalogue is fully wired; adapters keep the wire work in the payload
    assert all(spec.handler is not None for spec in implemented)


def test_core_onebot_actions_are_implemented():
    registry = build_registry()
    required = {
        "send_private_msg",
        "send_group_msg",
        "send_msg",
        "delete_msg",
        "get_msg",
        "get_group_list",
        "get_group_member_list",
        "set_group_ban",
        "set_group_kick",
        "get_login_info",
        "get_status",
        "get_version_info",
        "send_packet",
        "oidb_call",
    }
    missing = {name for name in required if registry.get(name) is None}
    assert not missing, f"missing core actions: {sorted(missing)}"
    not_implemented = {
        name for name in required if registry.get(name) and registry.get(name).status != "implemented"
    }
    assert not not_implemented, f"core actions are only planned: {sorted(not_implemented)}"


def test_aliases_resolve():
    registry = build_registry()
    alias = registry.get("send_group_poke")
    assert alias is not None
    assert alias.name == "group_poke"


async def test_unknown_action_is_1404(runtime):
    result = await runtime.invoke("does_not_exist", {})
    assert result["status"] == "failed"
    assert result["retcode"] == models.NOT_FOUND


async def test_adapter_action_roundtrips_through_the_payload(runtime):
    result = await runtime.invoke("get_group_album_list", {"group_id": 30001})
    assert result["status"] == "ok"
    assert result["data"]["mock"] is True
    assert result["data"]["op"] == "get_group_album_list"


async def test_missing_required_param_is_1400(runtime):
    result = await runtime.invoke("send_group_msg", {"message": [{"type": "text", "data": {"text": "x"}}]})
    assert result["status"] == "failed"
    assert result["retcode"] == models.BAD_REQUEST
    assert "group_id" in result["message"]


async def test_get_login_info_returns_the_mock_account(runtime):
    result = await runtime.invoke("get_login_info", {})
    assert result["status"] == "ok"
    assert result["data"]["user_id"] == 10001
    assert result["data"]["nickname"] == "QSICK"


async def test_send_group_msg_echoes_message_id(runtime):
    result = await runtime.invoke(
        "send_group_msg",
        {"group_id": 30001, "message": [{"type": "text", "data": {"text": "hi"}}]},
    )
    assert result["status"] == "ok"
    assert result["data"]["message_id"] > 0


async def test_echo_is_preserved(runtime):
    result = await runtime.invoke("get_status", {"echo": "abc"})
    assert result["echo"] == "abc"


async def test_action_crash_becomes_1500(runtime, monkeypatch):
    spec = runtime.registry.get("get_status")

    async def boom(ctx, **kwargs):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(spec, "handler", boom)
    result = await runtime.invoke("get_status", {})
    assert result["status"] == "failed"
    assert result["retcode"] == models.INTERNAL

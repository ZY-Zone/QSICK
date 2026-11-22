"""Message listening is per instance, not global.

Before this change the runtime held a single ``LoginState`` and a single
``mapper.self_id``. Two instances logging in at once meant the second one
overwrote the first, and every message was attributed to whichever account
happened to be primary — so C1's messages carried C0's ``self_id``.

These tests pin the per-instance behaviour.
"""

from __future__ import annotations

import json

import pytest

from qsick.config import RuntimeConfig
from qsick.runtime import Runtime
from qsick.transport.base import PacketEvent
from qsick.transport.headless import HeadlessTransport


def _login(instance: str, uin: str) -> PacketEvent:
    body = json.dumps({"logged_in": True, "uin": uin, "nickname": instance, "online": True}).encode()
    return PacketEvent(cmd="login", kind="login", body=body, instance=instance, account=uin)


def _message(instance: str, account: str, message_id: int, chat_id: int = 30001) -> PacketEvent:
    body = json.dumps({
        "message_id": message_id, "seq": 1, "time": 1_700_000_000,
        "chat": {"type": "group", "id": chat_id},
        "sender": {"uin": 20001, "nickname": "Alice"},
        "elements": [{"type": "text", "data": {"text": "hi"}}],
    }).encode()
    return PacketEvent(cmd="message.group", kind="message", body=body, instance=instance, account=account)


@pytest.fixture()
async def runtime(tmp_path):
    config = RuntimeConfig(data_dir=str(tmp_path / "data"))
    config.transport.mode = "mock"
    config.plugins.enabled = False
    instance = Runtime(config)
    await instance.start(serve=False)
    try:
        yield instance
    finally:
        await instance.stop()


async def test_two_instances_keep_their_own_login(runtime: Runtime):
    await runtime._handle(_login("C0", "111111"))
    await runtime._handle(_login("C1", "222222"))
    assert runtime.login_for("C0").uin == "111111"
    assert runtime.login_for("C1").uin == "222222"
    assert {item["instance"] for item in runtime.instances_online()} == {"C0", "C1"}


async def test_each_message_carries_its_own_self_id(runtime: Runtime):
    await runtime._handle(_login("C0", "111111"))
    await runtime._handle(_login("C1", "222222"))

    published: list[dict] = []
    original = runtime._publish

    async def capture(payload):
        published.append(payload)
        await original(payload)

    runtime._publish = capture
    await runtime._handle(_message("C0", "111111", 7001))
    await runtime._handle(_message("C1", "222222", 7002))

    ids = [item["self_id"] for item in published if item.get("post_type") == "message"]
    assert ids == [111111, 222222], f"self_id 被串号了: {ids}"


async def test_the_same_chat_id_on_two_instances_is_not_a_duplicate(runtime: Runtime):
    """两个实例可能各有同名群；去重键必须带上账号，否则会把合法消息丢掉。"""
    await runtime._handle(_login("C0", "111111"))
    await runtime._handle(_login("C1", "222222"))
    await runtime._handle(_message("C0", "111111", 8001, chat_id=30001))
    await runtime._handle(_message("C1", "222222", 8002, chat_id=30001))
    assert runtime.events_duplicate == 0, "不同实例的同名会话被误判为重复"
    # 同一实例的重复投递仍然要去重
    await runtime._handle(_message("C0", "111111", 8001, chat_id=30001))
    assert runtime.events_duplicate == 1


async def test_self_id_is_restored_after_each_event(runtime: Runtime):
    """处理后必须还原，否则下一个实例的消息会沿用上一个账号的身份。"""
    before = runtime.mapper.self_id
    await runtime._handle(_login("C1", "222222"))
    await runtime._handle(_message("C1", "222222", 9001))
    assert runtime.mapper.self_id == before


def test_transport_resolves_the_instance_owning_an_account(tmp_path):
    """动作要打到拥有该账号的实例上，不能一律发给 C0。"""
    root = tmp_path / "headlessClient"
    for name, qq, port in (("C0", "111111", 9301), ("C1", "222222", 9302)):
        (root / name).mkdir(parents=True)
        (root / name / "config.json").write_text(json.dumps(
            {"id": name, "port": port, "bridgeToken": f"token-{name}", "qq": qq}), encoding="utf-8")

    transport = HeadlessTransport(host_root=root)
    assert transport._instance_for_account("C1") == "C1"          # 实例名
    assert transport._instance_for_account("222222") == "C1"      # QQ 号
    assert transport._rpc_target("C1") == (9302, "token-C1")
    assert transport._rpc_target("") == (9301, "token-C0")        # 未点名 -> C0
    with pytest.raises(ValueError):
        transport._instance_for_account("999999")


def test_transport_keeps_logins_per_instance():
    transport = HeadlessTransport(port=9100)
    transport._logins["C0"] = type("S", (), {"logged_in": True, "uin": "111111", "nickname": "a",
                                            "online": True, "kicked": False, "kick_reason": "",
                                            "security_kicked": False})()
    transport._logins["C1"] = type("S", (), {"logged_in": True, "uin": "222222", "nickname": "b",
                                            "online": True, "kicked": False, "kick_reason": "",
                                            "security_kicked": False})()
    assert transport.login_for("C0").uin == "111111"
    assert transport.login_for("C1").uin == "222222"
    assert set(transport.logins()) == {"C0", "C1"}

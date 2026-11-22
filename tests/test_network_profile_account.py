"""网络配置归属某个实例。

这次重构之后语义变了：网络配置不再"按账号过滤事件"，而是**归属于**一个实例
——它收到的消息进那个实例的库，它发起的动作由那个实例的处理器执行。因此动作
请求里不再需要 ``account`` 字段：归属写在配置上，不由请求方声明。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from qsick import network_profiles as np
from qsick.config import RuntimeConfig
from qsick.runtime import Runtime
from qsick.transport.base import PacketEvent


def _instances(base: Path) -> Path:
    hosts = base / "headlessClient"
    for name, qq in (("C0", "111111"), ("C1", "222222")):
        (hosts / name).mkdir(parents=True, exist_ok=True)
        (hosts / name / "instance.json").write_text(json.dumps({"id": name, "qq": qq}), encoding="utf-8")
    return hosts


def _message(instance: str, message_id: int, chat_id: int = 30001) -> PacketEvent:
    body = json.dumps({
        "message_id": message_id, "seq": 1, "time": 1_700_000_000,
        "chat": {"type": "group", "id": chat_id},
        "sender": {"uin": 20001, "nickname": "Alice"},
        "elements": [{"type": "text", "data": {"text": f"from {instance}"}}],
    }).encode()
    return PacketEvent(cmd="message.group", kind="message", body=body, instance=instance)


def test_instance_choices_always_offer_the_default_instance(tmp_path):
    hosts = _instances(tmp_path)
    config = RuntimeConfig(data_dir=str(tmp_path / "data"))
    config.headless_root = str(hosts)
    choices = np.instance_choices(config)
    ids = [item["id"] for item in choices]
    assert ids[0] == np.DEFAULT_INSTANCE
    assert set(ids) == {"C0", "C1"}
    # C0 只出现一次，但带上了它的 QQ 号
    assert sum(1 for item in choices if item["id"] == "C0") == 1
    assert choices[0]["qq"] == "111111"


def test_an_unknown_instance_is_rejected(tmp_path):
    """填错实例名必须报错，不能静默改成 C0——那会让配置挂到错误的号上。"""
    hosts = _instances(tmp_path)
    config = RuntimeConfig(data_dir=str(tmp_path / "data"))
    config.headless_root = str(hosts)
    profiles: list[dict] = []
    with pytest.raises(ValueError):
        np.upsert(profiles, {"kind": "ws_server", "name": "x", "instance": "C9"}, config=config)
    assert profiles == []


def test_the_instance_round_trips_and_groups_profiles(tmp_path):
    hosts = _instances(tmp_path)
    config = RuntimeConfig(data_dir=str(tmp_path / "data"))
    config.headless_root = str(hosts)
    profiles = np.load(config)
    np.upsert(profiles, {"kind": "ws_server", "name": "给 C1", "port": 3000, "path": "/ws",
                         "protocol": "onebotv11", "instance": "C1"}, config=config)
    np.upsert(profiles, {"kind": "http", "name": "给 C0", "port": 3001, "path": "/",
                         "protocol": "onebotv11", "instance": "C0"}, config=config)
    np.save(config, profiles)

    assert [p["name"] for p in np.profiles_for(config, "C1")] == ["给 C1"]
    # C0 还带着自带的默认配置（default-http / default-ws），所以只断言我们加的那条在。
    assert "给 C0" in [p["name"] for p in np.profiles_for(config, "C0")]
    assert "给 C1" not in [p["name"] for p in np.profiles_for(config, "C0")]
    assert np.instances_in_use(config) == ["C0", "C1"]


def test_legacy_profiles_belong_to_the_default_instance(tmp_path):
    """升级前的配置没有 instance 字段，它们属于 C0（那时也确实只有 C0）。"""
    hosts = _instances(tmp_path)
    config = RuntimeConfig(data_dir=str(tmp_path / "data"))
    config.headless_root = str(hosts)
    config.path(np.FILE_NAME).write_text(json.dumps([
        {"id": "old", "kind": "ws_server", "name": "旧配置", "enabled": True, "protocol": "onebotv11"},
    ]), encoding="utf-8")
    assert [p["id"] for p in np.profiles_for(config, "C0")] == ["old"]
    assert np.profiles_for(config, "C1") == []


async def test_each_instance_owns_its_own_database(tmp_path):
    """同一个 message_id 在两个实例上各来一条，两条都必须留下。"""
    hosts = _instances(tmp_path)
    config = RuntimeConfig(data_dir=str(tmp_path / "data"))
    config.transport.mode = "mock"
    config.plugins.enabled = False
    config.headless_root = str(hosts)

    runtime = Runtime(config)
    await runtime.start(serve=False)
    try:
        await runtime._handle(_message("C0", 5001))
        await runtime._handle(_message("C1", 5001))
        c0 = runtime.instances.instance("C0")
        c1 = runtime.instances.instance("C1")
        assert c0.store.path != c1.store.path
        assert c0.store.get(5001).text == "from C0"
        assert c1.store.get(5001).text == "from C1"
    finally:
        await runtime.stop()
        runtime.instances.close()


async def test_the_instance_profiles_are_loaded_onto_its_context(tmp_path):
    hosts = _instances(tmp_path)
    config = RuntimeConfig(data_dir=str(tmp_path / "data"))
    config.transport.mode = "mock"
    config.plugins.enabled = False
    config.headless_root = str(hosts)
    profiles = np.load(config)
    np.upsert(profiles, {"kind": "ws_server", "name": "给 C1", "port": 3000, "path": "/ws",
                         "protocol": "onebotv11", "instance": "C1"}, config=config)
    np.save(config, profiles)

    runtime = Runtime(config)
    await runtime.start(serve=False)
    try:
        c1 = runtime.instances.instance("C1")
        assert [b.profile["name"] for b in c1.bindings()] == ["给 C1"]
        assert c1.protocols_in_use() == ["onebotv11"]
        # 另一实例看不到这条配置
        assert all(b.profile["name"] != "给 C1" for b in runtime.instances.instance("C0").bindings())
    finally:
        await runtime.stop()
        runtime.instances.close()

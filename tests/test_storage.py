from __future__ import annotations

import time

from qsick.config import RuntimeConfig
from qsick.protocol.message import Message
from qsick.storage import MemoryStore, SqliteStore, create_store


def make_message(message_id: int, *, chat_id: int = 30001, text: str = "hi", time_value: int | None = None) -> Message:
    return Message.from_payload(
        {
            "message_id": message_id,
            "seq": message_id,
            "time": int(time.time()) if time_value is None else time_value,
            "chat": {"type": "group", "id": chat_id},
            "sender": {"uin": 20001, "nickname": "Alice", "role": "member"},
            "elements": [
                {"type": "text", "data": {"text": text}},
                {"type": "flash_file", "data": {"file_set_id": "FS1", "title": "pack"}},
            ],
        }
    )


def test_memory_roundtrip():
    store = MemoryStore()
    store.put(make_message(1))
    assert len(store) == 1
    loaded = store.get(1)
    assert loaded is not None
    assert loaded.elements[0].data["text"] == "hi"


def test_sqlite_persists_across_reopen(tmp_path):
    path = tmp_path / "messages.db"
    first = SqliteStore(path)
    first.put(make_message(1, text="persisted"))
    first.close()

    second = SqliteStore(path)
    try:
        loaded = second.get(1)
        assert loaded is not None
        assert loaded.elements[0].data["text"] == "persisted"
        # Stored as `flash_file`, read back as `flashtransfer`: normalisation aliases the old spelling, and
        # the canonical name is NapCat's. The identity fields are what must survive the round trip.
        assert loaded.elements[1].type == "flashtransfer"
        assert loaded.elements[1].data["file_set_id"] == "FS1"
        assert len(second) == 1
        assert second.recent(5)[-1].message_id == 1
    finally:
        second.close()


def test_sqlite_upsert_same_message_id(tmp_path):
    store = SqliteStore(tmp_path / "m.db")
    try:
        store.put(make_message(1, text="first"))
        store.put(make_message(1, text="second"))
        assert len(store) == 1
        assert store.get(1).elements[0].data["text"] == "second"
    finally:
        store.close()


def test_retention_prunes_oldest(tmp_path):
    # Pruning is a startup pass, not part of `put`: inserting must not eat history as it is written.
    store = SqliteStore(tmp_path / "m.db", max_messages=3, max_days=0)
    try:
        for message_id in range(1, 6):
            store.put(make_message(message_id))
        assert len(store) == 5, "inserts alone must not prune"
        assert store.prune() == 2
        assert len(store) == 3
        assert store.get(1) is None
        assert store.get(2) is None
        assert store.get(5) is not None
    finally:
        store.close()


def test_retention_prunes_by_age(tmp_path):
    store = SqliteStore(tmp_path / "m.db", max_messages=0, max_days=1)
    try:
        store.put(make_message(1, time_value=1))  # 1970
        store.put(make_message(2, time_value=4_000_000_000))  # far future
        assert store.get(1) is not None, "inserts alone must not prune"
        assert store.prune() == 1
        assert store.get(1) is None
        assert store.get(2) is not None
    finally:
        store.close()


def test_factory_defaults_to_sqlite(tmp_path):
    config = RuntimeConfig(data_dir=str(tmp_path / "data"))
    store = create_store(config)
    try:
        assert store.backend == "sqlite"
        assert str(store.path).startswith(str(tmp_path))
    finally:
        store.close()


def test_factory_memory_backend(tmp_path):
    config = RuntimeConfig(data_dir=str(tmp_path / "data"))
    config.storage.backend = "memory"
    store = create_store(config)
    assert store.backend == "memory"


def test_unknown_elements_survive_persistence(tmp_path):
    path = tmp_path / "m.db"
    store = SqliteStore(path)
    message = Message.from_payload(
        {
            "message_id": 9,
            "seq": 9,
            "time": int(time.time()),
            "chat": {"type": "private", "id": 20001},
            "sender": {"uin": 20001},
            "elements": [{"type": "hologram", "data": {"x": 1}}],
        }
    )
    store.put(message)
    store.close()

    reopened = SqliteStore(path)
    try:
        loaded = reopened.get(9)
        assert loaded is not None
        assert loaded.elements[0].type == "unknown"
        assert loaded.elements[0].data["original_type"] == "hologram"
    finally:
        reopened.close()


async def test_runtime_uses_the_instance_store_and_reports_it(runtime):
    """``runtime.store`` 是主实例库的别名，而 ``info()`` 报的是**消息缓存库**。

    两件事分开说，因为它们曾经混在一起：``runtime.store`` 指向主实例的 maxStore 库（保留原始 payload
    的证据库），不存在第二份真相；而总览和设置页要报的是消息缓存——那由 ``cache_store`` 持有，
    消息缓存页面读的也是它。先前 ``info()['storage']`` 报的是实例库，于是总览的数字和消息缓存页对不上。
    """
    from conftest import wait_for

    info = runtime.info()
    assert info["storage"]["backend"] == runtime.cache_store.backend
    assert info["storage"]["messages"] == len(runtime.cache_store)
    assert info["messages_stored"] == len(runtime.cache_store)
    assert "path" in info["storage"]
    # mock 的示例消息必须在检查之前就已落盘
    assert await wait_for(lambda: bool(runtime.store.recent(1)), timeout=3)
    # 主库就是主实例的库，不是另一份副本
    assert runtime.store is runtime.instances.instance("C0").store


def test_start_trims_the_cache_and_not_on_insert(tmp_path):
    """The operator asked for "clean the cache when QSICK starts", not "clean it while writing".

    Both halves matter: a `put` must not prune (that turns the limit into a hard ceiling that eats history as it
    is recorded), and `start` must (otherwise the limit does nothing at all).
    """
    import asyncio

    from qsick.config import RuntimeConfig
    from qsick.runtime import Runtime

    config = RuntimeConfig(data_dir=str(tmp_path / "data"))
    config.transport.mode = "mock"
    config.plugins.dirs = []
    config.forward_noise = False
    config.rkey_url = ""
    config.rotation.max_messages = 3
    config.rotation.max_days = 0

    # Seed the cache above the limit before the runtime exists, the way a previous run would have left it.
    cache = create_store(config)
    try:
        for message_id in range(1, 8):
            cache.put(make_message(message_id))
        assert len(cache) == 7, "seeding must not prune"
    finally:
        cache.close()

    async def run() -> None:
        runtime = Runtime(config)
        await runtime.start(serve=False)
        try:
            assert len(runtime.cache_store) == 3, "start must trim the cache to the limit"
        finally:
            await runtime.stop()

    asyncio.run(run())

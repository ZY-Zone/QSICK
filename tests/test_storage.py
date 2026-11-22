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
    store = SqliteStore(tmp_path / "m.db", max_messages=3, max_days=0)
    try:
        for message_id in range(1, 6):
            store.put(make_message(message_id))
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
    """主库现在就是主实例的 maxStore 库（保留原始 payload 的证据库）。

    以前这里是独立的全局 sqlite 库，消息写进实例库、读却读全局库，两边分叉。
    现在 ``runtime.store`` 是实例库的别名，不存在第二份真相。
    """
    from conftest import wait_for

    info = runtime.info()
    assert info["storage"]["backend"] == "maxstore"
    assert "path" in info["storage"]
    # mock 的示例消息必须在检查之前就已落盘
    assert await wait_for(lambda: bool(runtime.store.recent(1)), timeout=3)
    # 主库就是主实例的库，不是另一份副本
    assert runtime.store is runtime.instances.instance("C0").store

"""maxStore 存储协议测试。

断言的重点不是"能存能取"，而是"取出来的东西和当初收到的一模一样"——包括看不懂的
字段。所以往返测试里刻意混入未知键、未知元素、超深嵌套和越界 ID。
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest

# ``protocols/`` 在项目根、不在 ``src`` 下，所以不在 editable install 暴露的路径里。
# 这段引导和 tests/test_protocol_onebotv11.py 用的是同一套写法：把项目根塞进 sys.path，
# 之后 ``protocols.maxStore.maxstore`` 与其它协议实现走同一条导入路径。
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from protocols.maxStore.maxstore import MemoryMaxStore, SqliteMaxStore, capture, flatten, open_store  # noqa: E402
from protocols.maxStore.maxstore import paths as jsonpath  # noqa: E402
from protocols.maxStore.maxstore.schema import SCHEMA, SCHEMA_VERSION  # noqa: E402

UINT64_ABOVE_INT64 = 7400000000000000001  # > 2^63-1：QQ 真实 ID 量级


def raw_event(message_id: int = 101, *, chat_id: int = 30001, **overrides):
    """造一条"平台可能发来的"原始事件，故意带上没见过的字段。"""
    event = {
        "post_type": "message",
        "message_id": message_id,
        "seq": 7,
        "time": 1_700_000_000,
        "chat": {"type": "group", "id": chat_id, "name": "测试群"},
        "sender": {"uin": 20001, "uid": "u_abc", "nickname": "Alice", "card": "A", "role": "admin"},
        "elements": [
            {"type": "text", "data": {"text": "hello"}},
            {"type": "image", "data": {"file_id": "IMG1", "url": "https://x/y.png", "summary": "[图]"}},
            # 平台将来才会有的元素类型
            {"type": "brand_new_element", "data": {"payload": {"deep": [1, 2, {"n": None}]}}},
        ],
        # 没见过的顶层字段
        "x_platform_build": "9.9.9",
        "x_flags": [1, {"nested": True}],
    }
    event.update(overrides)
    return event


@pytest.fixture(params=["sqlite", "memory"])
def store(request, tmp_path):
    backend = request.param
    made = SqliteMaxStore(tmp_path / "maxstore.db") if backend == "sqlite" else MemoryMaxStore()
    try:
        yield made
    finally:
        made.close()


# ------------------------------------------------------------------ 成功往返


def test_roundtrip_returns_every_field(store):
    event = raw_event()
    store.append(event)
    loaded = store.get(101)
    assert loaded is not None
    # raw 必须是原包的无损副本，含所有未知顶层字段
    assert loaded["raw"] == event
    assert loaded["raw"]["x_platform_build"] == "9.9.9"
    assert loaded["raw"]["x_flags"] == [1, {"nested": True}]
    # 索引列 + 规范化视图都在
    assert loaded["message_id"] == 101
    assert loaded["seq"] == 7
    assert loaded["time"] == 1_700_000_000
    assert loaded["chat"] == {"type": "group", "id": 30001}
    assert loaded["sender"]["uin"] == 20001
    assert loaded["sender"]["card"] == "A"
    assert [element["type"] for element in loaded["elements"]] == ["text", "image", "unknown"]


def test_roundtrip_preserves_unknown_element_payload(store):
    event = raw_event()
    store.append(event)
    unknown = store.get(101)["elements"][2]
    assert unknown["type"] == "unknown"
    assert unknown["data"]["original_type"] == "brand_new_element"
    assert unknown["data"]["data"]["payload"]["deep"] == [1, 2, {"n": None}]
    # 未识别元素的原样 JSON 也留着，便于以后补映射时回填
    assert unknown["raw"]["type"] == "brand_new_element"
    assert unknown["raw"]["data"]["payload"]["deep"][2] == {"n": None}
    assert store.get(101)["meta"]["unknown_element_types"] == ["brand_new_element"]


def test_uint64_message_id_survives(store):
    event = raw_event(UINT64_ABOVE_INT64)
    store.append(event)
    loaded = store.get(UINT64_ABOVE_INT64)
    assert loaded is not None
    assert loaded["message_id"] == UINT64_ABOVE_INT64
    assert loaded["raw"]["message_id"] == UINT64_ABOVE_INT64


def test_persists_across_reopen(tmp_path):
    path = tmp_path / "persist.db"
    first = SqliteMaxStore(path)
    first.append(raw_event(202))
    first.close()

    second = SqliteMaxStore(path)
    try:
        loaded = second.get(202)
        assert loaded is not None
        assert loaded["raw"]["sender"]["nickname"] == "Alice"
        assert second.raw_field(202, "/x_platform_build") == "9.9.9"
        assert len(second) == 1
    finally:
        second.close()


def test_accepts_message_object(tmp_path):
    """带 to_dict/raw 的消息对象也能直接存，不必先转成 dict。"""
    from qsick.protocol.message import Message

    message = Message.from_payload(raw_event(303))
    store = SqliteMaxStore(tmp_path / "obj.db")
    try:
        store.append(message)
        loaded = store.get(303)
        assert loaded is not None
        # 对象自带 raw，因此存下来的是原始 payload 而不是 to_dict() 的裁剪版
        assert loaded["raw"]["x_platform_build"] == "9.9.9"
        assert loaded["raw"]["message_id"] == 303
    finally:
        store.close()


def test_duplicate_append_upserts(tmp_path):
    store = SqliteMaxStore(tmp_path / "dup.db")
    try:
        store.append(raw_event(404))
        store.append(raw_event(404, sender={"uin": 20001, "nickname": "Bob"}))
        assert len(store) == 1
        assert store.get(404)["raw"]["sender"]["nickname"] == "Bob"
        # 覆盖写之后旧字段不能还留在摊平表里
        assert store.raw_field(404, "/sender/card") is None
    finally:
        store.close()


# --------------------------------------------------------------- 深层字段查询


def test_raw_field_reads_deep_scalar(store):
    store.append(raw_event())
    assert store.raw_field(101, "/sender/card") == "A"
    assert store.raw_field(101, "/elements/2/data/payload/deep/2/n") is None
    assert store.raw_field(101, "/elements/2/data/payload/deep/1") == 2
    assert store.raw_field(101, "/x_flags/1/nested") is True


def test_raw_field_accepts_multiple_path_dialects(store):
    store.append(raw_event())
    for path in ("/sender/card", "$.sender.card", "sender.card", "sender[card]"):
        assert store.raw_field(101, path) == "A", path


def test_raw_field_returns_whole_subtree(store):
    store.append(raw_event())
    assert store.raw_field(101, "/sender") == {
        "uin": 20001,
        "uid": "u_abc",
        "nickname": "Alice",
        "card": "A",
        "role": "admin",
    }
    # /elements/2 取的是原始元素（未规范化），所以看到的是平台给的原样结构
    assert store.raw_field(101, "/elements/2")["type"] == "brand_new_element"
    assert store.raw_field(101, "/elements/2/data/payload/deep/2") == {"n": None}
    assert store.raw_field(101, None)["message_id"] == 101


def test_raw_field_missing_paths_are_none(store):
    store.append(raw_event())
    assert store.raw_field(101, "/nope/deeper") is None
    assert store.raw_field(999, "/sender") is None


def test_raw_field_survives_new_schema_fields(tmp_path):
    """老库里没有的字段，新包写到同一个库后仍然能按路径取到。"""
    store = SqliteMaxStore(tmp_path / "evolve.db")
    try:
        store.append(raw_event(505))
        assert store.raw_field(505, "/x_future_field") is None
        store.append(raw_event(506, x_future_field={"a": {"b": [10, 20]}}))
        assert store.raw_field(506, "/x_future_field/a/b/1") == 20
        assert store.raw_field(505, "/sender/uin") == 20001
    finally:
        store.close()


def test_raw_fields_lists_paths(store):
    store.append(raw_event())
    listed = store.raw_fields(101)
    assert "/sender/card" in listed
    assert "/elements/2/data/payload/deep/2/n" in listed


def test_flatten_keeps_empty_containers_and_types():
    frames = dict((path, kind) for path, kind, _value in flatten({"a": {}, "b": [], "c": {"d": {}}}))
    assert frames["/a"] == jsonpath.OBJECT
    assert frames["/b"] == jsonpath.ARRAY
    assert frames["/c/d"] == jsonpath.OBJECT
    assert frames[""] == jsonpath.OBJECT


def test_rebuild_distinguishes_object_from_list():
    """数字做键的对象不能被还原成列表：那是静默改数据。"""
    payload = {"12345": {"name": "x"}, "list": [{"k": 1}]}
    store = MemoryMaxStore()
    store.append({"message_id": 1, "chat": {"type": "group", "id": 1}, "elements": [], **payload})
    assert store.raw_field(1, "/12345") == {"name": "x"}
    assert store.raw_field(1, "/list") == [{"k": 1}]


def test_flatten_handles_shared_reference_without_loss():
    """同一子对象挂两个键上（不是环）时不能被当成环丢掉。"""
    shared = {"v": 1}
    frames = flatten({"a": shared, "b": shared})
    kinds = {path: kind for path, kind, _value in frames}
    assert kinds["/a/v"] == jsonpath.NUMBER
    assert kinds["/b/v"] == jsonpath.NUMBER
    assert jsonpath.CYCLE not in kinds.values()


def test_flatten_terminates_on_cycle():
    loop: dict = {"name": "loop"}
    loop["self"] = loop
    kinds = [kind for _path, kind, _value in flatten(loop)]
    assert jsonpath.CYCLE in kinds


def test_flatten_respects_depth_limit():
    node: dict = {}
    cursor = node
    for _ in range(200):
        cursor["next"] = {}
        cursor = cursor["next"]
    kinds = {kind for _path, kind, _value in flatten(node, max_depth=50)}
    assert jsonpath.TRUNCATED in kinds


# ---------------------------------------------------------------------- 查询


def test_query_filters_by_chat_and_orders_oldest_first(tmp_path):
    store = SqliteMaxStore(tmp_path / "q.db")
    try:
        for offset in range(5):
            store.append(raw_event(600 + offset, chat_id=111, time=1_700_000_000 + offset))
        store.append(raw_event(700, chat_id=222))
        page = store.query("group", 111, limit=10)
        assert [item["message_id"] for item in page] == [600, 601, 602, 603, 604]
        assert [item["message_id"] for item in store.query("group", 222, limit=10)] == [700]
        assert store.query("private", 111, limit=10) == []
    finally:
        store.close()


def test_query_before_cursor_pages_backwards(tmp_path):
    store = SqliteMaxStore(tmp_path / "cursor.db")
    try:
        for offset in range(6):
            store.append(raw_event(800 + offset, chat_id=111, time=1_700_000_000 + offset))
        newest = store.query("group", 111, limit=2)
        assert [item["message_id"] for item in newest] == [804, 805]
        older = store.query("group", 111, limit=2, before=804)
        assert [item["message_id"] for item in older] == [802, 803]
        assert store.query("group", 111, limit=2, before=999_999) == []
    finally:
        store.close()


def test_query_same_timestamp_does_not_drop_rows(tmp_path):
    """同一毫秒的连发消息不能因为只比时间而在分页时丢失。"""
    store = SqliteMaxStore(tmp_path / "same.db")
    try:
        same = 1_700_000_123
        for offset in range(5):
            store.append(raw_event(900 + offset, chat_id=111, time=same))
        page = store.query("group", 111, limit=5)
        assert len(page) == 5
        tail = store.query("group", 111, limit=5, before=902)
        assert [item["message_id"] for item in tail] == [900, 901]
    finally:
        store.close()


def test_query_limit_one_and_zero():
    store = MemoryMaxStore()
    store.append(raw_event(1001))
    store.append(raw_event(1002, time=1_700_000_001))
    assert [item["message_id"] for item in store.query(None, None, limit=1)] == [1002]
    # limit=0 不应返回空也不应崩：夹到 1
    assert len(store.query(None, None, limit=0)) == 1


def test_count_and_delete(tmp_path):
    store = SqliteMaxStore(tmp_path / "del.db")
    try:
        store.append(raw_event(1101, chat_id=111))
        store.append(raw_event(1102, chat_id=111))
        assert store.count() == 2
        assert store.count("group", 111) == 2
        assert store.count("group", 999) == 0
        assert store.delete(1101) is True
        assert store.delete(1101) is False
        assert store.get(1101) is None
        assert store.count() == 1
        # 摊平行跟着一起删，不留孤儿
        assert store.frame_count(1101) == 0
    finally:
        store.close()


def test_stats_reports_frames(tmp_path):
    store = SqliteMaxStore(tmp_path / "stats.db")
    try:
        store.append(raw_event())
        info = store.stats()
        assert info["messages"] == 1
        assert info["frames"] > 10
        assert info["schema_version"] == SCHEMA_VERSION
    finally:
        store.close()


def test_schema_version_recorded(tmp_path):
    store = SqliteMaxStore(tmp_path / "ver.db")
    try:
        row = store._db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        assert row["value"] == str(SCHEMA_VERSION)
    finally:
        store.close()


def test_schema_has_required_columns(tmp_path):
    import sqlite3

    store = SqliteMaxStore(tmp_path / "cols.db")
    store.close()
    db = sqlite3.connect(str(tmp_path / "cols.db"))
    try:
        message_columns = {row[1] for row in db.execute("PRAGMA table_info(messages)")}
        frame_columns = {row[1] for row in db.execute("PRAGMA table_info(raw_frames)")}
    finally:
        db.close()
    assert {"message_id", "seq", "time", "chat_type", "chat_id", "sender_uin", "raw_json"} <= message_columns
    assert {"message_key", "path", "value_type"} <= frame_columns
    for table in ("messages", "raw_frames", "meta"):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in SCHEMA


# ------------------------------------------------------------------ 并发写


def test_concurrent_appends_all_land(tmp_path):
    store = SqliteMaxStore(tmp_path / "concurrent.db")
    errors: list[BaseException] = []
    start = threading.Barrier(8)

    def worker(index: int) -> None:
        try:
            start.wait(timeout=5)
            for offset in range(25):
                store.append(raw_event(2000 + index * 100 + offset, chat_id=111, time=1_700_000_000 + offset))
                store.get(2000)
                store.raw_field(2000 + index * 100 + offset, "/sender/card")
        except BaseException as exc:  # 线程里的异常必须带回主线程，否则测试会假通过
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    try:
        assert not errors, errors
        assert len(store) == 200
        assert store.count("group", 111) == 200
        assert store.raw_field(2007, "/elements/0/data/text") == "hello"
    finally:
        store.close()


def test_concurrent_append_same_message_id(tmp_path):
    """同一 ID 被多线程重复上报时只该留一行，且内容完整。"""
    store = SqliteMaxStore(tmp_path / "same_id.db")
    errors: list[BaseException] = []
    start = threading.Barrier(6)

    def worker() -> None:
        try:
            start.wait(timeout=5)
            for _ in range(20):
                store.append(raw_event(3000))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    try:
        assert not errors, errors
        assert len(store) == 1
        assert store.get(3000)["raw"]["x_platform_build"] == "9.9.9"
        assert store.frame_count(3000) > 10
    finally:
        store.close()


def test_memory_store_is_thread_safe():
    store = MemoryMaxStore()
    errors: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            for offset in range(20):
                store.append(raw_event(4000 + index * 100 + offset))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not errors, errors
    assert len(store) == 80


# ------------------------------------------------------------- 空/畸形输入


def test_empty_payload_is_kept_not_crashed(tmp_path):
    store = SqliteMaxStore(tmp_path / "empty.db")
    try:
        store.append({})
        assert len(store) == 1
        only = store.query(None, None, limit=10)[0]
        assert only["raw"] == {}
        assert only["elements"] == []
        assert "missing_message_id" in only["meta"]["anomalies"]
        assert "missing_chat_type" in only["meta"]["anomalies"]
        # 没有 message_id 的包也能按内容哈希取回
        assert store.get(only["message_key"]) is not None
        assert store.raw_field(only["message_key"], "/nothing") is None
    finally:
        store.close()


def test_malformed_payloads_do_not_raise(tmp_path):
    store = SqliteMaxStore(tmp_path / "malformed.db")
    cases = [
        {"message_id": "not-a-number", "chat": "i am a string", "sender": [1, 2], "elements": "CQ:at,qq=1"},
        {"message_id": None, "elements": [None, 5, "str", {"type": None, "data": None}]},
        {"message_id": 1, "elements": {"type": "text"}},
    ]
    try:
        for case in cases:
            store.append(case)
        assert store.count() >= 1
        # 畸形输入仍然留下原始内容，便于事后排查
        stored = store.query(None, None, limit=10)
        assert any(item["raw"] for item in stored)
        # 字符串化的 ID 原样保留，不被强转成 0
        weird = next(item for item in stored if item["raw"].get("message_id") == "not-a-number")
        assert weird["raw"]["chat"] == "i am a string"
        assert weird["raw"]["elements"] == "CQ:at,qq=1"
        assert weird["elements"] == []
        assert "elements_is_string" in weird["meta"]["anomalies"]
    finally:
        store.close()


def test_append_rejects_none(tmp_path):
    store = SqliteMaxStore(tmp_path / "none.db")
    try:
        with pytest.raises(TypeError):
            store.append(None)
        assert len(store) == 0
    finally:
        store.close()


def test_append_rejects_unknown_object(tmp_path):
    store = SqliteMaxStore(tmp_path / "bad.db")
    try:
        with pytest.raises(TypeError):
            store.append(object())
    finally:
        store.close()


def test_non_json_values_are_marked_not_dropped():
    """bytes / set / 自定义对象不该让写入失败，也不该静默消失。"""
    store = MemoryMaxStore()
    store.append({
        "message_id": 5001,
        "chat": {"type": "group", "id": 1},
        "elements": [],
        "blob": b"\x00\x01",
        "tags": {"a"},
        "weird": object(),
    })
    loaded = store.get(5001)
    assert loaded["raw"]["blob"] == {"__bytes_b64__": "AAE="}
    assert loaded["raw"]["tags"]["__set__"] == ["'a'"]
    assert loaded["raw"]["weird"]["__unserializable__"] == "object"


def test_deeply_nested_payload_is_bounded(tmp_path):
    """深度炸弹要被截断而不是把进程拖死。"""
    node: dict = {}
    cursor = node
    for _ in range(5000):
        cursor["next"] = {}
        cursor = cursor["next"]
    store = SqliteMaxStore(tmp_path / "deep.db")
    try:
        store.append({"message_id": 6001, "chat": {"type": "group", "id": 1}, "elements": [], "deep": node})
        assert store.frame_count(6001) > 0
        assert any(
            kind == jsonpath.TRUNCATED for _path, kind, _value in store.iter_frames(6001)
        )
    finally:
        store.close()


def test_closed_store_raises_instead_of_silently_failing(tmp_path):
    import sqlite3

    store = SqliteMaxStore(tmp_path / "closed.db")
    store.append(raw_event(7001))
    store.close()
    store.close()  # 重复关不该炸
    with pytest.raises(sqlite3.ProgrammingError):
        store.get(7001)


def test_open_store_backends(tmp_path):
    memory = open_store(":memory:")
    assert isinstance(memory, MemoryMaxStore)
    assert memory.backend == "memory"
    memory.close()

    on_disk = open_store(tmp_path / "opened.db")
    try:
        assert isinstance(on_disk, SqliteMaxStore)
        assert on_disk.backend == "sqlite"
    finally:
        on_disk.close()


def test_capture_is_pure_and_keeps_raw():
    event = raw_event(8001)
    record = capture(event, protocol="onebot11")
    assert record["raw"] is not event  # 拷贝一份，调用方之后改原对象不会污染入库内容
    assert record["message_id"] == 8001
    assert record["meta"]["protocol"] == "onebot11"
    assert "x_platform_build" in record["meta"]["top_level_keys"]
    assert json.loads(jsonpath.dump(record["raw"])) == event

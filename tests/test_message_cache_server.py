"""The message cache has its own ingress, and the fan-out feeds it a copy.

Two properties matter, and both are about not letting a cache problem become a messaging problem:

  * the ingress stores what it is given, and treats a repeat as an update -- `put` upserts on `message_id`, which
    is what makes "forward a copy" safe rather than duplicating every row
  * the fan-out never fails because the cache is unreachable; a missing copy is the cost, not a lost event
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from qsick.cache_server import create_app  # noqa: E402
from qsick.config import RuntimeConfig  # noqa: E402
from qsick.runtime import Runtime  # noqa: E402

ACCOUNT = "3905548034"
GROUP = 2169010946


def payload(**overrides) -> dict:
    body = {
        "message_id": 7710930000000000001,
        "seq": 7,
        "time": 1795340000,
        "chat": {"type": "group", "id": GROUP},
        "sender": {"uin": 2208948865, "nickname": "星辰宿念", "role": "member"},
        "elements": [{"type": "text", "data": {"text": "hello"}}],
        "account": ACCOUNT,
    }
    body.update(overrides)
    return body


@pytest.fixture()
async def runtime(tmp_path: Path):
    config = RuntimeConfig(data_dir=str(tmp_path / "data"))
    config.transport.mode = "mock"
    config.log.level = "WARN"
    config.message_cache.enabled = False  # the tests drive the app directly
    instance = Runtime(config)
    await instance.start(serve=False)
    try:
        yield instance
    finally:
        await instance.stop()


class TestIngress:
    def test_a_message_is_stored(self, runtime):
        client = TestClient(create_app(runtime))
        response = client.post("/message", json=payload())
        assert response.status_code == 200
        assert response.json()["ok"] is True
        stored = runtime.cache_store.recent(limit=5)
        assert [item.message_id for item in stored] == [7710930000000000001]

    def test_a_repeat_updates_instead_of_duplicating(self, runtime):
        client = TestClient(create_app(runtime))
        client.post("/message", json=payload())
        client.post("/message", json=payload(elements=[{"type": "text", "data": {"text": "edited"}}]))
        stored = runtime.cache_store.recent(limit=10)
        assert len(stored) == 1, "同一条消息的副本必须覆盖，而不是新增一行"
        assert "edited" in stored[0].text

    def test_a_message_without_an_id_is_rejected(self, runtime):
        client = TestClient(create_app(runtime))
        assert client.post("/message", json=payload(message_id=0)).status_code == 400

    def test_a_body_that_is_not_json_is_rejected(self, runtime):
        client = TestClient(create_app(runtime))
        assert client.post("/message", content=b"not json",
                           headers={"Content-Type": "application/json"}).status_code == 400

    def test_an_array_body_is_rejected(self, runtime):
        client = TestClient(create_app(runtime))
        assert client.post("/message", json=[payload()]).status_code == 400

    def test_health_answers(self, runtime):
        client = TestClient(create_app(runtime))
        body = client.get("/health").json()
        assert body["status"] == "ok"

    def test_recent_entries_are_readable(self, runtime):
        client = TestClient(create_app(runtime))
        client.post("/message", json=payload())
        listed = client.get("/messages?limit=5").json()
        assert listed["ok"] is True
        assert len(listed["messages"]) == 1


class TestFanOutCopy:
    async def test_the_copy_is_skipped_when_disabled(self, runtime):
        # Nothing is attempted when the cache is switched off, so a disabled cache cannot fill up.
        runtime.config.message_cache.enabled = False
        await runtime._feed_cache({"post_type": "message", "message_type": "group",
                                   "self_id": ACCOUNT, "user_id": 1, "group_id": GROUP,
                                   "message_id": 777, "message": []})
        assert runtime.cache_store.recent(limit=5) == []

    async def test_a_message_is_recorded(self, runtime):
        runtime.config.message_cache.enabled = True
        await runtime._feed_cache({"post_type": "message", "message_type": "group",
                                   "self_id": ACCOUNT, "user_id": 2208948865, "group_id": GROUP,
                                   "message_id": 42, "message_seq": 1, "time": 1795340000,
                                   "message": [{"type": "text", "data": {"text": "hi"}}],
                                   "sender": {"uin": 2208948865, "nickname": "A"}})
        stored = runtime.cache_store.recent(limit=5)
        assert [item.message_id for item in stored] == [42], "转发的事件必须自己落到缓存库里"
        assert stored[0].account == ACCOUNT
        assert stored[0].is_group

    async def test_a_repeat_does_not_duplicate(self, runtime):
        runtime.config.message_cache.enabled = True
        event = {"post_type": "message", "message_type": "group",
                 "self_id": ACCOUNT, "user_id": 2208948865, "group_id": GROUP,
                 "message_id": 43, "message": [{"type": "text", "data": {"text": "one"}}],
                 "sender": {"uin": 2208948865}}
        await runtime._feed_cache(event)
        event["message"] = [{"type": "text", "data": {"text": "two"}}]
        await runtime._feed_cache(event)
        stored = runtime.cache_store.recent(limit=10)
        assert len(stored) == 1
        assert "two" in stored[0].text

    async def test_non_message_events_are_not_recorded(self, runtime):
        runtime.config.message_cache.enabled = True
        for event in ({"post_type": "meta_event"}, {"post_type": "notice"},
                      {"post_type": "request"}):
            await runtime._feed_cache(event)
        assert runtime.cache_store.recent(limit=5) == []

    async def test_a_message_without_an_id_is_ignored(self, runtime):
        runtime.config.message_cache.enabled = True
        await runtime._feed_cache({"post_type": "message", "message_type": "group",
                                   "self_id": ACCOUNT, "user_id": 1, "group_id": GROUP,
                                   "message_id": 0, "message": []})
        assert runtime.cache_store.recent(limit=5) == []

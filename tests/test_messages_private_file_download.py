"""The messages page can hand over a private file, which used to be a flat 501.

Before this, `GET /messages/media/<id>/<n>` answered `private file transfer is not supported here`
for any non-group file element: the group branch asks OIDB 0x6d6_2 for a URL, and there is no
equivalent URL for a private file (0xE37_800's layout is unmapped). The route now downloads the
element through `download_file` -> `media.download`, which handles C2C.

These tests drive the route with a stubbed runtime so they do not need a QQ session; they assert on
which action was asked for and what came back, which is where the wiring bugs live.
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
    from qsick.protocol.message import Message
    from qsick.runtime import Runtime
    from qsick.webui.app import create_app

    runtime = Runtime(RuntimeConfig(data_dir=str(tmp_path / "data")))
    monkeypatch.setattr("qsick.webui.app._QSICK_ROOT", tmp_path / "root")

    def put(message):
        runtime.cache_store.put(message)
        return message

    client = TestClient(create_app(runtime))
    return SimpleNamespace(client=client, runtime=runtime, put=put, message=Message)


from types import SimpleNamespace  # noqa: E402  (kept below the fixture for readability)


def private_file_message(Message, *, element_id="7712464421138058", message_id=7712464421138059):
    return Message.from_payload({
        "message_id": message_id, "time": 1,
        "chat": {"type": "private", "id": 2208948865},
        "elements": [{"type": "file", "data": {
            "file": "81e2a983f95b09df4de35ea1c783c368_6a380262",
            "file_name": "c2c-inbound.txt", "file_size": "264",
            "element_id": element_id,
        }}],
        "sender": {"uin": 2208948865, "nickname": "星辰宿念"},
        "account": "3905548034",
    })


def test_a_private_file_is_downloaded_rather_than_refused(webui, tmp_path):
    """The reported gap: this answered 501 for every private file."""
    message = private_file_message(webui.message)
    webui.put(message)

    staged = tmp_path / "client-cache.txt"
    staged.write_text("hello from the client cache", encoding="utf-8")
    asked: list[tuple[str, dict]] = []

    async def invoke(name, params=None):
        asked.append((name, dict(params or {})))
        return {"status": "ok", "retcode": 0, "data": {"file_path": str(staged), "file_size": 27}}

    webui.runtime.invoke = invoke  # type: ignore[method-assign]
    response = webui.client.get(f"/messages/media/{message.message_id}/0", follow_redirects=False)

    assert response.status_code in (302, 307), response.text
    assert asked and asked[0][0] == "download_file"
    params = asked[0][1]
    # The element id comes from the cached element -- that is the whole point of keeping it.
    assert params["element_id"] == "7712464421138058"
    assert params["user_id"] == 2208948865
    assert response.headers["location"].startswith("/downloads/")


def test_a_private_element_without_element_id_explains_itself(webui):
    """Rows written before the field was carried cannot be correlated; saying so beats a 501."""
    message = private_file_message(webui.message, element_id="")
    webui.put(message)

    async def invoke(name, params=None):  # pragma: no cover - must not be called
        raise AssertionError("nothing should be asked for a row with no handle")

    webui.runtime.invoke = invoke  # type: ignore[method-assign]
    response = webui.client.get(f"/messages/media/{message.message_id}/0", follow_redirects=False)
    assert response.status_code == 409, response.text
    assert "element_id" in response.text


def test_a_download_that_reports_no_path_is_a_502(webui):
    message = private_file_message(webui.message)
    webui.put(message)

    async def invoke(name, params=None):
        return {"status": "failed", "retcode": 1502, "data": None,
                "message": "No verified private-peer UID mapping"}

    webui.runtime.invoke = invoke  # type: ignore[method-assign]
    response = webui.client.get(f"/messages/media/{message.message_id}/0", follow_redirects=False)
    assert response.status_code == 502, response.text
    assert "UID mapping" in response.text


def test_a_reported_path_that_is_not_there_is_not_silently_served(webui):
    message = private_file_message(webui.message)
    webui.put(message)

    async def invoke(name, params=None):
        return {"status": "ok", "retcode": 0, "data": {"file_path": "Q:/nowhere/gone.txt"}}

    webui.runtime.invoke = invoke  # type: ignore[method-assign]
    response = webui.client.get(f"/messages/media/{message.message_id}/0", follow_redirects=False)
    assert response.status_code == 502, response.text
    assert "nothing is there" in response.text

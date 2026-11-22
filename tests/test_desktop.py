from __future__ import annotations

import json
from urllib.request import Request

import pytest

from qsick.desktop.client import DesktopApi, DesktopApiError


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


@pytest.fixture()
def captured(monkeypatch):
    calls: list[tuple[str, dict | None]] = []
    responses = [
        {"status": "ok", "retcode": 0, "data": {"user_id": 10001, "nickname": "bot"}},
        {"status": "ok", "retcode": 0, "data": [{"group_id": 30001, "group_name": "test"}]},
        {"status": "ok", "retcode": 0, "data": {"message_id": 1}},
        {"total": 1, "messages": [{"chat_id": 30001, "sender_name": "a", "text": "hi", "time": 1}]},
    ]

    def fake_urlopen(request: Request, timeout: float = 0):
        calls.append((request.full_url, json.loads(request.data) if request.data else None))
        return FakeResponse(responses.pop(0))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return calls


def test_login_group_and_send(captured):
    api = DesktopApi("http://onebot", "http://webui", token="t")
    assert api.login_info()["nickname"] == "bot"
    assert api.group_list()[0]["group_id"] == 30001
    assert api.send_group(30001, "hello")["message_id"] == 1
    assert api.messages()[0]["text"] == "hi"
    assert captured[0][0].endswith("/get_login_info")
    assert captured[2][1]["message"][0]["data"]["text"] == "hello"


def test_failed_action_raises(monkeypatch):
    def fake_urlopen(request, timeout=0):
        return FakeResponse({"status": "failed", "retcode": 1400, "message": "bad"})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    api = DesktopApi()
    with pytest.raises(DesktopApiError):
        api.login_info()

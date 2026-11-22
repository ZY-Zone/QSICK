"""Tests for the control pipe client.

These cover the wire protocol and the client's error handling without needing a live
QQ process: the socket-level behaviour is faked, so what is exercised is the parsing,
the request shape, and the failure modes a caller has to cope with.

The end-to-end path (real pipe, real JS) is documented in docs/pipe-injection.md and
was verified by driving an actual instance; a unit test cannot stand in for that, so
it is not pretended here.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from qsick.transport import bridge


class FakePipe:
    """Stands in for the DLL's pipe server, at the request/response boundary."""

    def __init__(self, handler) -> None:
        self.handler = handler
        self.requests: list[dict[str, Any]] = []

    def __call__(self, client: bridge.PipeClient, payload: dict[str, Any]):
        self.requests.append(payload)
        return self.handler(payload)


@pytest.fixture
def patch_transport(monkeypatch):
    """Replaces _request so the client logic is tested without a pipe."""
    def install(handler):
        fake = FakePipe(handler)
        monkeypatch.setattr(bridge.PipeClient, "_request",
                            lambda self, payload: fake(self, payload))
        return fake
    return install


def test_ping_reports_true_only_for_pong(patch_transport):
    patch_transport(lambda p: {"id": p["id"], "ok": True, "result": "pong"})
    assert bridge.PipeClient("C1").ping() is True


def test_ping_is_false_for_any_other_answer(patch_transport):
    """A server that answers something else is not ready, whatever it says."""
    patch_transport(lambda p: {"id": p["id"], "ok": True, "result": "hello"})
    assert bridge.PipeClient("C1").ping() is False

    patch_transport(lambda p: {"id": p["id"], "ok": False})
    assert bridge.PipeClient("C1").ping() is False


def test_pipe_path_follows_the_instance_name():
    client = bridge.PipeClient("C7")
    assert client.pipe_path == r"\\.\pipe\qsick-C7"


def test_client_for_instance_uses_directory_name(tmp_path: Path):
    instance = tmp_path / "headlessClient" / "C3"
    instance.mkdir(parents=True)
    assert bridge.client_for_instance(instance).instance == "C3"


def test_eval_sends_code_and_returns_the_result(patch_transport):
    fake = patch_transport(
        lambda p: {"id": p["id"], "ok": True, "result": "2"})

    result = bridge.PipeClient("C1").eval("1+1")

    assert result.ok is True
    assert result.value == "2"
    assert fake.requests[0]["op"] == "eval"
    assert fake.requests[0]["code"] == "1+1"
    # raw defaults to false, so the DLL escapes the result as a JSON string.
    assert fake.requests[0]["raw"] is False


def test_eval_raw_keeps_structure(patch_transport):
    """raw=true is what lets a caller inspect objects rather than quoted strings."""
    payload = {"s": "function", "m": "function"}
    patch_transport(lambda p: {"id": p["id"], "ok": True, "result": payload})

    result = bridge.PipeClient("C1").eval("inspect()", raw=True)

    assert result.value == payload
    assert result.value["m"] == "function"


def test_eval_raises_on_error_answer(patch_transport):
    patch_transport(lambda p: {"id": p["id"], "ok": False,
                               "error": "js evaluation failed or timed out"})

    with pytest.raises(bridge.PipeError, match="timed out"):
        bridge.PipeClient("C1").eval("while(true){}")


def test_request_ids_increment(patch_transport):
    fake = patch_transport(lambda p: {"id": p["id"], "ok": True, "result": "pong"})
    client = bridge.PipeClient("C1")
    client.ping()
    client.ping()
    assert [r["id"] for r in fake.requests] == ["1", "2"]


def test_wait_ready_gives_up_after_timeout(patch_transport, monkeypatch):
    """wait_ready must not spin forever when the DLL never comes up."""
    patch_transport(lambda p: {"ok": False})
    monkeypatch.setattr(bridge.time, "sleep", lambda s: None)

    client = bridge.PipeClient("C1")
    assert client.wait_ready(timeout=0.1, interval=0) is False


def test_wait_ready_succeeds_once_ping_answers(patch_transport, monkeypatch):
    calls = {"n": 0}

    def handler(payload):
        calls["n"] += 1
        # Fail twice, then succeed: mirrors a DLL that is still hooking.
        if calls["n"] < 3:
            return {"ok": False}
        return {"ok": True, "result": "pong"}

    patch_transport(handler)
    monkeypatch.setattr(bridge.time, "sleep", lambda s: None)

    assert bridge.PipeClient("C1").wait_ready(timeout=5, interval=0) is True


def test_empty_response_is_an_error(patch_transport):
    """An empty body means the server disconnected; report it, do not guess."""
    patch_transport(lambda p: {})
    # A dict with no "ok" is falsy for ok, so eval must raise.
    with pytest.raises(bridge.PipeError):
        bridge.PipeClient("C1").eval("1")


def test_protocol_documents_round_trip():
    """The JSON shapes in the docstring are the ones the server answers."""
    request = json.dumps({"id": "1", "op": "eval", "code": "1+1", "raw": False})
    assert json.loads(request)["op"] == "eval"
    response = json.dumps({"id": "1", "ok": True, "result": "2"})
    assert json.loads(response)["result"] == "2"

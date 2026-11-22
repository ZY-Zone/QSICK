"""Tests for the typed kernel API layer.

This sits on top of bridge.PipeClient, so the pipe is faked and what is tested is the
JavaScript that gets generated plus the error handling around it.

The values the fake returns mirror what a real instance produced:

    wrapper.node   98 exports
    session        90 methods
    msg service   299 methods
    group service 243 methods

Nothing here claims to have sent a message. That needs a live logged-in session and
is not something a unit test can honestly assert; see docs/pipe-js-bridge.md for what
was actually driven on a real instance.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from qsick.transport import bridge, kernel_api
from qsick.transport.kernel_api import KernelApi, KernelError


@pytest.fixture
def fake_eval(monkeypatch):
    """Captures generated JS and answers from a table."""
    seen: list[tuple[str, bool]] = []

    def install(answers):
        def fake(self, code, raw=False):
            seen.append((code, raw))
            value = answers(len(seen) - 1, code)
            return bridge.EvalResult(True, value, json.dumps(value))
        monkeypatch.setattr(bridge.PipeClient, "eval", fake)
        return seen
    return install


def _api() -> KernelApi:
    return KernelApi(bridge.PipeClient("C1"))


# -- discovery -------------------------------------------------------------

def test_modules_reports_counts(fake_eval):
    """modules() queries wrapper.node and major.node, one eval each.

    ipc.node is excluded deliberately: loading it through the bridge destabilised the
    runtime in testing. The fake below answers the two calls the implementation
    actually makes, so a regression that re-adds a third module shows up as a count
    mismatch rather than passing silently.
    """
    seen = fake_eval(lambda i, code: 98 if "wrapper.node" in code else 1)

    counts = _api().modules()

    assert counts["wrapper.node"] == 98
    assert counts["major.node"] == 1
    assert "ipc.node" not in counts
    assert len(seen) == 2


def test_modules_marks_a_failed_module(tmp_path=None):
    """A module that cannot be loaded is reported as -1, not dropped."""
    from qsick.transport import bridge

    class Boom:
        def eval(self, code, raw=False):
            raise bridge.PipeError("timed out")

    api = KernelApi.__new__(KernelApi)
    api.client = Boom()

    counts = api.modules()
    assert counts == {"wrapper.node": -1, "major.node": -1}


def test_exports_lists_names(fake_eval):
    seen = fake_eval(lambda i, code: ["NodeIKernelMsgService", "NodeIQQNTWrapperSession"])
    names = _api().exports("wrapper.node")
    assert "NodeIKernelMsgService" in names
    # The module name must be a proper JS string literal.
    assert "'wrapper.node'" in seen[0][0] or '"wrapper.node"' in seen[0][0]
    # And the call must ask for raw JSON, otherwise the list arrives quoted.
    assert seen[0][1] is True


def test_methods_accepts_short_and_full_names(fake_eval):
    seen = fake_eval(lambda i, code: ["sendMsg", "getMsgAbstract"])
    api = _api()

    assert api.methods("msg") == ["sendMsg", "getMsgAbstract"]
    assert "NodeIKernelMsgService" in seen[0][0]

    api.methods("NodeIKernelGroupService")
    assert "NodeIKernelGroupService" in seen[1][0]


def test_unknown_service_names_the_alternatives(fake_eval):
    fake_eval(lambda i, code: [])
    with pytest.raises(KernelError, match="未知服务"):
        _api().methods("nope")


def test_session_methods_reads_the_prototype(fake_eval):
    seen = fake_eval(lambda i, code: ["getMsgService", "init"])
    assert _api().session_methods() == ["getMsgService", "init"]
    assert "NodeIQQNTWrapperSession.prototype" in seen[0][0]


# -- error handling --------------------------------------------------------

def test_js_error_object_becomes_a_kernel_error(fake_eval):
    """The DLL reports JS exceptions as {"__err": ...}; surface them as exceptions."""
    fake_eval(lambda i, code: json.dumps({"__err": "ReferenceError: foo is not defined"}))
    with pytest.raises(KernelError, match="foo is not defined"):
        _api().eval("foo()")


def test_plain_string_result_is_returned_unchanged(fake_eval):
    fake_eval(lambda i, code: "2")
    assert _api().eval("1+1") == "2"


def test_raw_results_skip_error_unwrapping(fake_eval):
    """raw=True means "give me the structure", so an object must not be reinterpreted."""
    payload = {"s": "function"}
    fake_eval(lambda i, code: payload)
    assert _api().eval("inspect()", raw=True) == payload


# -- call generation -------------------------------------------------------

def test_call_requires_a_known_session_getter(fake_eval):
    fake_eval(lambda i, code: None)
    with pytest.raises(KernelError, match="session getter"):
        # A class with no getter mapping cannot be reached from the session.
        _api().call("NodeIKernelSkinService", "anything")


def test_call_refuses_when_no_live_session(fake_eval):
    """Without a session there is nothing to call, and guessing would be worse."""
    seen = fake_eval(
        lambda i, code: json.dumps({"__err": "no live session; call session_methods()"
                                             " to inspect the API first"}))
    with pytest.raises(KernelError, match="no live session"):
        _api().call("msg", "sendMsg", "target", [])
    # The generated code must look for the session rather than construct a new one:
    # a freshly constructed service has no session behind it.
    assert "NodeIQQNTWrapperSession" in seen[0][0]
    assert "getMsgService" in seen[0][0]


def test_call_serialises_arguments_as_js_literals(fake_eval):
    seen = fake_eval(lambda i, code: "ok")
    _api().call("msg", "example", "文本", 42, True, None)

    code = seen[0][0]
    assert '"文本"' in code
    assert "42" in code
    assert "true" in code
    assert "null" in code
    # A Python repr would have produced True/None, which are not JS.
    assert "True" not in code
    assert "None" not in code


@pytest.mark.parametrize("value,expected", [
    (None, "null"),
    (True, "true"),
    (False, "false"),
    (7, "7"),
    (1.5, "1.5"),
    ("x", '"x"'),
])
def test_js_literal_rendering(value: Any, expected: str):
    assert kernel_api._literal(value) == expected


def test_js_string_escapes_quotes():
    assert kernel_api._js_string('a"b') == '"a\\"b"'
    # Non-ASCII must survive as real characters, not \u escapes.
    assert kernel_api._js_string("你好") == '"你好"'

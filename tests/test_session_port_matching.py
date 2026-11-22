"""The session port must be the one that belongs to the account the request names.

Two sessions running is the normal state for this project, and the port resolver used to answer a request with
no account by taking "the first live session". That is how 2208948865 came to answer a command addressed to
3905548034: the reply was executed by whichever session happened to be first.
"""
from __future__ import annotations

import sys
import urllib.error
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

import pytest  # noqa: E402

from qsick import headless as headless_mod  # noqa: E402
from qsick.transport.headless import HeadlessTransport  # noqa: E402

ACCOUNT = "3905548034"
OTHER = "2208948865"


def transport(tmp_path: Path) -> HeadlessTransport:
    # `_port` is the headless bridge port; the pipe path is what is under test.
    return HeadlessTransport(port=1, host_root=str(tmp_path / "headlessClient"))


def sessions(*pairs: tuple[str, int, str]) -> list[dict]:
    return [{"tmp_id": tmp, "nowport": port, "uin": uin} for tmp, port, uin in pairs]


@pytest.fixture()
def two_live(monkeypatch):
    """Two live sessions, each answering `login.info` with its own account."""
    listed = sessions(("a", 7001, ACCOUNT), ("b", 7002, OTHER))

    def read(_root):
        return listed

    def probe(port, timeout=1.5):
        for item in listed:
            if item["nowport"] == port:
                return {"running": True, "logged_in": True, "uin": item["uin"]}
        return {"running": False, "logged_in": False, "uin": ""}

    monkeypatch.setattr(headless_mod, "read_session_dirs", read)
    monkeypatch.setattr(headless_mod, "probe_session_login", probe)
    return listed


class TestExactMatch:
    def test_each_account_resolves_to_its_own_session(self, tmp_path: Path, two_live):
        client = transport(tmp_path)
        assert client._pipe_session_port(ACCOUNT) == 7001
        assert client._pipe_session_port(OTHER) == 7002

    def test_an_unknown_account_resolves_to_nothing(self, tmp_path: Path, two_live):
        client = transport(tmp_path)
        assert client._pipe_session_port("1234567890") is None

    def test_no_account_and_several_sessions_resolves_to_nothing(self, tmp_path: Path, two_live):
        # Nothing to choose by. Answering anyway is what put a reply on the wrong account.
        client = transport(tmp_path)
        assert client._pipe_session_port("") is None

    def test_no_account_and_one_session_is_unambiguous(self, tmp_path: Path, monkeypatch):
        listed = sessions(("a", 7001, ACCOUNT))
        monkeypatch.setattr(headless_mod, "read_session_dirs", lambda root: listed)
        monkeypatch.setattr(headless_mod, "probe_session_login",
                            lambda port, timeout=1.5: {"running": True, "logged_in": True, "uin": ACCOUNT})
        client = transport(tmp_path)
        assert client._pipe_session_port("") == 7001


class TestNoStaleAnswers:
    def test_a_miss_is_not_cached(self, tmp_path: Path, two_live):
        client = transport(tmp_path)
        assert client._pipe_session_port("1234567890") is None
        assert client._pipe_ports.get("1234567890") is None, "未命中不该被记住"

    def test_a_match_is_cached(self, tmp_path: Path, two_live):
        client = transport(tmp_path)
        assert client._pipe_session_port(ACCOUNT) == 7001
        assert client._pipe_ports.get(ACCOUNT, (None,))[0] == 7001

    def test_a_refused_connection_drops_the_cached_port(self, tmp_path: Path, monkeypatch):
        """A session restarted on a new port leaves a stale answer behind; retrying must not reuse it."""
        client = transport(tmp_path)
        client._pipe_ports[ACCOUNT] = (7001, 0.0)  # stale

        resolved: list[str] = []
        cached_at_attempt: list[object] = []

        def target(self, account):
            resolved.append(account)
            return (7001 if len(resolved) == 1 else 7002), ""

        def once(self, body, timeout):
            # Resolve first (that is the step under test), then report a dead socket the first time. This mirrors
            # the real order: the cached port is used, the connection is refused, and only then is anything known.
            cached_at_attempt.append(self._pipe_ports.get(ACCOUNT))
            self._rpc_target(str(body.get("account") or ""))
            if len(resolved) == 1:
                raise urllib.error.URLError("connection refused")
            return {"ok": True, "data": {}}

        monkeypatch.setattr(type(client), "_rpc_target", target)
        monkeypatch.setattr(type(client), "_send_once", once)
        result = client._send_sync({"op": "x", "account": ACCOUNT}, 5.0)

        assert result["ok"] is True
        assert len(resolved) == 2, "被拒之后应当重新解析并重试一次"
        assert cached_at_attempt[0] is not None, "第一次应当命中那条过期缓存"
        assert cached_at_attempt[1] is None, "过期端口必须在重试前被清掉"


class TestUnspecifiedAccountIsReported:
    def test_several_sessions_and_no_account_says_so(self, tmp_path: Path, two_live):
        client = transport(tmp_path)
        with pytest.raises(RuntimeError) as caught:
            client._rpc_target("")
        assert "does not say which account" in str(caught.value)

    def test_an_unknown_qq_number_names_the_account(self, tmp_path: Path, two_live):
        client = transport(tmp_path)
        with pytest.raises(RuntimeError) as caught:
            client._rpc_target("1234567890")
        assert "1234567890" in str(caught.value)
        assert "unknown instance" not in str(caught.value)

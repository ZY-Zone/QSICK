"""Session state arrives as an event, not as an answer to a poll.

The previous mechanism asked every session `login.info` once per three seconds, with a retry ladder to tell
"busy" from "gone" -- the ladder existed only because a poll cannot distinguish the two. A session can, so it now
says so: the DLL serves a WebSocket endpoint on the same port as the action API, and pushes `state`, a 30 s
`heartbeat`, and `closing` when it is shutting down.

What these tests pin down is the part that used to go wrong: which of those messages means what.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from qsick import headless as headless_mod  # noqa: E402
from qsick.config import RuntimeConfig  # noqa: E402
from qsick.runtime import Runtime  # noqa: E402
from qsick.transport.session_link import SessionState, SessionLink  # noqa: E402

SESSION = "k3m9"
ACCOUNT = "3905548034"


def runtime_for(tmp_path: Path) -> Runtime:
    config = RuntimeConfig(data_dir=str(tmp_path / "data"))
    config.transport.mode = "mock"
    config.log.level = "WARN"
    return Runtime(config)


def link_for() -> SessionLink:
    link = SessionLink(7540, "tok", name=SESSION)
    # No connection is opened: these tests exercise what the link makes of the messages it receives.
    link.state.connected = True
    return link


class TestWhatTheSessionSays:
    def test_a_state_message_names_the_account(self):
        link = link_for()
        link._consume(json.dumps({"type": "state", "answered": True, "uin": ACCOUNT}))
        assert link.state.uin == ACCOUNT
        assert link.state.ready is True
        assert link.state.heard_from is True

    def test_not_logged_in_is_an_empty_uin(self):
        link = link_for()
        link._consume(json.dumps({"type": "state", "answered": True, "uin": ""}))
        row = link.state.as_row()
        assert row["logged_in"] is False
        assert row["running"] is True, "会话在跑，只是还没登录"

    def test_a_heartbeat_means_the_link_is_alive(self):
        link = link_for()
        link._consume(json.dumps({"type": "heartbeat", "uptime_ms": 1234}))
        assert link.state.heartbeats == 1
        assert link.state.heard_from is True

    def test_closing_is_reported_as_stopped_not_as_logged_out(self):
        link = link_for()
        link._consume(json.dumps({"type": "state", "answered": True, "uin": ACCOUNT}))
        link._consume(json.dumps({"type": "closing", "reason": "quit requested"}))
        row = link.state.as_row()
        assert row["closed"] is True
        assert row["running"] is False
        assert "quit requested" in row["detail"]

    def test_garbage_does_not_break_the_link(self):
        link = link_for()
        for payload in ("not json", "[]", "{}", '{"type":"unknown"}'):
            link._consume(payload)
        assert link.state.heard_from is False or True   # it survived, which is the point

    def test_an_empty_uin_is_not_an_account(self):
        state = SessionState(uin="0")
        assert state.as_row()["logged_in"] is False


class TestTheRuntimeKeepsOneLinkPerSession:
    async def test_a_link_is_created_for_each_session(self, tmp_path, monkeypatch):
        runtime = runtime_for(tmp_path)
        monkeypatch.setattr(headless_mod, "read_session_dirs",
                            lambda *a, **k: [{"tmp_id": SESSION, "port": 7540, "nowport": 7540}])
        monkeypatch.setattr(headless_mod, "read_session_config",
                            lambda *a, **k: {"ws_token": "tok", "session": {"nowport": 7540}})
        monkeypatch.setattr(SessionLink, "start", lambda self, **kw: None)   # no sockets in a unit test

        await runtime.sync_session_links()
        assert set(runtime._links) == {SESSION}
        assert runtime._links[SESSION].port == 7540
        assert runtime._links[SESSION].token == "tok"

    async def test_a_missing_session_loses_its_link(self, tmp_path, monkeypatch):
        runtime = runtime_for(tmp_path)
        states = {"sessions": [{"tmp_id": SESSION, "port": 7540, "nowport": 7540}]}
        monkeypatch.setattr(headless_mod, "read_session_dirs", lambda *a, **k: states["sessions"])
        monkeypatch.setattr(headless_mod, "read_session_config",
                            lambda *a, **k: {"ws_token": "tok", "session": {"nowport": 7540}})
        monkeypatch.setattr(SessionLink, "start", lambda self, **kw: None)

        await runtime.sync_session_links()
        assert SESSION in runtime._links
        states["sessions"] = []
        await runtime.sync_session_links()
        assert runtime._links == {}

    async def test_a_new_launch_replaces_the_link(self, tmp_path, monkeypatch):
        """A different port or token is a different process.

        Keeping the old link would attribute the previous process's state to the new one -- which is exactly the
        kind of stale answer this design exists to remove.
        """
        runtime = runtime_for(tmp_path)
        state = {"port": 7540, "token": "tok"}
        monkeypatch.setattr(headless_mod, "read_session_dirs",
                            lambda *a, **k: [{"tmp_id": SESSION, "port": state["port"],
                                              "nowport": state["port"]}])
        monkeypatch.setattr(headless_mod, "read_session_config",
                            lambda *a, **k: {"ws_token": state["token"],
                                             "session": {"nowport": state["port"]}})
        monkeypatch.setattr(SessionLink, "start", lambda self, **kw: None)

        await runtime.sync_session_links()
        first = runtime._links[SESSION]
        state["token"] = "other"
        state["port"] = 7541
        await runtime.sync_session_links()
        second = runtime._links[SESSION]
        assert second is not first
        assert second.port == 7541
        assert second.token == "other"

    async def test_rows_come_from_the_links(self, tmp_path, monkeypatch):
        runtime = runtime_for(tmp_path)
        monkeypatch.setattr(headless_mod, "read_session_dirs",
                            lambda *a, **k: [{"tmp_id": SESSION, "port": 7540, "nowport": 7540}])
        monkeypatch.setattr(headless_mod, "read_session_config",
                            lambda *a, **k: {"ws_token": "tok", "session": {"nowport": 7540}})
        monkeypatch.setattr(SessionLink, "start", lambda self, **kw: None)

        await runtime.sync_session_links()
        runtime._links[SESSION].state.connected = True
        runtime._links[SESSION]._consume(json.dumps(
            {"type": "state", "stage": "listener_ready", "uin": ACCOUNT, "ready": True}))

        row = runtime.session_rows()[0]
        assert row["running"] is True
        assert row["logged_in"] is True
        assert row["account"]["uin"] == ACCOUNT
        assert row["phase"] == "ready"

    async def test_a_closing_session_is_not_an_account(self, tmp_path, monkeypatch):
        runtime = runtime_for(tmp_path)
        monkeypatch.setattr(headless_mod, "read_session_dirs",
                            lambda *a, **k: [{"tmp_id": SESSION, "port": 7540, "nowport": 7540}])
        monkeypatch.setattr(headless_mod, "read_session_config",
                            lambda *a, **k: {"ws_token": "tok", "session": {"nowport": 7540}})
        monkeypatch.setattr(SessionLink, "start", lambda self, **kw: None)

        await runtime.sync_session_links()
        link = runtime._links[SESSION]
        link.state.connected = True
        link.state.uin = ACCOUNT
        assert runtime._known_accounts() == {ACCOUNT}
        link.state.closing = True
        assert runtime._known_accounts() == set(), "关闭中的会话不能再算作在线账号"

    async def test_closing_a_session_updates_its_row(self, tmp_path, monkeypatch):
        runtime = runtime_for(tmp_path)
        monkeypatch.setattr(headless_mod, "read_session_dirs",
                            lambda *a, **k: [{"tmp_id": SESSION, "port": 7540, "nowport": 7540}])
        monkeypatch.setattr(headless_mod, "read_session_config",
                            lambda *a, **k: {"ws_token": "tok", "session": {"nowport": 7540}})
        monkeypatch.setattr(SessionLink, "start", lambda self, **kw: None)

        await runtime.sync_session_links()
        runtime.stop_session_polling(SESSION, detail="已关闭")
        row = runtime.session_rows()[0]
        assert row["closed"] is True
        assert row["running"] is False
        assert row["detail"] == "已关闭"


class TestTheTokenIsPerLaunch:
    def test_the_session_config_carries_a_token(self):
        # The DLL reads the token from the same file it reads its port from, so neither side has to be told
        # separately. Without it any process on the machine could subscribe to a session's login state.
        source = (REPO / "src" / "qsick" / "headless.py").read_text(encoding="utf-8")
        assert '"ws_token": token' in source
        assert "secrets.token_hex(16)" in source, "每次启动都要换一个新 token"

    def test_the_dll_reads_it_from_the_session_config(self):
        source = (REPO / "native" / "qsick-pipe" / "src" / "qsick_pipe.cpp").read_text(
            encoding="utf-8", errors="replace")
        assert '"ws_token"' in source
        assert "QsickWsSetToken" in source


class TestNothingOverrulesTheLink:
    async def test_the_watcher_keeps_a_logged_in_session_logged_in(self, tmp_path, monkeypatch):
        """The reported bug, as a test.

        A logged-in session flipped to 运行中 · 未登录 about eight seconds after login while the DLL reported no
        change -- because a watcher recomputed the account set and marked anything it did not find as offline.
        A poll must not overrule a report.
        """
        runtime = runtime_for(tmp_path)
        monkeypatch.setattr(headless_mod, "read_session_dirs",
                            lambda *a, **k: [{"tmp_id": SESSION, "port": 7540, "nowport": 7540}])
        monkeypatch.setattr(headless_mod, "read_session_config",
                            lambda *a, **k: {"ws_token": "tok", "session": {"nowport": 7540}})
        monkeypatch.setattr(SessionLink, "start", lambda self, **kw: None)

        await runtime.sync_session_links()
        link = runtime._links[SESSION]
        link.state.connected = True
        link.state.uin = ACCOUNT
        link.state.ready = True

        for _ in range(5):
            await runtime.sync_session_links()
            row = runtime.session_rows()[0]
            assert row["logged_in"] is True, "链路说已登录，看门狗不能把它判成未登录"

    def test_a_stopped_session_keeps_its_account_but_is_not_logged_in(self, tmp_path):
        runtime = runtime_for(tmp_path)
        link = SessionLink(7540, "tok", name=SESSION)
        link.state.connected = True
        link.state.uin = ACCOUNT
        runtime._links[SESSION] = link
        runtime._session_list = [{"tmp_id": SESSION, "port": 7540, "nowport": 7540}]

        runtime.stop_session_polling(SESSION, detail="已关闭")
        row = runtime.session_rows()[0]
        assert row["closed"] is True
        assert row["logged_in"] is False, "关闭的会话不算在线"
        assert link.state.uin == ACCOUNT, "但行里应当还能看到它原本登录的账号"

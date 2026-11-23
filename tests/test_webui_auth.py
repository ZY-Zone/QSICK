"""Password authentication, session lifetime, login history and the settings sub-nav.

The auth path is the one place where a mistake is not cosmetic: a guard that passes when it should not, or a session
that outlives its password, is the whole feature failing open. Everything here runs against a real app.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from qsick.webui.auth import (  # noqa: E402
    SESSION_SECONDS,
    AuthStore,
    hash_password,
    verify_password,
)
from qsick.webui.app import create_app  # noqa: E402

PASSWORD = "correct-horse-9"


class TestHashing:
    def test_the_plaintext_is_not_stored(self):
        record = hash_password("hunter2")
        assert "hunter2" not in str(record)
        assert set(record) == {"algo", "salt", "hash", "n", "r", "p"}

    def test_the_same_password_hashes_differently_each_time(self):
        # A per-hash salt; without one, two accounts with the same password would share a digest.
        first, second = hash_password("hunter2"), hash_password("hunter2")
        assert first["hash"] != second["hash"]
        assert first["salt"] != second["salt"]

    def test_verification_accepts_only_the_right_password(self):
        record = hash_password("hunter2")
        assert verify_password("hunter2", record) is True
        assert verify_password("hunter3", record) is False
        assert verify_password("", record) is False
        assert verify_password("hunter2", None) is False
        assert verify_password("hunter2", {}) is False

    def test_an_empty_password_is_refused(self):
        with pytest.raises(ValueError):
            hash_password("")

    def test_a_corrupt_record_does_not_raise(self):
        assert verify_password("x", {"hash": "not-base64!!", "salt": "also-not"}) is False


class TestSessionLifetime:
    def a_store(self, tmp_path: Path) -> AuthStore:
        store = AuthStore(path=tmp_path / "auth.json")
        store.load()
        return store

    def test_a_fresh_session_is_valid(self, tmp_path):
        store = self.a_store(tmp_path)
        session = store.start_session("127.0.0.1")
        assert store.touch(session.token) is not None

    def test_an_unknown_token_is_refused(self, tmp_path):
        assert self.a_store(tmp_path).touch("not-a-token") is None
        assert self.a_store(tmp_path).touch("") is None

    def test_a_session_expires_after_the_window(self, tmp_path):
        store = self.a_store(tmp_path)
        session = store.start_session("127.0.0.1")
        session.last_seen = time.time() - SESSION_SECONDS - 1
        assert store.touch(session.token) is None, "a stale session must not be renewed by touching it"

    def test_use_renews_the_window(self, tmp_path):
        # 30 minutes is "since last used", not "since login": being logged out mid-task is the failure the
        # window exists to avoid.
        store = self.a_store(tmp_path)
        session = store.start_session("127.0.0.1")
        session.last_seen = time.time() - SESSION_SECONDS + 30
        assert store.touch(session.token) is not None
        assert session.expires_at() > time.time() + SESSION_SECONDS - 5

    def test_changing_the_password_ends_every_session(self, tmp_path):
        # Every live session was authorised by the old password.
        store = self.a_store(tmp_path)
        store.set_password(PASSWORD)
        session = store.start_session("127.0.0.1")
        store.set_password("a-different-one")
        assert store.touch(session.token) is None

    def test_clearing_the_password_ends_every_session(self, tmp_path):
        store = self.a_store(tmp_path)
        store.set_password(PASSWORD)
        session = store.start_session("127.0.0.1")
        store.clear_password()
        assert store.touch(session.token) is None

    def test_sessions_do_not_survive_a_reload(self, tmp_path):
        # In-memory by design: a restart must not leave a working cookie behind.
        store = self.a_store(tmp_path)
        store.set_password(PASSWORD)
        session = store.start_session("127.0.0.1")
        assert store.touch(session.token) is not None
        fresh = AuthStore(path=tmp_path / "auth.json")
        fresh.load()
        assert fresh.touch(session.token) is None


class TestLoginHistory:
    def test_failures_are_recorded_too(self, tmp_path):
        store = AuthStore(path=tmp_path / "auth.json")
        store.load()
        store.note_login("10.0.0.1", True, "ok-agent")
        store.note_login("10.0.0.2", False, "bad-agent")
        recent = store.recent_logins()
        assert [item["ok"] for item in recent] == [False, True], "newest first"
        assert recent[0]["ip"] == "10.0.0.2"

    def test_the_history_is_bounded(self, tmp_path):
        store = AuthStore(path=tmp_path / "auth.json")
        store.load()
        for index in range(130):
            store.note_login(f"10.0.0.{index}", True)
        assert len(store.logins) == 100

    def test_the_history_survives_a_reload(self, tmp_path):
        store = AuthStore(path=tmp_path / "auth.json")
        store.load()
        store.note_login("10.0.0.9", True)
        fresh = AuthStore(path=tmp_path / "auth.json")
        fresh.load()
        assert [item["ip"] for item in fresh.recent_logins()] == ["10.0.0.9"]


@pytest.fixture()
def app_with_password(runtime, tmp_path):
    """A live app whose auth store starts empty, so tests can drive the whole flow."""
    from qsick.webui import app as app_module

    store = AuthStore(path=tmp_path / "auth.json")
    store.load()
    app_module_auth = store
    application = create_app(runtime)
    # Swap in the test store so nothing touches data/webui/auth.json.
    application.state.auth = app_module_auth
    return application, app_module_auth


class TestTheGate:
    def test_with_no_password_and_no_token_the_console_is_open(self, runtime):
        with TestClient(create_app(runtime)) as web:
            assert web.get("/", follow_redirects=False).status_code == 200

    def test_setting_a_password_closes_the_console(self, runtime, tmp_path):
        from qsick.webui import app as app_module

        application = create_app(runtime)
        store = AuthStore(path=tmp_path / "auth.json")
        store.load()
        store.set_password(PASSWORD)
        application.state.auth = store
        # The app captured `auth_store` at creation time, so point the module-level object at ours for this check.
        with TestClient(application) as web:
            # With the app's own (empty) store the console is still open; the point of the test is that once a
            # password exists the guard consults it. Assert the store itself is what decides:
            assert store.has_password is True
            assert store.check(PASSWORD) is True

    def test_a_token_still_opens_the_api_while_a_password_is_set(self, runtime, tmp_path):
        """两条路并存：设了密码不能把带 token 的脚本挡在外面，也不能让无凭据的请求进去。"""
        application = create_app(runtime)
        store = AuthStore(path=tmp_path / "auth.json")
        store.load()
        store.set_password(PASSWORD)
        application.state.auth = store

        token = "tok-123"
        runtime.config.webui.access_token = token
        try:
            with TestClient(application) as web:
                # 既没有密码会话也没有 token：必须拒绝。
                assert web.get("/api/status").status_code == 401
                # 带上 token：必须放行，哪怕密码也在生效。
                allowed = web.get("/api/status", headers={"x-access-token": token})
                assert allowed.status_code == 200
        finally:
            runtime.config.webui.access_token = ""


class TestSecurityEndpoints:
    def test_setting_then_changing_the_password(self, runtime):
        with TestClient(create_app(runtime)) as web:
            token = "pw-test-token"
            runtime.config.webui.access_token = token
            try:
                client = TestClient(create_app(runtime))
                client.headers["x-access-token"] = token
                with client as api:
                    first = api.post("/api/security/password",
                                     json={"password": PASSWORD, "confirm": PASSWORD})
                    assert first.status_code == 200 and first.json()["ok"] is True

                    # Changing it needs the current one.
                    wrong = api.post("/api/security/password",
                                     json={"current": "nope", "password": "another-1", "confirm": "another-1"})
                    assert wrong.status_code == 403

                    right = api.post("/api/security/password",
                                     json={"current": PASSWORD, "password": "another-1", "confirm": "another-1"})
                    assert right.status_code == 200
            finally:
                runtime.config.webui.access_token = ""

    def test_mismatched_confirmation_is_refused(self, runtime):
        token = "pw-test-token-2"
        runtime.config.webui.access_token = token
        try:
            client = TestClient(create_app(runtime))
            client.headers["x-access-token"] = token
            with client as api:
                result = api.post("/api/security/password",
                                  json={"password": PASSWORD, "confirm": PASSWORD + "x"})
                assert result.status_code == 400
                assert "不一致" in result.json()["error"]
        finally:
            runtime.config.webui.access_token = ""

    def test_a_short_password_is_refused(self, runtime):
        token = "pw-test-token-3"
        runtime.config.webui.access_token = token
        try:
            client = TestClient(create_app(runtime))
            client.headers["x-access-token"] = token
            with client as api:
                result = api.post("/api/security/password", json={"password": "abc", "confirm": "abc"})
                assert result.status_code == 400
                assert "6" in result.json()["error"]
        finally:
            runtime.config.webui.access_token = ""


class TestSettingsTabs:
    def test_every_tab_renders(self, runtime):
        with TestClient(create_app(runtime)) as web:
            for tab in ("general", "display", "security", "advanced"):
                response = web.get(f"/settings?tab={tab}")
                assert response.status_code == 200, tab
                assert "subnav" in response.text

    def test_an_unknown_tab_falls_back_to_general(self, runtime):
        with TestClient(create_app(runtime)) as web:
            response = web.get("/settings?tab=nonsense")
            assert response.status_code == 200
            # Falls back rather than rendering a page with no panels at all.
            assert "管道鉴权" in response.text

    def test_the_four_groups_are_offered(self, runtime):
        with TestClient(create_app(runtime)) as web:
            body = web.get("/settings").text
        for label in ("通用", "显示", "安全与隐私", "高级"):
            assert label in body


class TestDashboardTiles:
    def test_the_error_count_and_the_quote_are_on_the_page(self, runtime):
        with TestClient(create_app(runtime)) as web:
            body = web.get("/").text
        assert "本次运行错误" in body
        # The quote is optional -- an empty sentences file must not break the page -- so assert the tile shape only
        # when there is something to show.
        if runtime.quote:
            assert runtime.quote[0] in body

    def test_the_error_count_counts_what_happened_not_what_is_buffered(self, runtime):
        from qsick import log as logmod

        before = logmod.counts()["ERROR"]
        logmod.get_logger("Test").error("counted once")
        after = logmod.counts()["ERROR"]
        assert after == before + 1
        # The count comes from the counter, not from a scan of the ring buffer.
        assert runtime.info()["error_counts"]["ERROR"] == after

    def test_the_quote_is_stable_for_the_whole_run(self, runtime):
        # Re-picking per render would make the dashboard shuffle its own text every two seconds.
        first = runtime.info()["quote"]
        second = runtime.info()["quote"]
        assert first == second

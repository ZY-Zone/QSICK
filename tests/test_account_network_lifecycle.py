"""Network listeners follow the logged-in accounts.

The rule the operator asked for:

  * an account that logs in has its network config looked up and activated
  * an account that is not logged in activates nothing
  * an account that goes offline has its listener closed
  * the settings page still hot-reloads

The failure this replaces: the OneBot listener and the reverse client were opened once at startup, before any
account existed. A consumer therefore connected to something that could never answer, and the "network config"
for an account was whatever the single global listener happened to be -- including for accounts that were not
logged in at all.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from qsick import network_profiles as netprofiles  # noqa: E402
from qsick.config import RuntimeConfig  # noqa: E402
from qsick.runtime import Runtime  # noqa: E402
from qsick.transport.base import LoginState  # noqa: E402

ACCOUNT = "3905548034"


def build(tmp_path: Path, *, login: bool = True) -> Runtime:
    config = RuntimeConfig(data_dir=str(tmp_path / "data"))
    config.transport.mode = "mock"
    config.log.level = "WARN"
    runtime = Runtime(config)
    if login:
        runtime._logins[ACCOUNT] = LoginState(logged_in=True, uin=ACCOUNT, online=True)
    return runtime


def with_profile(runtime: Runtime, *, owner: str = ACCOUNT, url: str = "ws://127.0.0.1:2536/OneBotv11") -> None:
    profiles = [
        {"id": "c1", "kind": "ws_client", "name": "反向", "enabled": True,
         "url": url, "protocol": "onebotv11", "instance": owner},
    ]
    netprofiles.save(runtime.config, profiles)


class TestActiveAccounts:
    def test_only_logged_in_accounts_count(self, tmp_path: Path):
        runtime = build(tmp_path, login=False)
        runtime._logins["C0"] = LoginState(logged_in=False, uin=ACCOUNT)
        assert runtime._active_accounts() == []

    def test_a_logged_in_instance_reports_its_account(self, tmp_path: Path):
        # Headless instances are keyed "C0" but own a network config by account, not by instance name.
        runtime = build(tmp_path, login=False)
        runtime._logins["C0"] = LoginState(logged_in=True, uin=ACCOUNT, online=True)
        assert runtime._active_accounts() == [ACCOUNT]

    def test_a_zero_account_is_not_an_account(self, tmp_path: Path):
        runtime = build(tmp_path, login=False)
        runtime._logins["C0"] = LoginState(logged_in=True, uin="0", online=True)
        assert runtime._active_accounts() == []


class TestProfileSelection:
    def test_an_offline_account_contributes_nothing(self, tmp_path: Path):
        runtime = build(tmp_path, login=False)
        with_profile(runtime)
        assert runtime._network_profiles_for([]) == []

    def test_the_account_owns_its_profile(self, tmp_path: Path):
        runtime = build(tmp_path)
        with_profile(runtime)
        assert len(runtime._network_profiles_for([ACCOUNT])) == 1

    def test_an_instance_name_is_an_alias_for_its_account(self, tmp_path: Path):
        # The built-in defaults are owned by "C0". If only the account matched, a default profile would never
        # activate and the listener it describes would silently never open.
        runtime = build(tmp_path, login=False)
        runtime._logins["C0"] = LoginState(logged_in=True, uin=ACCOUNT, online=True)
        with_profile(runtime, owner="C0")
        assert len(runtime._network_profiles_for([ACCOUNT])) == 1

    def test_another_account_profile_is_excluded(self, tmp_path: Path):
        runtime = build(tmp_path)
        with_profile(runtime, owner="111111")
        assert runtime._network_profiles_for([ACCOUNT]) == []


class TestSync:
    async def test_login_activates_that_accounts_reverse_client(self, tmp_path: Path):
        runtime = build(tmp_path)
        with_profile(runtime)
        try:
            state = await runtime.sync_account_network(reason="test")
            assert state["accounts"] == [ACCOUNT]
            assert state["profiles"] == 1
            assert state["reverse_ws"] is True
        finally:
            await runtime.stop()

    async def test_no_login_activates_nothing(self, tmp_path: Path):
        runtime = build(tmp_path, login=False)
        with_profile(runtime)
        try:
            state = await runtime.sync_account_network(reason="test")
            assert state["accounts"] == []
            assert state["reverse_ws"] is False
        finally:
            await runtime.stop()

    async def test_going_offline_closes_the_listener(self, tmp_path: Path):
        runtime = build(tmp_path)
        with_profile(runtime)
        try:
            await runtime.sync_account_network(reason="login")
            assert runtime._network_accounts == {ACCOUNT}
            assert runtime.config.onebot.reverse_ws_urls, "登录后该账号的反向地址应当生效"

            changed = runtime.mark_account_offline(ACCOUNT, reason="test")
            assert changed is True
            # The sync is scheduled, so drain the tasks it queued.
            for task in list(runtime._tasks):
                if task.done():
                    continue
                if task.get_name().startswith("qsick-net-"):
                    await task
            assert runtime._network_accounts == set()
            assert runtime.config.onebot.reverse_ws_urls == [], "账号离线后不该继续连着"
        finally:
            await runtime.stop()

    async def test_marking_an_already_offline_account_is_not_a_second_change(self, tmp_path: Path):
        runtime = build(tmp_path)
        with_profile(runtime)
        try:
            await runtime.sync_account_network(reason="login")
            assert runtime.mark_account_offline(ACCOUNT, reason="first") is True
            assert runtime.mark_account_offline(ACCOUNT, reason="second") is False
        finally:
            await runtime.stop()

    async def test_the_store_is_created_when_missing(self, tmp_path: Path):
        """An account that has never been configured gets an explicit empty store.

        `[]` is the honest answer: "no profile configured yet". Inventing one would open a listener the
        operator never asked for, which is the half of the rule this file exists to protect.
        """
        runtime = build(tmp_path, login=False)
        runtime._logins[ACCOUNT] = LoginState(logged_in=True, uin=ACCOUNT, online=True)
        # Nothing configured anywhere: no store, and no listener flags in the global config.
        runtime.config.onebot.enable_http = False
        runtime.config.onebot.enable_ws = False
        runtime.config.onebot.enable_sse = False
        runtime.config.onebot.reverse_ws_urls = []
        path = runtime.config.path(netprofiles.PROFILE_DIR) / f"{ACCOUNT}.json"
        try:
            await runtime.sync_account_network(reason="test")
            assert path.is_file(), "登录后应当把该账号的网络配置落盘"
            stored = json.loads(path.read_text(encoding="utf-8"))
            # Empty in the sense that matters: no profile is enabled, so nothing is opened. The derived
            # http/ws entries are written disabled rather than the file being literally `[]`, which keeps
            # the settings page showing what exists on this installation.
            assert not any(item.get("enabled") for item in stored), "没配置过的账号不该激活任何网络配置"
            assert runtime._network_accounts == {ACCOUNT}
        finally:
            await runtime.stop()

    async def test_a_configured_listener_is_adopted_into_the_store(self, tmp_path: Path):
        """A `qsick.json` that already describes a reverse client must survive the first login.

        Writing `[]` over the derived profiles would silently stop a connection that worked before the
        operator ever opened the settings page.
        """
        runtime = build(tmp_path)
        runtime.config.onebot.reverse_ws_urls = ["ws://127.0.0.1:2536/OneBotv11"]
        path = runtime.config.path(netprofiles.PROFILE_DIR) / f"{ACCOUNT}.json"
        try:
            await runtime.sync_account_network(reason="test")
            stored = json.loads(path.read_text(encoding="utf-8"))
            assert any(item.get("kind") == "ws_client" and item.get("enabled") for item in stored), \
                "qsick.json 里已有的反向地址必须被采纳，而不是被清空"
            assert runtime.config.onebot.reverse_ws_urls == ["ws://127.0.0.1:2536/OneBotv11"]
        finally:
            await runtime.stop()

    async def test_an_existing_store_is_never_rewritten_by_login(self, tmp_path: Path):
        runtime = build(tmp_path)
        with_profile(runtime)
        # The store is one file per account now, so the file to watch is that account's.
        path = runtime.config.path(netprofiles.PROFILE_DIR) / f"{ACCOUNT}.json"
        before = path.read_text(encoding="utf-8")
        try:
            await runtime.sync_account_network(reason="test")
            assert path.read_text(encoding="utf-8") == before, "已有配置不该被登录流程改写"
        finally:
            await runtime.stop()

    async def test_force_reapplies_without_an_account_change(self, tmp_path: Path):
        # This is what the settings page needs: saving a profile changes nothing about who is logged in, but
        # the listener must still be rebuilt.
        runtime = build(tmp_path)
        with_profile(runtime)
        try:
            await runtime.sync_account_network(reason="first")
            same = await runtime.sync_account_network(reason="second")
            assert same["changed"] is False
            forced = await runtime.sync_account_network(force=True, reason="settings saved")
            assert forced["changed"] is True
        finally:
            await runtime.stop()

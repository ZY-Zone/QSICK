"""A logged-in account is served only after it has had time to finish coming up.

A session reports itself logged in before its client can answer anything: the login frame arrives, then the
listener attaches, then the message services come up. Binding a network listener in that window hands the
consumer a connection whose every action fails with "Data Not Existed" or times out -- and the consumer then
reconnects looking for a working one, which is what made the connection look unstable.

So activation waits `NETWORK_ACTIVATION_DELAY` seconds. These tests drive that with a small delay rather than the
real five, and the suite-wide fixture zeroes it for every other test.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from qsick import network_profiles as netprofiles  # noqa: E402
from qsick import runtime as runtime_module  # noqa: E402
from qsick.config import RuntimeConfig  # noqa: E402
from qsick.runtime import Runtime  # noqa: E402
from qsick.transport.base import LoginState  # noqa: E402

ACCOUNT = "3905548034"
URL = "ws://127.0.0.1:2536/onebot"


def build(tmp_path: Path) -> Runtime:
    config = RuntimeConfig(data_dir=str(tmp_path / "data"))
    config.transport.mode = "mock"
    config.log.level = "WARN"
    runtime = Runtime(config)
    netprofiles.save(config, [{
        "id": "p1", "kind": "ws_client", "name": "反向", "enabled": True,
        "url": URL, "protocol": "onebotv11", "instance": ACCOUNT,
    }])
    return runtime


@pytest.fixture()
def short_delay(monkeypatch):
    monkeypatch.setattr(runtime_module, "network_activation_delay", lambda config: 0.2)


class TestActivationWaits:
    async def test_a_fresh_login_is_not_activated_immediately(self, tmp_path: Path, short_delay):
        runtime = build(tmp_path)
        runtime._logins[ACCOUNT] = LoginState(logged_in=True, uin=ACCOUNT, online=True)
        try:
            state = await runtime.sync_account_network(reason="login")
            assert state["accounts"] == [], "刚登录的账号还不该被激活"
            assert runtime._network_accounts == set()
            assert runtime.config.onebot.reverse_ws_urls == []
        finally:
            await runtime.stop()

    async def test_it_activates_once_the_delay_has_passed(self, tmp_path: Path, short_delay):
        runtime = build(tmp_path)
        runtime._logins[ACCOUNT] = LoginState(logged_in=True, uin=ACCOUNT, online=True)
        try:
            await runtime.sync_account_network(reason="login")
            # The timer is what finishes the job; nothing else calls back in.
            assert runtime._network_timer is not None
            await asyncio.wait_for(asyncio.shield(runtime._network_timer), timeout=3)
            assert runtime._network_accounts == {ACCOUNT}
            assert runtime.config.onebot.reverse_ws_urls == [URL], "延时结束后应当激活该账号的反向地址"
        finally:
            await runtime.stop()

    async def test_an_already_settled_account_is_not_delayed_again(self, tmp_path: Path, short_delay):
        # A later sync (a heartbeat, another link push) must not re-impose the delay on an account that is
        # already being served -- otherwise the listener would flap on every state push.
        runtime = build(tmp_path)
        runtime._logins[ACCOUNT] = LoginState(logged_in=True, uin=ACCOUNT, online=True)
        try:
            await runtime.sync_account_network(reason="login")
            await asyncio.wait_for(asyncio.shield(runtime._network_timer), timeout=3)
            again = await runtime.sync_account_network(reason="heartbeat")
            assert again["accounts"] == [ACCOUNT], "已稳定登录的账号不该再被过滤掉"
        finally:
            await runtime.stop()

    async def test_going_offline_takes_effect_at_once(self, tmp_path: Path, short_delay):
        # The delay is about waiting for a client to come up, not about waiting to notice it went away.
        runtime = build(tmp_path)
        runtime._logins[ACCOUNT] = LoginState(logged_in=True, uin=ACCOUNT, online=True)
        try:
            await runtime.sync_account_network(reason="login")
            await asyncio.wait_for(asyncio.shield(runtime._network_timer), timeout=3)
            assert runtime._network_accounts == {ACCOUNT}

            runtime._logins[ACCOUNT] = LoginState(logged_in=False, uin=ACCOUNT, online=False)
            state = await runtime.sync_account_network(reason="offline")
            assert state["accounts"] == []
            assert runtime.config.onebot.reverse_ws_urls == [], "账号离线应当立刻关闭，不等计时器"
        finally:
            await runtime.stop()


class TestSavingSettingsSkipsTheDelay:
    async def test_settle_false_activates_now(self, tmp_path: Path, short_delay):
        # The operator clicked save on an account that is already serving. Making them wait would look like the
        # save did nothing.
        runtime = build(tmp_path)
        runtime._logins[ACCOUNT] = LoginState(logged_in=True, uin=ACCOUNT, online=True)
        try:
            state = await runtime.sync_account_network(
                force=True, reason="settings saved", settle=False)
            assert state["accounts"] == [ACCOUNT]
            assert runtime.config.onebot.reverse_ws_urls == [URL]
        finally:
            await runtime.stop()


class TestTimerLifecycle:
    async def test_stopping_cancels_a_pending_activation(self, tmp_path: Path, short_delay):
        runtime = build(tmp_path)
        runtime._logins[ACCOUNT] = LoginState(logged_in=True, uin=ACCOUNT, online=True)
        await runtime.sync_account_network(reason="login")
        timer = runtime._network_timer
        assert timer is not None and not timer.done()
        await runtime.stop()
        assert timer.cancelled() or timer.done(), "关掉运行时不该留下会触发的计时器"

    async def test_repeated_transitions_share_one_timer(self, tmp_path: Path, short_delay):
        # A login produces several transitions in a row (link state, login frame, first event). One pending timer
        # is enough -- it reads live state when it fires -- and scheduling one per transition would let a busy
        # login postpone activation indefinitely.
        runtime = build(tmp_path)
        runtime._logins[ACCOUNT] = LoginState(logged_in=True, uin=ACCOUNT, online=True)
        try:
            await runtime.sync_account_network(reason="link")
            first = runtime._network_timer
            await runtime.sync_account_network(reason="login frame")
            await runtime.sync_account_network(reason="first event")
            assert runtime._network_timer is first
        finally:
            await runtime.stop()

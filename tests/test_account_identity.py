"""Identity must follow the request's account, not the runtime's primary one.

Found by live testing: with two accounts logged in (2208948865 and 3905548034), every Milky
`get_login_info` reported 2208948865 -- even when the request body pinned 3905548034 and the
network profile was owned by 3905548034. `ActionContext.login` is a runtime-wide view
("at least one instance is logged in"), so `self_id` was the primary account no matter which
account the request named.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
for entry in (str(ROOT), str(ROOT / "src")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from qsick.transport.base import LoginState  # noqa: E402


def context(account: str, logins: dict, primary: str):
    """A real ActionContext over a runtime that has two accounts logged in."""
    from qsick.onebot.context import ActionContext

    runtime = SimpleNamespace(_logins=logins, self_id=int(primary) if primary.isdigit() else 0)
    return ActionContext(
        runtime=runtime,
        transport=SimpleNamespace(),
        config=SimpleNamespace(),
        store=SimpleNamespace(),
        rkey=None,
        media=None,
        login=SimpleNamespace(logged_in=True, online=True, uin=primary, nickname="primary",
                              kicked=False, security_kicked=False, kick_reason=""),
        account=account,
    )


def two_accounts():
    return {
        "2208948865": LoginState(logged_in=True, uin="2208948865", nickname="owner", online=True),
        "3905548034": LoginState(logged_in=True, uin="3905548034", nickname="member", online=True),
    }


def test_self_id_follows_the_requested_account():
    logins = two_accounts()
    assert context("3905548034", logins, "2208948865").self_id == 3905548034
    assert context("2208948865", logins, "2208948865").self_id == 2208948865


def test_self_id_falls_back_when_no_account_is_pinned():
    """An instance name is not a uin; the runtime view is still the right answer there."""
    logins = two_accounts()
    assert context("", logins, "3905548034").self_id == 3905548034
    assert context("C0", logins, "3905548034").self_id == 3905548034


def test_login_state_is_matched_by_uin_not_by_key():
    """Headless instances key the map by instance id; pipe sessions key it by uin."""
    logins = {"C0": LoginState(logged_in=True, uin="111111", nickname="headless", online=True)}
    state = context("111111", logins, "111111").login_state_for()
    assert state is not None and state.nickname == "headless"
    assert context("999999", logins, "111111").login_state_for() is None


def test_get_login_info_answers_for_the_requested_account():
    from qsick.onebot.actions.system.get_login_info import _handle

    logins = two_accounts()
    owner = asyncio.run(_handle(context("2208948865", logins, "2208948865")))
    member = asyncio.run(_handle(context("3905548034", logins, "2208948865")))
    assert owner == {"user_id": 2208948865, "nickname": "owner"}
    assert member == {"user_id": 3905548034, "nickname": "member"}


def test_get_status_reports_the_requested_account():
    from qsick.onebot.actions.system.get_status import _handle

    logins = two_accounts()
    result = asyncio.run(_handle(context("3905548034", logins, "2208948865")))
    assert result["user_id"] == 3905548034

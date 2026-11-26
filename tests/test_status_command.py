"""The `#QS` status command.

Three separate questions, tested separately because they fail differently:

1. **Is this message the command?** An exact match on the text segments. A message that merely starts
   with `#QS` is conversation, and a bot that answers every one is a bot nobody can talk to.
2. **Should this account answer?** Per-session opt-in, read from that session's own config -- one
   machine can run a bot account that answers and a personal one that stays quiet.
3. **What does the reply say?** Five fixed lines, with the uptime formatted by the same function the
   overview page uses.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
for entry in (str(ROOT), str(ROOT / "src")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from qsick import status_command  # noqa: E402


# ------------------------------------------------------------------ is it the command


@pytest.mark.parametrize("text", ["#QS", " #QS ", "#qs", "#Qs", "\t#QS\n"])
def test_the_command_is_matched_after_trimming_and_case_folding(text):
    assert status_command.is_status_command(text)


@pytest.mark.parametrize("text", [
    "", "#QS你好", "#QS 你好", "你好 #QS", "#qsick", "QS", "#", "##QS", "# Q S",
])
def test_anything_else_is_conversation(text):
    assert not status_command.is_status_command(text)


def test_the_command_word_is_what_the_operator_documented():
    assert status_command.COMMAND == "#QS"


# ------------------------------------------------------------------------ the reply


def test_the_reply_is_the_five_agreed_lines():
    reply = status_command.build_reply(version="0.3.0", uptime="1小时 2分 3秒",
                                       account="3905548034")
    assert reply.splitlines() == [
        "QSICK状态",
        "QSICK版本：0.3.0",
        "运行时间：1小时 2分 3秒",
        "当前账号：3905548034",
        "欢迎访问http://www.sickcc.online/QSICK了解更多",
    ]


def test_the_uptime_is_passed_in_not_recomputed():
    """The overview page formats uptime through `runtime.info()`; a second formatter here would
    eventually disagree with it, and the operator would see two different numbers."""
    reply = status_command.build_reply(version="1", uptime="刚刚", account="1")
    assert "运行时间：刚刚" in reply


# ------------------------------------------------------- per-session persistence


@pytest.fixture()
def session_root(tmp_path):
    from qsick import headless

    directory = headless.session_dll_dir(tmp_path) / "k3m9"
    directory.mkdir(parents=True)
    (directory / headless.PIPE_DLL_CONFIG).write_text(json.dumps({
        "session": {"tmp_id": "k3m9", "port": 7601, "nowport": 7601},
        "port": 7601, "nowport": 7601,
    }), encoding="utf-8")
    return tmp_path


def test_the_switch_defaults_to_off(session_root):
    from qsick import headless

    assert headless.session_status_command(
        headless.read_session_config("k3m9", session_root)) is False


def test_turning_it_on_survives_a_reread_and_keeps_the_rest_of_the_config(session_root):
    from qsick import headless

    headless.set_session_status_command("k3m9", True, session_root)
    config = headless.read_session_config("k3m9", session_root)
    assert headless.session_status_command(config) is True
    # The port block is untouched: this switch is not allowed to move anything it does not own.
    assert headless.session_ports(config) == (7601, 7601)


def test_turning_it_off_again(session_root):
    from qsick import headless

    headless.set_session_status_command("k3m9", True, session_root)
    headless.set_session_status_command("k3m9", False, session_root)
    assert headless.session_status_command(
        headless.read_session_config("k3m9", session_root)) is False


def test_the_session_list_carries_the_flag(session_root):
    from qsick import headless

    headless.set_session_status_command("k3m9", True, session_root)
    rows = headless.read_session_dirs(session_root)
    assert rows and rows[0]["status_command"] is True


def test_a_session_without_the_key_reports_off(session_root):
    from qsick import headless

    rows = headless.read_session_dirs(session_root)
    assert rows[0]["status_command"] is False


# --------------------------------------------------------- the runtime responder


def _runtime_for(rows):
    """A Runtime whose session list reports the given rows.

    The rows are attached through links, not written straight into `_session_list`: `session_rows()`
    merges each link's own state over the config entry, so a bare list entry has its `account` dict
    replaced with an empty one -- which silently disabled the responder and made the first version of
    this test pass for the wrong reason.
    """
    from qsick.config import RuntimeConfig
    from qsick.runtime import Runtime

    runtime = Runtime(RuntimeConfig(data_dir="temp/status-command-test"))
    runtime._session_list = [{"tmp_id": row["tmp_id"]} for row in rows]
    for row in rows:
        extra = {key: value for key, value in row.items() if key != "tmp_id"}
        runtime._links[row["tmp_id"]] = SimpleNamespace(
            state=SimpleNamespace(as_row=lambda extra=extra: dict(extra)))
    return runtime


def _event(uin, text, *, message_type="group", group_id=2169010946, user_id=1927676487):
    return {
        "post_type": "message", "message_type": message_type, "self_id": int(uin),
        "group_id": group_id, "user_id": user_id, "target_id": user_id,
        "message": [{"type": "text", "data": {"text": text}}],
    }


class Recorder:
    """Stands in for `Runtime.invoke` and keeps what was asked for."""

    def __init__(self, fail=False):
        self.calls: list[tuple[str, dict]] = []
        self.fail = fail

    async def __call__(self, action, params=None):
        self.calls.append((action, dict(params or {})))
        if self.fail:
            raise RuntimeError("transport down")
        return {"status": "ok", "retcode": 0, "data": {"message_id": 1}}


async def respond(rows, event, *, fail=False):
    runtime = _runtime_for(rows)
    recorder = Recorder(fail=fail)
    runtime.invoke = recorder  # type: ignore[method-assign]
    answered = await runtime.answer_status_command(event)
    return answered, recorder.calls


ROW_ON = {"tmp_id": "k3m9", "status_command": True,
          "account": {"uin": "3905548034"}, "running": True, "logged_in": True}
ROW_OFF = {"tmp_id": "w7p2", "status_command": False,
           "account": {"uin": "3905548034"}, "running": True, "logged_in": True}


async def test_a_group_message_gets_a_group_reply_from_that_account():
    answered, calls = await respond([ROW_ON], _event(3905548034, "#QS"))
    assert answered is True
    assert len(calls) == 1
    action, params = calls[0]
    assert action == "send_group_msg"
    assert params["group_id"] == 2169010946
    # The reply is sent by the account that received the command, not by whichever session is first.
    assert params["account"] == "3905548034"
    assert params["message"].startswith("QSICK状态")


async def test_a_private_message_gets_a_private_reply():
    answered, calls = await respond([ROW_ON], _event(3905548034, "#QS", message_type="private"))
    assert answered is True
    action, params = calls[0]
    assert action == "send_private_msg"
    assert params["user_id"] == 1927676487


async def test_a_session_with_the_switch_off_stays_silent():
    """The whole point of a per-session switch: this account must not answer."""
    answered, calls = await respond([ROW_OFF], _event(3905548034, "#QS"))
    assert answered is False
    assert calls == []


async def test_an_account_with_no_session_at_all_stays_silent():
    answered, calls = await respond([ROW_ON], _event(1234567890, "#QS"))
    assert answered is False
    assert calls == []


async def test_a_session_that_is_switched_on_but_not_logged_in_contributes_nothing():
    row = dict(ROW_ON, logged_in=False, account={})
    answered, calls = await respond([row], _event(3905548034, "#QS"))
    assert answered is False
    assert calls == []


async def test_ordinary_text_does_not_trigger_a_reply():
    for text in ("你好", "#QS你好", "QSICK状态"):
        answered, calls = await respond([ROW_ON], _event(3905548034, text))
        assert answered is False, text
        assert calls == []


async def test_a_non_text_segment_does_not_count_as_the_command():
    """`@bot #QS` is someone talking to the bot, not asking it for its status."""
    event = _event(3905548034, "")
    event["message"] = [{"type": "at", "data": {"qq": "3905548034"}},
                        {"type": "text", "data": {"text": " #QS"}}]
    answered, calls = await respond([ROW_ON], event)
    assert answered is False
    assert calls == []


async def test_a_sent_message_is_not_a_command():
    """The reply itself is a message this account sends; answering it would be an echo loop."""
    event = _event(3905548034, "#QS")
    event["post_type"] = "message_sent"
    answered, calls = await respond([ROW_ON], event)
    assert answered is False
    assert calls == []


async def test_a_failing_send_is_swallowed():
    """A status reply must never disturb the event flow, so the failure is logged and dropped."""
    answered, calls = await respond([ROW_ON], _event(3905548034, "#QS"), fail=True)
    assert answered is True
    assert len(calls) == 1

def test_the_segment_predicate_requires_every_segment_to_be_text():
    """Kept next to the string predicate because the difference between them is the whole rule."""
    from qsick.status_command import is_status_command_segments

    assert is_status_command_segments([{"type": "text", "data": {"text": "#QS"}}])
    assert is_status_command_segments(
        [{"type": "text", "data": {"text": " #QS "}}])
    # Two text segments that concatenate to the command are still the command.
    assert is_status_command_segments([
        {"type": "text", "data": {"text": "#"}},
        {"type": "text", "data": {"text": "QS"}},
    ])
    # Any non-text segment disqualifies the message.
    assert not is_status_command_segments([
        {"type": "at", "data": {"qq": "3905548034"}},
        {"type": "text", "data": {"text": " #QS"}},
    ])
    assert not is_status_command_segments([{"type": "image", "data": {"file": "x"}}])
    # Degenerate inputs are not the command either.
    assert not is_status_command_segments([])
    assert not is_status_command_segments(None)
    assert not is_status_command_segments([{"type": "text"}])
    assert not is_status_command_segments([{"nope": 1}])

"""Actions that can target a private chat must not be stopped by the shared `group_id` pre-check.

The dispatcher validates every op through one check:

    const groupId = NO_GROUP_ID_OPS.has(op) ? '' : id(p.group_id, 'group_id');

which throws when `group_id` is absent. That is correct for the group-scoped ops. For anything that can
also address a private chat it is wrong, and the failure is invisible until a caller tries it: the error
blames `group_id` even though the handler below would have resolved `user_id` fine.

Two real cases, both measured through a live OneBot adapter:

    send_private_forward_msg   {user_id, messages:[node…]}  -> "group_id must be …"
    message.history            {user_id, count}             -> "group_id must be …"

These call the shipped dispatcher directly with stub services, so they need no QQ process and no network:
the assertion is that validation gets *past* the pre-check, which shows up as a different message (or no
message at all).
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DISPATCHER = ROOT / "src" / "qsick" / "headless_actions.cjs"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is required")

#: Drives the real dispatcher. Each case is `[op, params]`; the script reports the first line of whatever
#: error came back, or null when the call got through validation.
_HARNESS = r"""
const path = require("path");
const mod = require(process.argv[1]);
const cases = JSON.parse(process.argv[2]);
const msgService = {
  multiForwardMsgWithComment: async () => ({ result: 0 }),
  getMultiMsg: async () => ({ result: 0, msgList: [] }),
};
const dispatcher = mod.createActionDispatcher({
  getSession: () => ({ getMsgService: () => msgService }),
  getIdentity: async () => ({ logged_in: true, uin: "1" }),
  resolveUid: async (uin) => "u_" + uin,
});
(async () => {
  const out = [];
  for (const [op, params] of cases) {
    try {
      await dispatcher.execute(op, params);
      out.push(null);
    } catch (error) {
      out.push(String((error && error.message) || error).split("\n")[0]);
    }
  }
  process.stdout.write(JSON.stringify(out));
})();
"""

GROUP_ID_COMPLAINT = "group_id must be a positive canonical decimal ID"


def run_cases(cases: list[list[object]]) -> list[str | None]:
    completed = subprocess.run(
        ["node", "-e", _HARNESS, str(DISPATCHER), json.dumps(cases)],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def test_private_forward_is_not_blocked_by_the_group_check():
    """`send_private_forward_msg` with only `user_id` must reach its handler.

    Its handler reads `group_id` and `user_id` as alternatives (resolving the latter to a UID), so the
    pre-check demanding `group_id` made the private half unreachable.
    """
    [message] = run_cases([["send_private_forward_msg", {
        "user_id": 2208948865,
        "messages": [{"message_id": 1, "name": "x"}],
    }]])
    assert message != GROUP_ID_COMPLAINT, message


def test_private_history_is_not_blocked_by_the_group_check():
    """`message.history` with only `user_id` previously failed before its own validation ran."""
    [message] = run_cases([["message.history", {"user_id": 2208948865, "count": 5}]])
    assert message != GROUP_ID_COMPLAINT, message


def test_group_scoped_actions_still_require_a_group():
    """The exemption must not be widened into "no op validates its target".

    A group action called without `group_id` has to keep failing, or a caller mistake turns into a call
    against an empty id.
    """
    [message] = run_cases([["group.kick", {"user_id": 2208948865}]])
    assert message == GROUP_ID_COMPLAINT, message


def test_group_history_still_works():
    """The paired assertion for the fix: the group half was never broken and stays working.

    Asserted against the pre-check message rather than against a clean return: the stub services cannot
    answer a history read, so the call fails further in. Getting past validation is the point -- a clean
    `None` here would only mean the stub grew a fake database.
    """
    [message] = run_cases([["message.history", {"group_id": 2169010946, "count": 5}]])
    assert message != GROUP_ID_COMPLAINT, message

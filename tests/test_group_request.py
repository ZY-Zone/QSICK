"""Group requests: the flag, the applicant, and the op that never existed.

Three defects are pinned here, all measured rather than imagined:

1. `set_group_add_request` called `request.group`, which the payload does not implement — so every
   call answered `Unsupported operation: request.group`. Same class as `download_file` and
   `oidb_call`: the action was listed as implemented and did nothing.

2. The `request` event for a join application carried `user_id: 0`. Measured in the operator's own
   consumer log: `[3905548034 <= 2169010946, 0]`. The applicant is identified by *uid* in the frame,
   and a stranger applying to join has never appeared in our traffic, so the uid->uin table cannot
   answer — writing 0 there invented a user.

3. There was no way to get a request's `flag`, which is the kernel's `GroupNotify.seq`. It is not in
   the OLPush frame at all; it only comes from the notification list.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
for entry in (str(ROOT), str(ROOT / "src")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from qsick.onebot.actions.group_admin.get_group_notices import GetGroupNoticesAction  # noqa: E402
from qsick.onebot.actions.request.set_group_add_request import SetGroupAddRequestAction  # noqa: E402
from qsick.onebot.registry import ActionError  # noqa: E402

MODULE = ROOT / "src" / "qsick" / "headless_actions.cjs"


class Ctx:
    account = "3905548034"

    def __init__(self, reply=None) -> None:
        self.seen: list[tuple[str, dict]] = []
        self._reply = reply or {}

    async def call(self, op, params, label="", **kwargs):
        self.seen.append((op, dict(params)))
        return SimpleNamespace(ok=True, data=dict(self._reply))


# ------------------------------------------------------------------ the notice list


async def test_the_notice_list_reports_flag_and_uid():
    ctx = Ctx({"doubt": False, "count": 1, "notifies": [{
        "flag": "7786745039323557773", "type": 7, "status": 1, "group_id": 2169010946,
        "group_name": "test", "user_id_uid": "u_abc", "nickname": "申请人",
        "comment": "我填的验证信息",
    }]})
    result = await GetGroupNoticesAction().run(ctx)
    assert result["count"] == 1
    row = result["notifies"][0]
    # The two things nothing else can supply.
    assert row["flag"] == "7786745039323557773"
    assert row["user_id_uid"] == "u_abc"
    assert row["comment"] == "我填的验证信息"
    # The applicant number is *not* claimed: only the uid is known.
    assert "user_id" not in row


async def test_the_notice_list_never_invents_a_user_id():
    """`user_id_uid` is named that way on purpose -- it must not read like a QQ number."""
    ctx = Ctx({"notifies": [{"flag": "1", "user_id_uid": "", "type": 7}]})
    result = await GetGroupNoticesAction().run(ctx)
    assert result["notifies"][0]["user_id_uid"] == ""
    assert result["notifies"][0].get("user_id") is None


async def test_it_calls_the_new_op():
    ctx = Ctx({"notifies": []})
    await GetGroupNoticesAction().run(ctx)
    assert [op for op, _ in ctx.seen] == ["group.notices"]


# -------------------------------------------------------------------- the operation


async def test_approving_passes_the_flag_and_the_agree_code():
    ctx = Ctx({"seq": "7786745039323557773", "action": "agree", "result": None})
    result = await SetGroupAddRequestAction().run(
        ctx, flag="7786745039323557773", approve=True, sub_type="add", group_id=2169010946)
    op, payload = ctx.seen[0]
    assert op == "group.request.operate"
    assert payload["seq"] == "7786745039323557773"
    assert payload["action"] == "agree"
    assert payload["group_code"] == "2169010946"
    assert result["action"] == "agree"


async def test_refusing_carries_the_reason():
    ctx = Ctx({"result": None})
    await SetGroupAddRequestAction().run(
        ctx, flag="1", approve=False, reason="不加人")
    payload = ctx.seen[0][1]
    assert payload["action"] == "refuse"
    assert payload["postscript"] == "不加人"


async def test_a_missing_flag_is_refused_before_calling_out():
    """The flag cannot be guessed: it is a server-side handle, not derivable from the message."""
    ctx = Ctx()
    with pytest.raises(ActionError, match="flag is required"):
        await SetGroupAddRequestAction().run(ctx, approve=True)
    assert ctx.seen == []


async def test_a_numeric_type_is_forwarded_but_a_word_is_not():
    """`type` is the OneBot sub-type *and* the kernel's notify type; only the number means the latter."""
    ctx = Ctx({"result": None})
    await SetGroupAddRequestAction().run(ctx, flag="1", approve=True, type=7)
    assert ctx.seen[0][1]["type"] == 7

    ctx = Ctx({"result": None})
    await SetGroupAddRequestAction().run(ctx, flag="1", approve=True, type="add")
    assert "type" not in ctx.seen[0][1]
    assert ctx.seen[0][1]["action"] == "agree"


def test_the_op_this_action_calls_really_exists():
    """The exact defect this action had: it called `request.group`, which is not declared."""
    source = MODULE.read_text(encoding="utf-8")
    start = source.index("const REQUIREMENTS = Object.freeze({")
    end = source.index("});", start)
    declared = source[start:end]
    assert "'group.request.operate':" in declared
    assert "'group.notices':" in declared
    assert "op === 'group.request.operate'" in source
    assert "op === 'group.notices'" in source


# ------------------------------------------------------- the event that had user_id 0


def test_an_unresolvable_applicant_omits_the_number_and_keeps_the_uid():
    """The operator's log showed `user_id: 0` for a real application.

    A stranger applying to join has never been in our traffic, so no uid->uin fact exists. `0` is not
    a QQ number, and a consumer that reads it addresses nobody -- so the field is omitted rather than
    filled with a placeholder, and the uid travels instead.
    """
    from qsick.olpush import PushEventMapper

    mapper = PushEventMapper(lambda _uid: "", self_uin="3905548034")
    event = mapper._group_request(2169010946, "u_stranger", "我填的验证信息")
    assert event is not None
    assert "user_id" not in event, "an unresolved applicant must not be reported as user 0"
    assert event["user_id_uid"] == "u_stranger"
    assert event["comment"] == "我填的验证信息"
    assert event["request_type"] == "group"


def test_a_resolvable_applicant_still_gets_a_number():
    from qsick.olpush import PushEventMapper

    mapper = PushEventMapper(lambda uid: "1927676487" if uid == "u_known" else "",
                             self_uin="3905548034")
    event = mapper._group_request(2169010946, "u_known", "")
    assert event["user_id"] == 1927676487
    assert event["user_id_uid"] == "u_known"


def test_doubt_is_captured_from_the_callback_and_not_referenced_out_of_scope():
    """Measured failure: `doubt is not defined`, thrown *after* the call had already succeeded.

    `groupEvent`'s selector receives `(doubt, list)` and returns the list; the rows are built outside
    that closure, so `doubt` has to be captured. A working request that then throws is the worst
    shape of bug -- the notifications were fetched and the caller saw an error.
    """
    source = MODULE.read_text(encoding="utf-8")
    start = source.index("if (op === 'group.notices')")
    end = source.index("if (op === 'group.request.operate')")
    body = source[start:end]
    assert "let doubtFlag = false;" in body
    assert "doubtFlag = Boolean(doubt);" in body
    assert "doubt: doubtFlag" in body
    # And it must not reference the bare name in the returned object.
    assert "doubt: Boolean(doubt)" not in body

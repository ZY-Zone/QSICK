"""`service.call` — the transport used to discover signatures that are not published.

The op exists because knowing a method name is only half the answer. `Group.setIdentityTitleInfo`,
`Msg.setMsgEmojiLikes`, `Msg.sendNudge` and `Group.operateSysNotify` are all present on this client,
and none of their argument shapes is documented anywhere this project can read. Calling one with no
arguments and reading the error is how the shape gets found; guessing it produces a failure that
reads like a broken feature.

Nothing here asserts that those four work -- only that the transport forwards what it is given and
reports the answer, or the complaint, without inventing either.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
for entry in (str(ROOT), str(ROOT / "src")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from qsick.onebot.actions.system.service_call import ServiceCallAction  # noqa: E402
from qsick.onebot.registry import ActionError  # noqa: E402

MODULE = ROOT / "src" / "qsick" / "headless_actions.cjs"
NODE = shutil.which("node")
requires_node = pytest.mark.skipif(NODE is None, reason="node is unavailable")


def run_js(script: str) -> str:
    assert NODE is not None
    completed = subprocess.run([NODE, "-e", script], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=25)
    assert completed.returncode == 0, (completed.stdout or "") + (completed.stderr or "")
    return completed.stdout


class Ctx:
    account = "3905548034"

    def __init__(self, reply=None) -> None:
        self.seen: list[dict] = []
        self._reply = reply or {}

    async def call(self, op, params, label="", **kwargs):
        self.seen.append({"op": op, **dict(params)})
        return SimpleNamespace(ok=True, data=dict(self._reply))


# ------------------------------------------------------------------- core side


async def test_the_arguments_are_forwarded_untouched():
    ctx = Ctx({"service": "Group", "method": "setGroupRemark", "arg_count": 2, "returned": "object",
               "result": {"result": 0}})
    result = await ServiceCallAction().run(
        ctx, service="Group", method="setGroupRemark", args=[2169010946, "x"])
    assert ctx.seen[0]["op"] == "service.call"
    assert ctx.seen[0]["args"] == [2169010946, "x"]
    assert result["result"] == {"result": 0}


async def test_no_args_is_the_probing_form():
    ctx = Ctx({"service": "Group", "method": "setIdentityTitleInfo", "arg_count": 0,
               "returned": "undefined", "result": None})
    result = await ServiceCallAction().run(ctx, service="Group", method="setIdentityTitleInfo")
    assert ctx.seen[0]["args"] == []
    assert result["arg_count"] == 0
    # A void method is still a successful call, and the reply says which it was.
    assert result["returned"] == "undefined"


async def test_a_missing_service_or_method_is_refused_locally():
    for params, message in (
        ({"method": "x"}, "service is required"),
        ({"service": "Group"}, "method is required"),
    ):
        with pytest.raises(ActionError, match=message):
            await ServiceCallAction().run(Ctx(), **params)


async def test_a_path_like_service_name_is_refused():
    """The name is interpolated into `get<Name>Service`, so it has to stay a bare identifier."""
    for bad in ("Gr.oup", "../Group", "Group Service"):
        with pytest.raises(ActionError, match="bare name"):
            await ServiceCallAction().run(Ctx(), service=bad, method="x")


async def test_a_non_array_args_value_is_refused():
    with pytest.raises(ActionError, match="args must be an array"):
        await ServiceCallAction().run(Ctx(), service="Group", method="x", args={"a": 1})


async def test_too_many_arguments_are_refused():
    with pytest.raises(ActionError, match="at most 24"):
        await ServiceCallAction().run(Ctx(), service="Group", method="x", args=list(range(25)))


# ----------------------------------------------------------------- payload side


@requires_node
def test_it_calls_the_method_on_the_named_service():
    script = """
const { createActionDispatcher } = require(MODULE);
(async () => {
  const calls = [];
  const group = {
    setIdentityTitleInfo: function (groupId, memberUin, title) {
      calls.push([groupId, memberUin, title]);
      return {result: 0, title};
    },
  };
  const session = {getGroupService: () => group};
  const dispatcher = createActionDispatcher({
    getSession: () => session, getIdentity: () => ({uin: '1'}),
  });
  const out = await dispatcher.execute('service.call', {
    service: 'Group', method: 'setIdentityTitleInfo', args: [2169010946, 3905548034, '头衔'],
  });
  console.log(JSON.stringify({out, calls}));
})().catch(error => {console.error(error); process.exitCode = 1;});
""".replace("MODULE", json.dumps(str(MODULE)))
    result = json.loads(run_js(script))
    assert result["calls"] == [[2169010946, 3905548034, "头衔"]]
    assert result["out"]["result"] == {"result": 0, "title": "头衔"}
    assert result["out"]["arg_count"] == 3


@requires_node
def test_a_rejected_call_reports_the_complaint_not_a_generic_error():
    """The complaint is the payload of a probing call: it names the missing property."""
    script = """
const { createActionDispatcher } = require(MODULE);
(async () => {
  const group = {
    setIdentityTitleInfo: function () { throw new TypeError('Cannot read properties of undefined (reading \\'groupCode\\')'); },
  };
  const session = {getGroupService: () => group};
  const dispatcher = createActionDispatcher({
    getSession: () => session, getIdentity: () => ({uin: '1'}),
  });
  try {
    await dispatcher.execute('service.call', {service: 'Group', method: 'setIdentityTitleInfo'});
    console.log(JSON.stringify({threw: false}));
  } catch (error) {
    console.log(JSON.stringify({threw: true, code: error.code, message: error.message,
                                details: error.details}));
  }
})();
""".replace("MODULE", json.dumps(str(MODULE)))
    result = json.loads(run_js(script))
    assert result["threw"] is True
    assert result["code"] == "UPSTREAM"
    assert "groupCode" in result["message"]
    assert result["details"]["service"] == "Group"
    assert result["details"]["method"] == "setIdentityTitleInfo"


@requires_node
def test_a_missing_method_lists_what_the_service_does_have():
    script = """
const { createActionDispatcher } = require(MODULE);
(async () => {
  class Group { setIdentityTitleInfo() {} setGroupRemark() {} }
  const session = {getGroupService: () => new Group()};
  const dispatcher = createActionDispatcher({
    getSession: () => session, getIdentity: () => ({uin: '1'}),
  });
  try {
    await dispatcher.execute('service.call', {service: 'Group', method: 'nope'});
    console.log(JSON.stringify({threw: false}));
  } catch (error) {
    console.log(JSON.stringify({threw: true, code: error.code, message: error.message,
                                methods: (error.details || {}).methods}));
  }
})();
""".replace("MODULE", json.dumps(str(MODULE)))
    result = json.loads(run_js(script))
    assert result["threw"] is True
    assert result["code"] == "UNSUPPORTED"
    assert "setIdentityTitleInfo" in result["methods"]


@requires_node
def test_an_unserialisable_result_is_described_not_silently_empty():
    script = """
const { createActionDispatcher } = require(MODULE);
(async () => {
  const msg = {sendNudge: function () { const m = new Map(); m.set('a', 1); return m; }};
  const session = {getMsgService: () => msg};
  const dispatcher = createActionDispatcher({
    getSession: () => session, getIdentity: () => ({uin: '1'}),
  });
  const out = await dispatcher.execute('service.call', {service: 'Msg', method: 'sendNudge'});
  console.log(JSON.stringify(out));
})();
""".replace("MODULE", json.dumps(str(MODULE)))
    result = json.loads(run_js(script))
    # A Map stringifies to {}; saying so beats handing back an empty object that looks like "no data".
    assert result["result"] == {}
    assert result["returned"] == "object"


@requires_node
def test_a_dotted_method_name_is_refused():
    script = """
const { createActionDispatcher } = require(MODULE);
(async () => {
  const session = {getGroupService: () => ({})};
  const dispatcher = createActionDispatcher({
    getSession: () => session, getIdentity: () => ({uin: '1'}),
  });
  try {
    await dispatcher.execute('service.call', {service: 'Group', method: 'a.b'});
    console.log(JSON.stringify({threw: false}));
  } catch (error) {
    console.log(JSON.stringify({threw: true, code: error.code}));
  }
})();
""".replace("MODULE", json.dumps(str(MODULE)))
    result = json.loads(run_js(script))
    assert result["threw"] is True
    assert result["code"] == "INVALID_PARAMS"

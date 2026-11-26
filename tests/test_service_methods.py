"""`service.methods` — ask the client what it can do, instead of guessing OIDB numbers.

Why this action exists
----------------------
Four capabilities were reported missing: a group member's special title, a message reaction, a nudge,
and approving or rejecting a join request. None has an entry in the payload's operation table,
because no kernel service method was found for them.

The obvious alternative -- guess OIDB command numbers and see what answers -- was measured to be a
dead end. On `0x6D6_2`, whose correct request body is known, four plausible bodies were tried:

    empty                                  -> errorCode 253
    field1 varint 0                        -> 253
    field1 varint 1, field2 varint 0       -> 253
    field1 length-delimited, empty         -> 253

Every one answers "invalid pkg and decode req failed". A body that is merely valid protobuf is not
enough; the server needs the exact schema. So a probe whose bodies are guesses cannot distinguish
"this command does not exist" from "my body is wrong", and the signal is worthless.

Reading the method names off the service objects answers the question directly, and does it for the
whole client in one call.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for entry in (str(ROOT), str(ROOT / "src")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from qsick.onebot.actions.system.service_methods import ServiceMethodsAction  # noqa: E402
from qsick.onebot.registry import ActionError  # noqa: E402

MODULE = ROOT / "src" / "qsick" / "headless_actions.cjs"
NODE = shutil.which("node")
requires_node = pytest.mark.skipif(NODE is None, reason="node is unavailable")


class Ctx:
    account = "3905548034"

    def __init__(self, reply=None) -> None:
        self.seen: list[dict] = []
        self._reply = reply or {}

    async def call(self, op, params, label="", **kwargs):
        from types import SimpleNamespace

        self.seen.append({"op": op, **dict(params)})
        return SimpleNamespace(ok=True, data=dict(self._reply))


# ------------------------------------------------------------------- core side


async def test_a_named_service_returns_its_methods():
    ctx = Ctx({"service": "Msg", "filtered": True, "total": 120, "methods": ["sendMsg", "recallMsg"]})
    result = await ServiceMethodsAction().run(ctx, service="Msg", contains="msg")
    assert ctx.seen[0]["op"] == "service.methods"
    assert ctx.seen[0]["service"] == "Msg"
    assert ctx.seen[0]["contains"] == "msg"
    assert result["methods"] == ["sendMsg", "recallMsg"]
    assert result["matched"] == 2


async def test_the_survey_form_asks_for_everything_in_one_call():
    """One call, not one per service: each probe otherwise costs a session rebuild."""
    ctx = Ctx({"service": "*", "filtered": True, "total": 900,
               "services": {"Msg": {"total": 1, "methods": ["setMsgEmojiLike"]}}})
    result = await ServiceMethodsAction().run(ctx, service="*", contains="emoji")
    assert ctx.seen[0]["service"] == "*"
    assert result["services"] == {"Msg": {"total": 1, "methods": ["setMsgEmojiLike"]}}
    assert result["matched"] == 1


async def test_an_empty_service_is_refused():
    with pytest.raises(ActionError, match="service is required"):
        await ServiceMethodsAction().run(Ctx(), service="")


async def test_a_dotted_or_path_like_name_is_refused():
    """The name is interpolated into `get<Name>Service`, so it must stay a bare identifier."""
    for bad in ("Ms.g", "../Msg", "Msg Service"):
        with pytest.raises(ActionError, match="bare name"):
            await ServiceMethodsAction().run(Ctx(), service=bad)


async def test_no_filter_is_not_forwarded():
    ctx = Ctx({"service": "Msg", "filtered": False, "methods": []})
    await ServiceMethodsAction().run(ctx, service="Msg")
    assert "contains" not in ctx.seen[0]


# ----------------------------------------------------------------- payload side


def run_js(script: str) -> str:
    assert NODE is not None
    completed = subprocess.run([NODE, "-e", script], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=25)
    assert completed.returncode == 0, (completed.stdout or "") + (completed.stderr or "")
    return completed.stdout


@requires_node
def test_it_reads_methods_off_the_prototype_chain():
    """The services are class instances, so own properties are not where the methods are."""
    script = """
class Base { getCommonInfo() {} }
class MsgService extends Base {
  constructor() { super(); this.state = 1; }
  sendMsg() {}
  setMsgEmojiLike() {}
  recallMsg() {}
}
const { createActionDispatcher } = require(MODULE);
(async () => {
  const session = {
    getMsgService: () => new MsgService(),
    getGroupService: () => ({ getGroupList() {}, setGroupRemark() {} }),
  };
  const dispatcher = createActionDispatcher({
    getSession: () => session, getIdentity: () => ({uin: '1'}),
  });
  const one = await dispatcher.execute('service.methods', {service: 'Msg', contains: 'emoji'});
  const all = await dispatcher.execute('service.methods', {service: '*', contains: 'remark'});
  console.log(JSON.stringify({one, all}));
})().catch(error => {console.error(error); process.exitCode = 1;});
""".replace("MODULE", json.dumps(str(MODULE)))
    result = json.loads(run_js(script))
    # Own properties are not methods; the inherited one is.
    assert result["one"]["methods"] == ["setMsgEmojiLike"]
    assert result["all"]["services"]["Group"]["methods"] == ["setGroupRemark"]


@requires_node
def test_a_missing_service_getter_says_which_ones_exist():
    script = """
const { createActionDispatcher } = require(MODULE);
(async () => {
  const dispatcher = createActionDispatcher({
    getSession: () => ({getMsgService: () => ({})}), getIdentity: () => ({uin: '1'}),
  });
  try {
    await dispatcher.execute('service.methods', {service: 'Nope'});
    console.log(JSON.stringify({threw: false}));
  } catch (error) {
    console.log(JSON.stringify({threw: true, code: error.code, message: error.message,
                                getters: (error.details || {}).getters}));
  }
})();
""".replace("MODULE", json.dumps(str(MODULE)))
    result = json.loads(run_js(script))
    assert result["threw"] is True
    assert result["code"] == "UNSUPPORTED"
    assert result["getters"] == ["getMsgService"]


@requires_node
def test_the_survey_neither_calls_methods_nor_fires_accessors():
    """A survey that invoked a method (or ran a getter) would be a write operation wearing a read's
    name. Property *descriptors* are what it reads, so neither can happen."""
    script = """
const { createActionDispatcher } = require(MODULE);
(async () => {
  const called = [];
  const fired = [];
  const target = {};
  const service = new Proxy(target, {
    ownKeys: () => ['sendMsg', 'setMsgEmojiLike'],
    getOwnPropertyDescriptor: (_t, prop) => ({
      value: function () { called.push(String(prop)); },
      enumerable: true, configurable: true, writable: true,
    }),
    get: (_t, prop) => { fired.push(String(prop)); return undefined; },
  });
  const session = {getMsgService: () => service};
  const dispatcher = createActionDispatcher({
    getSession: () => session, getIdentity: () => ({uin: '1'}),
  });
  const result = await dispatcher.execute('service.methods', {service: 'Msg'});
  console.log(JSON.stringify({methods: result.methods, called, fired}));
})();
""".replace("MODULE", json.dumps(str(MODULE)))
    result = json.loads(run_js(script))
    # The methods are listed...
    assert result["methods"] == ["sendMsg", "setMsgEmojiLike"]
    # ...and neither called nor read through a getter.
    assert result["called"] == []
    assert result["fired"] == []

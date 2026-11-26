"""The generic OIDB channel: `oidb.call` in the payload, `oidb_call` on the core side.

Several capabilities have no kernel service method at all -- a member's title, a message reaction, a
nudge, approving a join request -- and the only route to them is a command number plus a body. That
makes this seam load-bearing rather than a convenience, so it is pinned here.

It was also broken: the action called a `_handle` that does not exist and used `re` without
importing it, so every call answered `name '_handle' is not defined`. Neither failure was reachable
from a test that only checked the action was registered.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
for entry in (str(ROOT), str(ROOT / "src")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

MODULE = ROOT / "src" / "qsick" / "headless_actions.cjs"
NODE = shutil.which("node")
requires_node = pytest.mark.skipif(NODE is None, reason="node is unavailable")


def run_js(script: str, timeout: int = 25) -> str:
    assert NODE is not None
    completed = subprocess.run([NODE, "-e", script], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=timeout)
    assert completed.returncode == 0, (completed.stdout or "") + (completed.stderr or "")
    return completed.stdout


# ------------------------------------------------------------------- payload side


@requires_node
def test_oidb_call_is_declared_with_the_sso_transport():
    source = MODULE.read_text(encoding="utf-8")
    assert "'oidb.call': ['Msg', 'sendSsoCmdReqByContend']" in source


@requires_node
def test_oidb_call_returns_the_raw_body_and_status():
    """The body is not interpreted: a caller that knows the layout parses it, one that does not learns it."""
    script = """
function varintField(field, value) {
  const bytes = [];
  let v = value;
  while (v >= 0x80) { bytes.push((v & 0x7f) | 0x80); v = Math.floor(v / 128); }
  bytes.push(v);
  return Buffer.from([(field << 3), ...bytes]);
}
function lengthField(field, buffer) {
  const len = [];
  let v = buffer.length;
  while (v >= 0x80) { len.push((v & 0x7f) | 0x80); v = Math.floor(v / 128); }
  len.push(v);
  return Buffer.concat([Buffer.from([(field << 3) | 2, ...len]), buffer]);
}
const assert = require('node:assert/strict');
const { createActionDispatcher } = require(MODULE);
(async () => {
  const seen = [];
  const msg = {
    sendSsoCmdReqByContend: async (name, packet) => {
      seen.push({ name, hex: Buffer.from(packet).toString('hex') });
      // OidbSvcTrpcTcpBase: 1 command, 2 subCommand, 3 errorCode, 4 body, 5 errorMsg.
      // Built with the module's own writer so the varints are right -- hand-written bytes got the
      // tag order wrong once already.
      const W = require(PRIVATE).Writer || null;
      const body = Buffer.from([0x0a, 0x02, 0x68, 0x69]);
      const envelope = Buffer.concat([
        varintField(1, 0xE37), varintField(2, 800), varintField(3, 0),
        lengthField(4, body),
      ]);
      return {result: 0, rspbuffer: envelope};
    },
  };
  const dispatcher = createActionDispatcher({
    getSession: () => ({getMsgService: () => msg}),
    getIdentity: () => ({uin: '1'}),
  });
  const result = await dispatcher.execute('oidb.call', {
    command: 0xE37, sub_command: 800, body_hex: '086869',
  });
  console.log(JSON.stringify({result, seen}));
})().catch(error => {console.error(error); process.exitCode = 1;});
""".replace("MODULE", json.dumps(str(MODULE))) \
        .replace("PRIVATE", json.dumps(str(ROOT / "src" / "qsick" / "headless_privatefile.cjs")))
    output = json.loads(run_js(script))
    result = output["result"]
    assert result["command"] == 0xE37
    assert result["sub_command"] == 800
    assert result["error_code"] == 0
    assert result["body_hex"] == "0a026869"
    assert result["body_size"] == 4
    # Which naming style routes is per-command, so the transport's resolved name is reported
    # rather than reconstructed. Plain is the default; a caller probing an unmapped command can
    # pass `style` to try the other one.
    assert output["seen"][0]["name"] == "OidbSvc.0xE37_800"
    assert result["resolved"] == "OidbSvc.0xE37_800"
    # The request body carried exactly the bytes the caller asked for.
    assert "086869" in output["seen"][0]["hex"]


@requires_node
def test_an_odd_length_body_is_refused_before_sending():
    script = """
const { createActionDispatcher } = require(MODULE);
(async () => {
  let called = false;
  const dispatcher = createActionDispatcher({
    getSession: () => ({getMsgService: () => { called = true; return {}; }}),
    getIdentity: () => ({uin: '1'}),
  });
  try {
    await dispatcher.execute('oidb.call', {command: 1, body_hex: 'abc'});
    console.log(JSON.stringify({threw: false}));
  } catch (error) {
    console.log(JSON.stringify({threw: true, code: error.code, message: error.message}));
  }
})();
""".replace("MODULE", json.dumps(str(MODULE)))
    output = json.loads(run_js(script))
    assert output["threw"] is True
    assert "whole bytes" in output["message"]


@requires_node
def test_a_non_hex_body_is_refused():
    script = """
const { createActionDispatcher } = require(MODULE);
(async () => {
  const dispatcher = createActionDispatcher({
    getSession: () => ({getMsgService: () => ({})}), getIdentity: () => ({uin: '1'}),
  });
  try {
    await dispatcher.execute('oidb.call', {command: 1, body_hex: 'zz'});
    console.log(JSON.stringify({threw: false}));
  } catch (error) {
    console.log(JSON.stringify({threw: true, message: error.message}));
  }
})();
""".replace("MODULE", json.dumps(str(MODULE)))
    output = json.loads(run_js(script))
    assert output["threw"] is True
    assert "hexadecimal" in output["message"]


@requires_node
def test_a_refused_command_is_reported_as_upstream_not_as_a_caller_error():
    """`not routed` and `bad payload` are different answers; collapsing them hides which one happened."""
    script = """
const { createActionDispatcher } = require(MODULE);
(async () => {
  const dispatcher = createActionDispatcher({
    getSession: () => ({getMsgService: () => ({
      sendSsoCmdReqByContend: async () => ({result: 236, errMsg: '访问接口未配置路由'}),
    })}),
    getIdentity: () => ({uin: '1'}),
  });
  try {
    await dispatcher.execute('oidb.call', {command: 0xE37, sub_command: 800, body_hex: ''});
    console.log(JSON.stringify({threw: false}));
  } catch (error) {
    console.log(JSON.stringify({threw: true, code: error.code, message: error.message,
                                details: error.details}));
  }
})();
""".replace("MODULE", json.dumps(str(MODULE)))
    output = json.loads(run_js(script))
    assert output["threw"] is True
    assert output["code"] == "UPSTREAM"
    assert output["details"]["command"] == 0xE37


# ---------------------------------------------------------------------- core side


async def test_the_core_action_reaches_the_payload_op():
    from qsick.onebot.actions.base import ActionContext
    from qsick.onebot.actions.packet.oidb_call import oidbcall

    seen: list[tuple[str, dict]] = []

    class Ctx:
        async def call(self, op, params, label="", **kwargs):
            seen.append((op, dict(params)))
            return SimpleNamespace(ok=True, data={
                "command": 0xE37, "sub_command": 800, "resolved": "OidbSvcTrpcTcp.0xe37_800",
                "error_code": 0, "error_message": "", "body_hex": "0a00", "body_size": 2})

    result = await oidbcall(Ctx(), command=0xE37, sub_command=800, body_hex="086869")
    assert [item[0] for item in seen] == ["oidb.call"]
    forwarded = seen[0][1]
    assert forwarded["command"] == 0xE37
    assert forwarded["sub_command"] == 800
    assert forwarded["body_hex"] == "086869"
    assert result["body_hex"] == "0a00"
    # The fake below reports its own resolved name; the point here is that the
    # core action passes it through untouched.
    assert result["resolved"] == "OidbSvcTrpcTcp.0xe37_800"


async def test_the_core_action_rejects_bad_input_before_calling_out():
    from qsick.onebot.actions.base import ActionError
    from qsick.onebot.actions.packet.oidb_call import oidbcall

    class Ctx:
        async def call(self, *args, **kwargs):  # pragma: no cover - must not run
            raise AssertionError("the payload must not be reached")

    with pytest.raises(ActionError, match="requires command"):
        await oidbcall(Ctx(), command=0)
    with pytest.raises(ActionError, match="hex"):
        await oidbcall(Ctx(), command=1, body_hex="zz")
    with pytest.raises(ActionError, match="whole bytes"):
        await oidbcall(Ctx(), command=1, body_hex="abc")


async def test_an_explicit_timeout_is_forwarded():
    from qsick.onebot.actions.packet.oidb_call import oidbcall

    seen: list[dict] = []

    class Ctx:
        async def call(self, op, params, label="", **kwargs):
            seen.append(dict(params))
            return SimpleNamespace(ok=True, data={})

    await oidbcall(Ctx(), command=1, body_hex="", timeout_ms=2500)
    assert seen[0]["timeout_ms"] == 2500


async def test_a_naming_style_can_be_named():
    """Neither style is universally right, so the caller can pick one for an unmapped command."""
    from qsick.onebot.actions.base import ActionError
    from qsick.onebot.actions.packet.oidb_call import oidbcall

    seen: list[dict] = []

    class Ctx:
        async def call(self, op, params, label="", **kwargs):
            seen.append(dict(params))
            return SimpleNamespace(ok=True, data={})

    await oidbcall(Ctx(), command=1, body_hex="", style="trpc")
    assert seen[0]["style"] == "trpc"
    with pytest.raises(ActionError, match="plain / trpc / lower"):
        await oidbcall(Ctx(), command=1, body_hex="", style="nonsense")

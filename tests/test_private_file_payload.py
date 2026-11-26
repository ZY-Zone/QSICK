"""Private file upload and download: what the payload can do, and what it refuses to invent.

Upload is the same `sendMsg` + FILE element path group upload uses -- QQ has no separate "send a file
to a friend" endpoint, the client uploads the bytes itself for either chat type. So the only
difference is the peer: `chatType: 1` with a resolved uid, versus `chatType: 2` with the group number.

Download is genuinely blocked: report 15 documents `0xE37_800` as the private-file command (group
files use `0x6D6_2`) and lists its full protocol as "not covered". The payload therefore probes and
reports; it must never template a URL from a host and a path, because a wrong URL moves the failure
to the transfer where it is far harder to see.
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

MODULE = ROOT / "src" / "qsick" / "headless_actions.cjs"
PRIVATE_MODULE = ROOT / "src" / "qsick" / "headless_privatefile.cjs"
NODE = shutil.which("node")

requires_node = pytest.mark.skipif(NODE is None, reason="node is unavailable")


def run_js(script: str, timeout: int = 25) -> str:
    assert NODE is not None
    completed = subprocess.run([NODE, "-e", script], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=timeout)
    assert completed.returncode == 0, (completed.stdout or "") + (completed.stderr or "")
    return completed.stdout


# --------------------------------------------------------------- request layout


@requires_node
def test_the_probe_builds_the_documented_envelope():
    """Command 0xE37, sub 800, and the body one level down -- the group path taught that lesson."""
    script = """
const m = require(MODULE);
const env = m.encodeEnvelope(m.encodeBody({peerUin: 1927676487, fileUuid: 'uuid-1', fileHash: 'abc'}));
// 1: command (0xE37), 2: subCommand (800), 4: body, 12: reserved
console.log(JSON.stringify({
  command: m.CMD_NUMBER,
  sub: m.SUB_COMMAND,
  names: m.commandNames(),
  hex: env.toString('hex'),
  fields: m.describe(env).map(f => ({field: f.field, wire: f.wire, value: f.value, length: f.length})),
}));
""".replace("MODULE", json.dumps(str(PRIVATE_MODULE)))
    result = json.loads(run_js(script))
    assert result["command"] == 0xE37
    assert result["sub"] == 800
    # The trpc prefix routes on this build; the plain one does not. Both are probed, in this order.
    assert result["names"][0] == "OidbSvcTrpcTcp.0xe37_800"
    assert result["names"][1] == "OidbSvc.0xe37_800"
    top = {item["field"]: item for item in result["fields"]}
    assert set(top) >= {1, 2, 4, 12}
    assert top[1]["value"] == 0xE37
    assert top[2]["value"] == 800
    assert top[12]["value"] == 1
    assert top[4]["length"] > 0


@requires_node
def test_the_md5_is_only_sent_when_known():
    """C2C requires an md5 (unlike group files), but an empty string is not a hash."""
    script = """
const m = require(MODULE);
const withHash = m.encodeBody({peerUin: 1, fileUuid: 'u', fileHash: 'deadbeef'});
const without = m.encodeBody({peerUin: 1, fileUuid: 'u', fileHash: ''});
console.log(JSON.stringify({
  with_len: withHash.length,
  without_len: without.length,
  with_fields: m.describe(withHash).map(f => f.field),
  without_fields: m.describe(without).map(f => f.field),
}));
""".replace("MODULE", json.dumps(str(PRIVATE_MODULE)))
    result = json.loads(run_js(script))
    assert 5 in result["with_fields"]
    assert 5 not in result["without_fields"]


@requires_node
def test_the_probe_never_returns_a_url():
    """The layout is unmapped, so the honest answer is evidence, not a guess."""
    script = """
const m = require(MODULE);
(async () => {
  const refused = await m.probePrivateFileUrl(() => null, {peerUin: 1, fileUuid: 'u'});
  const routed = await m.probePrivateFileUrl(() => ({
    sendSsoCmdReqByContend: async () => ({result: 0, rspbuffer: Buffer.from([0x08, 0x01])}),
  }), {peerUin: 1, fileUuid: 'u'});
  console.log(JSON.stringify({refused, routed, hasUrlRefused: 'url' in refused, hasUrlRouted: 'url' in routed}));
})();
""".replace("MODULE", json.dumps(str(PRIVATE_MODULE)))
    result = json.loads(run_js(script))
    assert result["refused"]["routed"] is False
    assert result["routed"]["routed"] is True
    assert result["routed"]["raw_hex"]
    assert result["hasUrlRefused"] is False
    assert result["hasUrlRouted"] is False


@requires_node
def test_the_probe_falls_back_to_the_second_command_name():
    """One naming style routes on this build; the probe must try both rather than assume."""
    script = """
const m = require(MODULE);
(async () => {
  const tried = [];
  const result = await m.probePrivateFileUrl(() => ({
    sendSsoCmdReqByContend: async (name) => {
      tried.push(name);
      // The plain style is routed; the trpc one is not.
      if (name.startsWith('OidbSvcTrpcTcp')) return {result: 236, errMsg: '访问接口未配置路由', rspbuffer: Buffer.alloc(0)};
      return {result: 0, rspbuffer: Buffer.from([0x08, 0x2a])};
    },
  }), {peerUin: 1, fileUuid: 'u'});
  console.log(JSON.stringify({tried, routed: result.routed, command: result.command}));
})();
""".replace("MODULE", json.dumps(str(PRIVATE_MODULE)))
    result = json.loads(run_js(script))
    assert len(result["tried"]) == 2
    assert result["routed"] is True
    assert result["command"].startswith("OidbSvc.0x")


# ----------------------------------------------------------------- upload wiring


@requires_node
def test_private_upload_is_declared_with_the_send_service():
    """The op exists and asks for exactly the services a file send needs."""
    script = """
const src = require('fs').readFileSync(MODULE_PATH, 'utf8');
const declared = /'upload_private_file':\\s*\\[([^\\]]+)\\]/.exec(src);
console.log(JSON.stringify({found: Boolean(declared), services: declared ? declared[1] : ''}));
""".replace("MODULE_PATH", json.dumps(str(MODULE)))
    result = json.loads(run_js(script))
    assert result["found"], "upload_private_file is not declared in REQUIREMENTS"
    for needed in ("sendMsg", "addKernelMsgListener", "removeKernelMsgListener"):
        assert needed in result["services"]


@requires_node
def test_private_and_group_upload_share_one_branch():
    """No duplicated upload path: the difference is the peer, and a second copy would drift."""
    source = MODULE.read_text(encoding="utf-8")
    assert "if (op === 'group.file.upload' || op === 'upload_private_file') {" in source
    # The element is built once, from shared locals.
    assert source.count("elementType: 3, // FILE") == 1


@requires_node
def test_a_private_upload_result_does_not_report_a_group_folder():
    """`folder_id` on a C2C send would be a field that means nothing to the caller."""
    source = MODULE.read_text(encoding="utf-8")
    assert "const location = isPrivate" in source
    assert "chat_type: 'private'" in source


@requires_node
def test_upload_without_a_verified_uid_fails_instead_of_guessing():
    """`u_<uin>` is not a valid peer on this build; a wrong uid sends into nothing."""
    source = MODULE.read_text(encoding="utf-8")
    assert "No verified peer UID for this account" in source
    # The uid must come from the real mapping, never be synthesised from the account number.
    assert "await resolveUid(userId)" in source


# ------------------------------------------------------------- element passthrough


@requires_node
def test_a_received_file_url_is_passed_through_when_present():
    """`downloadUrl` is a real element field; carry it when QQ fills it, stay silent when it does not.

    Both spellings are accepted because the field name has been seen in two forms and this build has
    never produced a populated one, so neither can be ruled out.
    """
    codec = (ROOT / "src" / "qsick" / "headless_codec.cjs").read_text(encoding="utf-8")
    assert "download_url" in codec
    assert "body.downloadUrl" in codec
    assert "body.download_url" in codec
    # Absent means absent: no placeholder URL is ever written.
    assert "if (downloadUrl) data.download_url = downloadUrl;" in codec

"""`message.emoji_like`: set or remove one reaction on a message.

Signature measured by calling `Msg.setMsgEmojiLikes` directly:
`(peer, msgSeq, msgId, emojiId, setFlag)` — and confirmed by the reaction appearing in the client.
The one trap: `setFlag` is a boolean, while the number `1` *removes* the reaction, so an early probe
that passed `1` twice looked like it had failed when it had actually toggled.
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
NODE = shutil.which("node")
requires_node = pytest.mark.skipif(NODE is None, reason="node is unavailable")


def run_js(script: str) -> str:
    assert NODE is not None
    completed = subprocess.run([NODE, "-e", script], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=25)
    assert completed.returncode == 0, (completed.stdout or "") + (completed.stderr or "")
    return completed.stdout


PRELUDE = """
const { createActionDispatcher } = require(MODULE);
const calls = [];
const msgService = {
  setMsgEmojiLikes: function (peer, seq, msgId, emojiId, setFlag) {
    calls.push({peer, seq, msgId, emojiId, setFlag});
    return {result: 0, errMsg: ''};
  },
};
const session = {getMsgService: () => msgService};
function dispatcher(refs) {
  return createActionDispatcher({
    getSession: () => session,
    getIdentity: () => ({uin: '1'}),
    messageRefs: refs,
  });
}
"""


def dispatch(body: str, refs: str = "new Map([['1', {peer: {chatType: 2, peerUid: '2169010946'}, msgSeq: '1464'}]])"):
    # The substitution has to cover the prelude too -- it is where the module path lives.
    script = (PRELUDE + f"""
(async () => {{
  const d = dispatcher({refs});
  const out = {body};
  console.log(JSON.stringify({{out, calls}}));
}})().catch(error => {{
  console.log(JSON.stringify({{code: error.code, message: error.message, calls}}));
}});
""").replace("MODULE", json.dumps(str(MODULE)))
    return json.loads(run_js(script))


@requires_node
def test_it_calls_setMsgEmojiLikes_with_the_measured_signature():
    result = dispatch("await d.execute('message.emoji_like', "
                      "{message_id: '1', emoji_id: '128512', set: true})")
    call = result["calls"][0]
    # Exactly the five positional arguments the kernel asserts.
    assert call["peer"] == {"chatType": 2, "peerUid": "2169010946", "guildId": ""}
    assert call["seq"] == "1464"
    assert call["msgId"] == "1"
    assert call["emojiId"] == "128512"
    assert call["setFlag"] is True


@requires_node
def test_set_false_removes_it():
    result = dispatch("await d.execute('message.emoji_like', "
                      "{message_id: '1', emoji_id: '128512', set: false})")
    assert result["calls"][0]["setFlag"] is False


@requires_node
def test_a_numeric_set_flag_is_not_turned_into_one():
    """`1` is how the client spells "remove"; coercing a truthy number to `true` would invert the call."""
    result = dispatch("await d.execute('message.emoji_like', {message_id: '1', set: 1})")
    assert result["calls"][0]["setFlag"] is False


@requires_node
def test_a_string_true_is_accepted():
    result = dispatch("await d.execute('message.emoji_like', {message_id: '1', set: 'true'})")
    assert result["calls"][0]["setFlag"] is True


@requires_node
def test_set_defaults_to_true():
    result = dispatch("await d.execute('message.emoji_like', {message_id: '1'})")
    assert result["calls"][0]["setFlag"] is True
    assert result["calls"][0]["emojiId"] == "128077"


@requires_node
def test_an_unknown_message_is_refused_before_calling_out():
    result = dispatch("await d.execute('message.emoji_like', {message_id: '999'})",
                      refs="new Map()")
    assert result["code"] == "NOT_FOUND"
    assert result["calls"] == []


@requires_node
def test_a_private_chat_uses_its_own_peer_uid():
    result = dispatch(
        "await d.execute('message.emoji_like', {message_id: '1', emoji_id: '5'})",
        refs="new Map([['1', {peer: {chatType: 1, peerUid: 'u_abc'}, msgSeq: '7'}]])")
    assert result["calls"][0]["peer"] == {"chatType": 1, "peerUid": "u_abc", "guildId": ""}
    assert result["out"]["chat_type"] == "private"


@requires_node
def test_a_server_refusal_is_reported_with_its_code():
    script = (PRELUDE.replace("{result: 0, errMsg: ''}", "{result: 65003, errMsg: '未设置过该表情'}") + f"""
(async () => {{
  const d = dispatcher(new Map([['1', {{peer: {{chatType: 2, peerUid: '2169010946'}}, msgSeq: '1464'}}]]));
  try {{
    await d.execute('message.emoji_like', {{message_id: '1', set: false}});
    console.log(JSON.stringify({{threw: false}}));
  }} catch (error) {{
    console.log(JSON.stringify({{threw: true, code: error.code, message: error.message,
                                details: error.details}}));
  }}
}})();
""").replace("MODULE", json.dumps(str(MODULE)))
    result = json.loads(run_js(script))
    assert result["threw"] is True
    assert result["code"] == "SERVICE_ERROR"
    assert result["details"]["serviceCode"] == 65003


def test_the_op_is_declared_and_handled():
    source = MODULE.read_text(encoding="utf-8")
    start = source.index("const REQUIREMENTS = Object.freeze({")
    end = source.index("});", start)
    assert "'message.emoji_like':" in source[start:end]
    assert "op === 'message.emoji_like'" in source


@requires_node
def test_an_unindexed_message_can_be_reacted_to_with_explicit_parameters():
    """`messageRefs` only holds messages read from history, and a reaction almost always targets a
    message that just arrived. Without this fallback the op could not do the one thing it is for."""
    result = dispatch(
        "await d.execute('message.emoji_like', "
        "{message_id: '1', emoji_id: '128512', group_id: '2169010946', message_seq: '1464'})",
        refs="new Map()")
    call_seen = result["calls"][0]
    assert call_seen["peer"] == {"chatType": 2, "peerUid": "2169010946", "guildId": ""}
    assert call_seen["seq"] == "1464"


@requires_node
def test_explicit_parameters_do_not_override_an_indexed_message():
    result = dispatch(
        "await d.execute('message.emoji_like', {message_id: '1', group_id: '999999', message_seq: '7'})")
    # The indexed peer and seq win: they came from the client itself.
    assert result["calls"][0]["peer"]["peerUid"] == "2169010946"
    assert result["calls"][0]["seq"] == "1464"


@requires_node
def test_a_missing_sequence_is_reported_not_guessed():
    result = dispatch(
        "await d.execute('message.emoji_like', {message_id: '1', group_id: '2169010946'})",
        refs="new Map()")
    assert result["code"] == "INVALID_PARAMS"
    assert "message_seq" in result["message"]
    assert result["calls"] == []

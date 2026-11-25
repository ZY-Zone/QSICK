"""Synthetic native shapes through the real injected callbacks and JS extractor; no QQ IO."""
from __future__ import annotations

import ast
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


def injected_js():
    source = (ROOT / "native/qsick-pipe/src/ipc_probe.cpp").read_text(encoding="utf-8")
    return "".join(ast.literal_eval(line.strip().rstrip(";")) for line in source.splitlines()
                   if line.strip().startswith('"') and line.strip().rstrip(";").endswith('"'))


def run_js(body):
    js = injected_js()
    emit = js[js.index("function __emit(raw,kind)"):js.index("var __noticeBridge=null;")]
    recall_start = js.index("onMsgRecall:function(") + len("onMsgRecall:")
    recall = js[recall_start:js.index("          };          var __id=__svc.addKernelMsgListener", recall_start)]
    setup = f"""
const assert = require('node:assert/strict');
const __req = require;
const __c = require({json.dumps(str(ROOT / 'src/qsick/headless_codec.cjs'))});
const __appDir = () => {json.dumps(str(ROOT / 'src/qsick'))};
const __mkCodec = () => __c;
const __lb = {{uin:'999'}};
const __uidByUin = new Map([['100','u_member'],['200','u_admin']]);
const __messageRefs = new Map();
const __recentRecall = {{}};
let __noticeBridge = null;
const output = [];
const __append = (name, text) => {{ if(name !== 'events.jsonl') throw Error(text); output.push(JSON.parse(text)); }};
{emit}
const recall = {recall};
const base = {{chatType:2, peerUid:'1234', peerUin:'1234', senderUid:'u_admin', senderUin:'200', msgId:'7001', msgSeq:'10', msgTime:'1'}};
const text = {{elementType:1,textElement:{{content:'hello'}}}};
const tip = body => ({{elementType:8,grayTipElement:{{subElementType:4,groupElement:body}}}});
{body}
"""
    result = subprocess.run([NODE, "-e", setup], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=60)
    assert result.returncode == 0, result.stderr


def test_pipe_bundle_ships_bridge_and_extractor():
    tree = ast.parse((ROOT / "src/qsick/headless.py").read_text(encoding="utf-8"))
    assignment = next(node for node in tree.body if isinstance(node, ast.Assign)
                      and any(isinstance(target, ast.Name) and target.id == "PIPE_JS_MODULES"
                              for target in node.targets))
    modules = ast.literal_eval(assignment.value)
    assert {"headless_notices.cjs", "pipe_notice_bridge.cjs"} <= set(modules)


def test_group_tip_uses_real_extractor_and_uid_handoff():
    run_js("""
__emit({...base,elements:[tip({type:8,shutUp:{duration:60,member:{uid:'u_member'},admin:{uid:'u_admin'}}})]},'message');
assert.equal(output.length,1);
assert.equal(output[0].type,'notice');
assert.equal(output[0].uin,'999');
assert.equal(output[0].cmd,'notice.group_ban');
assert.equal(output[0].payload.user_id,'100');
assert.equal(output[0].payload.operator_id,'200');
assert.equal(output[0].payload.uin_by_uid.u_member,'100');
assert.equal(output[0].payload.group_id,'1234');
""")


def test_group_file_keeps_message_and_upload():
    run_js("""
__emit({...base,elements:[{elementType:3,fileElement:{fileName:'a.txt',fileSize:'12',fileUuid:'opaque',fileBizId:102}}]},'message');
assert.deepEqual(output.map(e=>e.type),['notice','message']);
assert.equal(output[0].cmd,'notice.group_upload');
assert.equal(output[0].payload.file.id,'opaque');
assert.equal(output[1].protocol.message_id,'7001');
assert.ok(__messageRefs.has('7001'));
""")


def test_unknown_gray_tip_is_not_message_but_sent_receipt_survives():
    run_js("""
const raw = {...base,elements:[tip({type:999})]};
__emit(raw,'message');
assert.equal(output.length,0);
__emit(raw,'sent');
assert.equal(output.length,1);
assert.equal(output[0].type,'sent');
assert.equal(output[0].message.msgId,'7001');
""")


def test_mixed_tip_keeps_chat_and_private_uid_resolves():
    run_js("""
__emit({...base,elements:[tip({type:5,groupName:'renamed',memberUid:'u_admin'}),text]},'message');
assert.deepEqual(output.map(e=>e.type),['notice','message']);
__emit({...base,chatType:1,peerUid:'u_member',peerUin:undefined,elements:[text]},'sent');
assert.equal(output[2].protocol.chat.id,'100');
assert.equal(output[2].type,'sent');
""")


def test_recall_matches_conversation_and_does_not_guess_operator():
    run_js("""
__emit({...base,peerUid:'4321',peerUin:'4321',msgId:'7000',elements:[text]},'message');
__emit({...base,elements:[text]},'message');
output.length=0;
recall(2,'1234','10');
recall(2,'4321','10');
assert.equal(output.length,2);
assert.equal(String(output[0].payload.message_id),'7001');
assert.equal(String(output[1].payload.message_id),'7000');
assert.equal(output[0].payload.msg_seq,'10');
assert.equal(output[0].payload.operator_id,0);
assert.equal(output[0].payload.user_id,200);
recall(1,'u_member','20');
assert.equal(output[2].payload.peer_id,'100');
assert.equal(output[2].payload.peer_uid,'u_member');
recall(1,'u_unknown','20');
assert.equal(output[3].payload.peer_id,0);
""")

"""Regression: the composer must not lose focus on every poll.

``updateComposer()`` runs on a 3 s poll (via loadHistory) and every 12 s
(via loadAccounts → chooseAccount). It used to assign ``input.disabled``
unconditionally. A browser blurs a focused control the moment it becomes
disabled, so typing stopped halfway and the caret vanished until the user
clicked the box again.

The real behaviour lives in chat.js, so this test loads the function body into
a Node double rather than re-implementing it: the assertion is about the shipped
code, not a copy of it.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CHAT_JS = ROOT / "src" / "qsick" / "webui" / "static" / "chat.js"

HARNESS = r"""
'use strict';
let writes = 0;
const input = {
  _disabled: true,
  get disabled() { return this._disabled; },
  set disabled(value) {
    writes += 1;
    if (value && dual.activeElement === input) dual.activeElement = null;   // browsers blur on disable
    this._disabled = value;
  },
  value: 'typing halfway', placeholder: '', focus() { dual.activeElement = input; },
};
const send = { disabled: false, textContent: '' };
const emoji = { disabled: false };
const length = { textContent: '' };
const dual = {
  activeElement: null,
  getElementById(id) {
    if (id === 'chat-send') return send;
    if (id === 'emoji-toggle') return emoji;
    if (id === 'chat-length') return length;
    return null;
  },
};

// ---- shipped logic, extracted from chat.js -------------------------------
const state = { account: { logged_in: true }, current: { chat_type: 'group' },
                privateSend: true, loading: false, sending: false, attachments: [] };
const document = dual;
const $ = id => dual.getElementById(id);
__COMPOSER__
// -------------------------------------------------------------------------

input.focus();
updateComposer();
const focused = dual.activeElement === input;
const before = writes;
for (let i = 0; i < 20; i++) {
  state.loading = true;  updateComposer();   // a poll in flight
  state.loading = false; updateComposer();   // poll finished
}
const stillFocused = dual.activeElement === input;
const extra = writes - before;
const keptValue = input.value === 'typing halfway';

state.current = null;                       // a real state change (left the conversation)
updateComposer();
const disabledOnLogout = input.disabled === true;

console.log(JSON.stringify({ focused, stillFocused, extra, keptValue, disabledOnLogout }));
"""


@pytest.fixture(scope="module")
def composer_body() -> str:
    source = CHAT_JS.read_text(encoding="utf-8")
    start = source.index("function updateComposer()")
    end = source.index("function nearBottom()")
    block = source[start:end]
    # composerLocked is declared just above the function; keep that declaration.
    decl_start = source.rindex("\n", 0, source.rindex("let composerLocked", 0, start)) + 1
    return source[decl_start:end]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_repeated_updates_do_not_disable_the_focused_composer(composer_body: str, tmp_path: Path) -> None:
    script = tmp_path / "composer_harness.cjs"
    script.write_text(HARNESS.replace("__COMPOSER__", composer_body), encoding="utf-8")
    result = subprocess.run([shutil.which("node"), str(script)], capture_output=True, text=True,
                            encoding="utf-8", timeout=60)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout.strip().splitlines()[-1])

    assert data["focused"] is True, "the composer should accept focus when the chat is usable"
    assert data["stillFocused"] is True, "20 polls stole the caret"
    assert data["extra"] == 0, f"disabled was written {data['extra']} extra times during idle polls"
    assert data["keptValue"] is True, "in-progress text was lost"
    assert data["disabledOnLogout"] is True, "a real state change must still disable the composer"

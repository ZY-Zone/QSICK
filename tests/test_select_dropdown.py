"""Regression: every <select> must use the custom dropdown, including late ones.

The WebUI replaces native ``<select>`` popups with its own ``.dd-menu`` panel.
The first version did that by scanning ``querySelectorAll("select")`` once at
``DOMContentLoaded``, which silently left two kinds of dropdown native:

* selects inserted after load (plugin config rows, anything rendered later);
* selects that were ``disabled`` at load and enabled afterwards -- the
  "快捷发送" account box is exactly this: ``loadQuickAccounts()`` disables it
  when the list is empty, then re-enables it when accounts arrive.

Both showed the system-drawn popup, which is what the report was about. The
fix switches to event delegation, so nothing has to remember to re-register.

The assertions run against the shipped ``app.js`` (extracted, not reimplemented)
so they fail if the delegation is ever undone.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "src" / "qsick" / "webui" / "static" / "app.js"

HARNESS = r"""
'use strict';
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf-8');
const start = src.indexOf('// ---------------------------------------------------------- custom select menus');
const end = src.indexOf('document.addEventListener("DOMContentLoaded"');
if (start < 0 || end < 0) { console.log(JSON.stringify({error: 'dropdown block not found'})); process.exit(0); }

class El {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.children = []; this.dataset = {}; this.style = {};
    this.hidden = false; this.value = ''; this.disabled = false;
    this.multiple = false; this.options = []; this.listeners = {};
    this.textContent = ''; this.className = ''; this.offsetHeight = 0;
  }
  appendChild(c) { this.children.push(c); if (c.tagName === 'OPTION') this.options.push(c); return c; }
  addEventListener(n, f) { (this.listeners[n] = this.listeners[n] || []).push(f); }
  dispatchEvent(e) { (this.listeners[e.type] || []).forEach(f => f.call(this, e)); }
  setAttribute(k, v) { this[k] = v; }
  getAttribute(k) { return this[k]; }
  getBoundingClientRect() { return {left: 0, top: 0, bottom: 20, width: 120, height: 20}; }
  contains(n) { return n === this; }
  closest(sel) { return sel === 'select' ? this : null; }
  set innerHTML(v) { if (v === '') { this.children = []; this.options = []; } }
  get innerHTML() { return ''; }
}

const docListeners = {};
global.document = {
  body: new El('body'),
  createElement: t => new El(t),
  querySelectorAll: () => [],
  addEventListener: (n, f) => { (docListeners[n] = docListeners[n] || []).push(f); },
};
global.window = {innerHeight: 800, addEventListener: () => {}};
global.Event = class { constructor(t) { this.type = t; } };

// 用 indirect eval 在全局作用域执行，函数声明才会成为真正的全局名；
// 直接 eval 会把它们关在块作用域里，后面调用 initSelectMenus 就找不到。
(0, eval)(src.slice(start, end));
initSelectMenus();

function makeSelect(values, disabled) {
  const s = new El('select');
  if (disabled) s.disabled = true;
  values.forEach(v => s.appendChild(Object.assign(new El('option'), {value: v, textContent: v})));
  return s;
}
function click(select) {
  (docListeners['mousedown'] || []).forEach(f => f({target: select, preventDefault: () => {}}));
}

const out = {};

// 1. a select that exists before init
const a = makeSelect(['a']);
click(a);
out.static_opens = selectMenu.hidden === false && selectMenu.children.length === 1;

// 2. inserted after init (was native before the fix)
closeSelectMenu();
const b = makeSelect(['b', 'c']);
click(b);
out.dynamic_opens = selectMenu.hidden === false && selectMenu.children.length === 2;

// 3. disabled at init, enabled later (the quick-send account box)
closeSelectMenu();
const c = makeSelect(['d'], true);
click(c);
out.disabled_stays_closed = selectMenu.hidden === true;
c.disabled = false;
click(c);
out.reenabled_opens = selectMenu.hidden === false && selectMenu.children.length === 1;

// 4. multiple selects keep their native behaviour
closeSelectMenu();
const d = makeSelect(['e']);
d.multiple = true;
click(d);
out.multiple_ignored = selectMenu.hidden === true;

// 5. choosing an item writes back to the real select and fires 'change'
closeSelectMenu();
const e = makeSelect(['x', 'y']);
let changed = 0;
e.addEventListener('change', () => { changed += 1; });
click(e);
selectMenu.children[1].dispatchEvent({type: 'click'});
out.selection_written = e.value === 'y';
out.change_fired = changed === 1;
out.menu_closed_after_pick = selectMenu.hidden === true;

// 6. a disabled option must not be pickable
closeSelectMenu();
const f = makeSelect(['only']);
f.options[0].disabled = true;
click(f);
const before = f.value;
selectMenu.children[0].dispatchEvent({type: 'click'});
out.disabled_option_ignored = f.value === before && selectMenu.hidden === false;

// 7. <dialog> 里的下拉必须退出这套菜单。
//    showModal() 把对话框放进浏览器的 top layer，层级高于任何 z-index；而这份
//    菜单挂在 body 上，硬接管只会得到一个被对话框盖住的菜单（用户报的"下拉被
//    小窗口覆盖"就是这个）。所以带 data-native-select 的交给系统弹出层。
closeSelectMenu();
const g = makeSelect(['headless', 'pipe']);
g.dataset.nativeSelect = '';
click(g);
out.dialog_select_stays_native = selectMenu.hidden === true;

// 同一个页面上，其它 select 不受影响
closeSelectMenu();
const h = makeSelect(['other']);
click(h);
out.other_select_still_custom = selectMenu.hidden === false;

console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_every_select_uses_the_custom_menu(tmp_path: Path) -> None:
    harness = tmp_path / "dropdown_harness.cjs"
    harness.write_text(HARNESS, encoding="utf-8")
    result = subprocess.run(
        ["node", str(harness), str(APP_JS)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )
    assert result.returncode == 0, result.stderr[:800]
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert "error" not in payload, payload

    assert payload["static_opens"], "static select did not open the custom menu"
    assert payload["dynamic_opens"], (
        "a select inserted after load did not open the custom menu — "
        "delegation was replaced by a one-shot querySelectorAll scan")
    assert payload["disabled_stays_closed"], "a disabled select opened a menu"
    assert payload["reenabled_opens"], (
        "a select that was disabled at load and enabled later never got the "
        "custom menu — the driver then showed the system popup")
    assert payload["multiple_ignored"], "multiple select should keep native behaviour"
    assert payload["dialog_select_stays_native"], (
        "a select marked data-native-select opened the custom menu — inside a "
        "modal <dialog> that menu lives under the top layer and is hidden by the "
        "dialog itself, which is exactly the 'dropdown covered by the window' bug")
    assert payload["other_select_still_custom"], (
        "opting one select out broke the others")
    assert payload["selection_written"], "picking an option did not update the select"
    assert payload["change_fired"], "picking an option did not fire change"
    assert payload["menu_closed_after_pick"], "menu stayed open after a pick"
    assert payload["disabled_option_ignored"], "a disabled option was selectable"

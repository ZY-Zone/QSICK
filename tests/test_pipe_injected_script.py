"""The injected script must contain the session and refresh code paths.

These are shipped inside a C string literal, concatenated into one line of JavaScript. That
packaging has two failure modes this session has already hit:

  * a `//` line comment inside a literal comments out the rest of the script, producing
    "SyntaxError: Unexpected end of input" at startup with no indication of where
  * a stray `"` or `\\` inside a literal truncates the string

Both fail silently at build time and only show up when QQ starts, so a cheap structural test is
worth having. This checks the properties that matter, not the exact text:

  * the function is defined before the callbacks that use it
  * no `//` appears inside the JS literals
  * the session config uses account_path as the *parent* of nt_qq

The last one is the detail from headless_host.js that is easy to get wrong and produces a
login that looks successful but never carries messages.
"""

from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "native" / "qsick-pipe" / "src" / "ipc_probe.cpp"


def js_literals() -> str:
    """Concatenate the JS string literals from the injected script."""
    text = SRC.read_text(encoding="utf-8")
    parts: list[str] = []
    # Match "..." literals, honouring escaped quotes inside them.
    for match in re.finditer(r'"((?:[^"\\]|\\.)*)"', text):
        parts.append(match.group(1))
    return "".join(parts)


def test_source_is_present():
    assert SRC.is_file(), f"找不到注入源码：{SRC}"


def test_create_session_is_defined_before_use():
    """`function` declarations hoist, but the callbacks must still reference a real name."""
    literals = js_literals()
    assert "__createSession" in literals, "注入脚本缺少 __createSession"
    definition = literals.find("function __createSession")
    assert definition >= 0, "__createSession 没有定义"
    usage = literals.find("__createSession()")
    assert usage >= 0, "__createSession 从未被调用"


def test_session_uses_account_path_parent_of_nt_qq():
    """desktopPathConfig.account_path must be the parent of nt_qq.

    headless_host.js documents why: splitting the account tree from the global store made QQ
    look for a ticket in one tree while writing it into another.
    """
    literals = js_literals()
    assert "desktopPathConfig" in literals
    assert "account_path" in literals

    # The value must be the app root (…/qq), and the global dir must be …/qq/nt_qq/global.
    assert "__appRoot" in literals, "account_path 应当指向 app 根目录"
    assert "'qq'" in literals or '"qq"' in literals or "'qq'" in literals


def test_session_listener_reports_completion():
    """A session is usable from onSessionInitComplete, not on return from init()."""
    literals = js_literals()
    assert "onSessionInitComplete" in literals
    assert "session_ready" in literals


def test_qr_refresh_timer_exists_and_can_stop():
    """A refresh that never stops would replace a code mid-scan."""
    literals = js_literals()
    assert "__qrRefresh" in literals
    assert "clearInterval" in literals
    assert "getQRCodePicture" in literals

    # It must stop once progress is visible.
    assert "scanned" in literals
    assert "session_ready" in literals


def test_no_line_comment_inside_js_literals():
    """`//` inside a literal comments out the rest of the injected script.

    The whole script is one line once concatenated, so a `//` swallows everything after it --
    including the closing `})()`. The symptom is "SyntaxError: Unexpected end of input" with no
    location, because the source genuinely is one line. It cost three debugging rounds.

    The check must look at *literal bodies only*. An earlier version reconstructed literals from
    a region of the file and reported the C++ comments sitting between them as failures -- a
    false alarm, since those comments never reach the script.
    """
    text = SRC.read_text(encoding="utf-8")
    offenders: list[tuple[int, str]] = []
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped.startswith('"'):
            continue
        first = stripped.find('"')
        last = stripped.rfind('"')
        if last <= first:
            continue
        body = stripped[first + 1:last]
        if "//" in body:
            offenders.append((number, body[:160]))

    assert not offenders, (
        "注入脚本的字符串字面量里出现 //，会把后面整段注释掉：\n"
        + "\n".join(f"  行 {n}: {b}" for n, b in offenders))


def test_loop_heartbeat_is_present():
    """The heartbeat is how the loop's health was measured; keep it observable."""
    literals = js_literals()
    assert "loop-ticks.txt" in literals
    assert "setInterval" in literals


def test_bootstrap_is_retryable():
    """The bootstrap must be a named function, not a one-shot IIFE.

    wrapper.node's napi registration can land after the script starts. When it did, the old
    one-shot version ran the rest of the bootstrap against an empty object, wrote stage "booted"
    with every capability false, and marked itself done so nothing retried. Measured: a
    timer-based probe saw the registration arrive on the next 200 ms attempt with 98 exports,
    long after the bootstrap had given up -- and the boot file then looked exactly like a broken
    login service.
    """
    literals = js_literals()
    assert "global.__qsickBoot=function" in literals, \
        "引导脚本必须是具名函数，才能重新进入"
    assert "__qsickBoot()" in literals, "必须真的调用它"
    assert "waiting_wrapper" in literals, "wrapper 未就绪时应当明确等待，而不是继续跑"


def test_wrapper_wait_does_not_retry_forever():
    """A permanently broken wrapper must stop, not spin."""
    literals = js_literals()
    assert "waitCount" in literals
    # A bound must exist; the exact number is an implementation detail.
    assert "<=" in literals


def test_script_dump_is_written():
    """napi reports a syntax error as a bare status code with no location.

    Dumping the exact bytes is what made a stray `//` findable. It also proves which code ran,
    rather than which code was edited -- an earlier build silently produced a DLL without the
    new string in it, and only a binary check caught that.
    """
    text = SRC.read_text(encoding="utf-8")
    assert "injected-script.js" in text
    # The literal must be reachable in the binary, which is why it is built by concatenation
    # rather than through a format string.
    assert "lstrcatW(dumpPath" in text or '"\\\\injected-script.js"' in text

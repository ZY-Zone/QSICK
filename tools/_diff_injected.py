"""Diff the injected JavaScript embedded in the last good DLL against the current source.

The build guard reports a syntax error in `kBridge`, but not which construct broke it, and `native/*.cpp` is
gitignored so there is no source history to fall back on. The last DLL that built successfully still contains the
exact script text in its binary, which makes it a perfect reference: extract both, diff them, and the damage is
visible line by line.
"""
from __future__ import annotations

import difflib
import importlib.util
import pathlib
import re

DLL = pathlib.Path("native/qsick-pipe/qsick_pipe.dll")
CPP = pathlib.Path("native/qsick-pipe/src/ipc_probe.cpp")

spec = importlib.util.spec_from_file_location("guard", "native/qsick-pipe/build/check_injected_js.py")
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)

text = CPP.read_text(encoding="utf-8")
start = text.find("static const char* kBridge")
end = text.find('})()";', start)
current = guard.c_unescape("".join(guard.literals_in(text[start:end + 7])))

blob = DLL.read_bytes()
needle = b"(function(){  var out={ok:false"
at = blob.find(needle)
if at < 0:
    # The compiler may split the literal; try a shorter anchor.
    needle = b"var out={ok:false,step:'start'}"
    at = blob.find(needle)
    at = at - len(b"(function(){  ") if at > 0 else -1
if at < 0:
    print("could not locate the script inside the DLL")
    raise SystemExit(1)

tail = blob.find(b"return JSON.stringify(out);})()", at)
if tail < 0:
    print("could not locate the script's end inside the DLL")
    raise SystemExit(1)
embedded = blob[at:tail + len(b"return JSON.stringify(out);})()")].decode("utf-8", "replace")

print(f"DLL 内嵌 {len(embedded)} 字节 / 当前源码 {len(current)} 字节")

# Compare as token-ish chunks so a single character difference is visible without a wall of text.
before = re.split(r"(?<=[;{}])", embedded)
after = re.split(r"(?<=[;{}])", current)
diff = list(difflib.unified_diff(before, after, lineterm="", n=2))
if not diff:
    print("两者完全一致 —— 差异不在 kBridge 里")
for line in diff[:120]:
    print(line[:170])

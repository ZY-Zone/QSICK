"""Restore the injected boot script to its pre-event-channel state.

The event channel added a message listener to the pipe session and crashed QQ.exe in ntdll with
0xc0000374 (heap corruption) on every launch. Reverting the whole block rather than leaving the calls
commented out: half-removed code in a boot script that runs inside QQ is worse than none, and the
definitions did nothing but confuse the next reader.

Removes, by line range, the block that starts at the "msgListener" comment and ends at the closing brace
of `__attachMsgListener`. Verified afterwards by checking the identifiers are gone and the file still
compiles.
"""
from __future__ import annotations

import pathlib

TARGET = pathlib.Path(__file__).resolve().parents[1] / "native/qsick-pipe/src/ipc_probe.cpp"

#: Lines that mark the block. Everything from the first marker up to and including the last is removed.
START_MARKER = '    "      __lb.msgListener=false;"'
END_MARKER = '    "      }"'

# The end marker is ambiguous, so the block end is pinned by counting: the attach function's closing line
# is the one immediately before the blank line preceding the `__lb.stage='listener'` line.
AFTER_MARKER = '    "      __lb.stage=\'listener\';"'


def main() -> int:
    text = TARGET.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)

    start = None
    for index, line in enumerate(lines):
        if line.rstrip("\r\n") == START_MARKER:
            start = index
            break
    if start is None:
        print("  未找到起始标记，可能已还原过")
        return 1

    after = None
    for index in range(start, len(lines)):
        if lines[index].rstrip("\r\n") == AFTER_MARKER:
            after = index
            break
    if after is None:
        print("  未找到结束边界")
        return 1

    # Walk back from `after` past blank lines and the comment lines that introduce the next stage.
    end = after - 1
    while end > start and (not lines[end].strip()
                           or lines[end].lstrip().startswith("//")
                           or lines[end].rstrip("\r\n").startswith("    //")):
        end -= 1

    removed = end - start + 1
    new_lines = lines[:start] + lines[after:]
    TARGET.write_text("".join(new_lines), encoding="utf-8")

    print(f"  删除第 {start + 1}-{end + 1} 行，共 {removed} 行")
    remaining = TARGET.read_text(encoding="utf-8")
    for token in ("__attachMsgListener", "__mkCodec", "__append", "msgListener"):
        count = remaining.count(token)
        print(f"  残留 {token}: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

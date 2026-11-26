"""Extract the printable strings around a method name in `wrapper.node`.

The four capabilities live in the client's native module, so the argument names are not in any JS --
but the conversion code that reads `arg.groupCode` usually carries the field name as a literal
nearby. This prints those neighbours, which is cheaper and far more reliable than guessing one field
at a time over the wire.

    python tools/strings_around.py setIdentityTitleInfo
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

WRAPPER = Path(r"D:\projects\QSICK\headlessClient\Pipe\versions\9.9.33-52230\resources\app\wrapper.node")
MIN_LEN = 4
WINDOW = 2400


def printable(blob: bytes) -> list[str]:
    """ASCII runs of at least MIN_LEN characters, in order."""
    return [match.group().decode("ascii", "replace")
            for match in re.finditer(rb"[\x20-\x7e]{%d,}" % MIN_LEN, blob)]


def main() -> int:
    needle = sys.argv[1] if len(sys.argv) > 1 else "setIdentityTitleInfo"
    data = WRAPPER.read_bytes()
    pattern = needle.encode()
    positions = [match.start() for match in re.finditer(re.escape(pattern), data)]
    print(f"{needle}: {len(positions)} 处，文件 {len(data) / 1024 / 1024:.1f} MB")
    for index, position in enumerate(positions[:6], 1):
        start = max(0, position - WINDOW)
        end = min(len(data), position + WINDOW)
        neighbours = printable(data[start:end])
        print(f"\n=== 第 {index} 处 @ 0x{position:x}（±{WINDOW} 字节）")
        for text in neighbours:
            # Only strings that read like field names, messages or signatures.
            if len(text) > 120:
                continue
            print(f"   {text}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

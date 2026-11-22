"""Show which symbols dominate a DLL's image, by section.

`qsick_pipe.dll` crashes QQ at startup; `skeleton.dll` (built with the same flags, containing only the
handshake) does not. Rather than guessing which source construct is responsible, this reads the PE section
table and the COFF symbol table so the biggest contributors are named.

Windows symbol tables are often stripped from release builds, so this reports sections first -- that is
always available -- and names symbols only when they are present.

Run: python tools/image_report.py [dll]
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NATIVE = ROOT / "native" / "qsick-pipe"

SECTION_NAMES = {
    ".text": "code",
    ".rdata": "read-only data (string literals live here)",
    ".data": "initialised data",
    ".bss": "zero-initialised data",
    ".tls": "thread-local storage",
    ".pdata": "exception unwind info",
    ".reloc": "base relocations",
    ".rsrc": "resources",
}


def sections(path: Path) -> list[tuple[str, int, int]]:
    data = path.read_bytes()
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    count = struct.unpack_from("<H", data, pe + 6)[0]
    opt_size = struct.unpack_from("<H", data, pe + 20)[0]
    opt = pe + 24
    table = opt + opt_size
    out = []
    for index in range(count):
        base = table + index * 40
        raw = data[base:base + 8].rstrip(b"\0").decode("ascii", "replace")
        vsize, vaddr, rawsize, rawptr = struct.unpack_from("<IIII", data, base + 8)
        out.append((raw, max(vsize, rawsize), rawptr))
    return out


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else NATIVE / "qsick_pipe.dll"
    rows = sections(path)
    total = sum(size for _, size, _ in rows)
    print(f"  {path.name}: {path.stat().st_size // 1024} KB on disk, {total // 1024} KB mapped")
    print()
    print(f"  {'section':10s} {'size':>10s}  what it holds")
    for name, size, _ in sorted(rows, key=lambda row: -row[1]):
        if size < 4096:
            continue
        print(f"  {name:10s} {size // 1024:8d} KB  {SECTION_NAMES.get(name, '')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

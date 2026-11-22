"""Compare two DLLs' import tables, to explain why one kills QQ and the other does not.

Both are built the same way (`/LD /MT /O2`) and injected the same way. `blank_probe.dll` survives; the real
payload takes QQ down to 2 processes and kills it in ~17 s. The remaining differences are the imports the
loader must resolve at attach time -- and a DLL that drags in user32/advapi32/shell32 while the process is
still starting up can deadlock or re-enter the loader where a DLL that imports almost nothing cannot.

Reads the PE import directory directly, so it needs no dumpbin.

Run: python tools/compare_imports.py [a.dll] [b.dll]
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NATIVE = ROOT / "native" / "qsick-pipe"


def imports(path: Path) -> list[str]:
    data = path.read_bytes()
    if data[:2] != b"MZ":
        raise ValueError(f"{path.name}: not a PE file")
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe:pe + 4] != b"PE\0\0":
        raise ValueError(f"{path.name}: bad PE signature")
    sections = struct.unpack_from("<H", data, pe + 6)[0]
    opt_size = struct.unpack_from("<H", data, pe + 20)[0]
    opt = pe + 24
    magic = struct.unpack_from("<H", data, opt)[0]
    is64 = magic == 0x20B
    # Import directory is entry 1 in the data directories, at a fixed offset in both PE32 and PE32+.
    dd = opt + (112 if is64 else 96)
    import_rva = struct.unpack_from("<I", data, dd + 8)[0]
    if not import_rva:
        return []

    # Map RVA -> file offset using the section table.
    sec = opt + opt_size
    spans = []
    for i in range(sections):
        base = sec + i * 40
        vsize, vaddr, rawsize, rawptr = struct.unpack_from("<IIII", data, base + 8)
        spans.append((vaddr, max(vsize, rawsize), rawptr))

    def offset(rva: int) -> int | None:
        for vaddr, size, rawptr in spans:
            if vaddr <= rva < vaddr + size:
                return rawptr + (rva - vaddr)
        return None

    names: list[str] = []
    cursor = offset(import_rva)
    if cursor is None:
        return []
    while True:
        entry = data[cursor:cursor + 20]
        if len(entry) < 20 or entry == b"\0" * 20:
            break
        name_rva = struct.unpack_from("<I", entry, 12)[0]
        if not name_rva:
            break
        at = offset(name_rva)
        if at is None:
            break
        end = data.index(b"\0", at)
        names.append(data[at:end].decode("ascii", "replace").lower())
        cursor += 20
    return sorted(set(names))


def main() -> int:
    left = Path(sys.argv[1]) if len(sys.argv) > 1 else NATIVE / "qsick_pipe.dll"
    right = Path(sys.argv[2]) if len(sys.argv) > 2 else NATIVE / "blank_probe.dll"

    a = imports(left)
    b = imports(right)
    print(f"  {left.name} ({left.stat().st_size // 1024} KB): {len(a)} 个依赖")
    print(f"    {', '.join(a)}")
    print()
    print(f"  {right.name} ({right.stat().st_size // 1024} KB): {len(b)} 个依赖")
    print(f"    {', '.join(b)}")
    print()
    extra = [name for name in a if name not in b]
    print(f"  只在 {left.name} 里（共 {len(extra)} 个）：")
    print(f"    {', '.join(extra) if extra else '无'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Measure each translation unit's contribution to the payload's image, one .obj at a time.

Why this rather than more text-poking: the full payload's `.data` is 97 KB and the skeleton's is 5 KB, a 19x
gap that has to come from somewhere specific. String literals normally land in `.rdata`, so `.data` means
*writable initialised* globals -- and eyeballing the sources did not find anything near 92 KB, which means the
guess was wrong somewhere.

Compiling each file on its own and reading its COFF sections names the contributor without needing a link,
which matters because the units cannot link alone (`ipc_probe.cpp` references symbols from `qsick_pipe.cpp`).

Run: python tools/section_breakdown.py
"""
from __future__ import annotations

import os
import struct
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NATIVE = ROOT / "native" / "qsick-pipe"
SRC = NATIVE / "src"
OUT = ROOT / "temp" / "objbench"

#: COFF section names, in the order worth reporting.
WANTED = (b".text", b".rdata", b".data", b".bss")


def find_vcvars() -> Path | None:
    vswhere = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / \
        "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
    if vswhere.is_file():
        out = subprocess.run(
            [str(vswhere), "-latest", "-products", "*",
             "-requires", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
             "-property", "installationPath"],
            capture_output=True, text=True, timeout=60, check=False).stdout.strip()
        if out:
            candidate = Path(out) / "VC" / "Auxiliary" / "Build" / "vcvars64.bat"
            if candidate.is_file():
                return candidate
    for version in ("18", "2022"):
        candidate = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / \
            "Microsoft Visual Studio" / version / "BuildTools" / "VC" / "Auxiliary" / "Build" / "vcvars64.bat"
        if candidate.is_file():
            return candidate
    return None


def obj_sections(path: Path) -> dict[str, int]:
    """Section sizes from a COFF object file (no PE parsing -- .obj layout is different)."""
    data = path.read_bytes()
    if data[:2] != b"\x64\x86":
        raise ValueError(f"{path.name}: not an x64 COFF object")
    count = struct.unpack_from("<H", data, 2)[0]
    opt_size = struct.unpack_from("<H", data, 16)[0]
    table = 20 + opt_size
    sizes: dict[str, int] = {}
    for index in range(count):
        base = table + index * 40
        raw = data[base:base + 8].rstrip(b"\0")
        # VirtualSize is 0 in .obj files; SizeOfRawData is the one that means something there.
        raw_size = struct.unpack_from("<I", data, base + 16)[0]
        if raw.startswith(b".") or raw.startswith(b"/"):
            name = raw.decode("ascii", "replace")
            sizes[name] = sizes.get(name, 0) + raw_size
    return sizes


def main() -> int:
    vcvars = find_vcvars()
    if vcvars is None:
        print("  找不到 MSVC 工具链")
        return 1
    OUT.mkdir(parents=True, exist_ok=True)

    rows: list[tuple[str, dict[str, int]]] = []
    for source in sorted(SRC.glob("*.cpp")):
        obj = OUT / (source.stem + ".obj")
        # Absolute paths for both: the compiler runs from NATIVE, and a relative output path resolved against
        # that directory silently wrote nowhere -- every file reported "compile failed" while the same command
        # typed by hand worked.
        command = (
            f'call "{vcvars}" >nul 2>&1 && '
            f'cl /nologo /c /MT /O2 /DNDEBUG /W3 "{source}" /Fo:"{obj}"'
        )
        subprocess.run(["cmd", "/c", command], capture_output=True, text=True,
                       timeout=600, check=False, cwd=str(NATIVE))
        if not obj.is_file():
            print(f"  {source.name}: 编译失败")
            continue
        rows.append((source.name, obj_sections(obj)))

    header = "  " + "file".ljust(20) + "".join(n.ljust(11) for n in (".text", ".rdata", ".data"))
    print(header)
    print("  " + "-" * (len(header) - 2))
    totals: dict[str, int] = {}
    for name, sizes in rows:
        line = "  " + name.ljust(20)
        for section in (".text", ".rdata", ".data"):
            value = sizes.get(section, 0) // 1024
            totals[section] = totals.get(section, 0) + sizes.get(section, 0)
            line += f"{value} KB".ljust(11)
        print(line)
    print("  " + "-" * (len(header) - 2))
    line = "  " + "合计".ljust(20)
    for section in (".text", ".rdata", ".data"):
        line += f"{totals.get(section, 0) // 1024} KB".ljust(11)
    print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

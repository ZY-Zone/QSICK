"""Build a reduced variant of the payload for crash bisection.

Builds `skeleton.cpp` (handshake only, no other translation units) to the same output name the real build
uses, so `inject_probe.py` can drive it unchanged. Same compiler flags as `build_pipe_dll.bat`: if the
skeleton survives and the full payload does not, the difference is the linked-in code, not the build options.

Run: python tools/build_variant.py            # skeleton
     python tools/build_variant.py --list
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NATIVE = ROOT / "native" / "qsick-pipe"


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


VARIANTS = {
    # name -> (sources, output dll name)
    "skeleton": (["src/skeleton.cpp"], "skeleton.dll"),
    "blank": (["src/blank_probe.cpp"], "blank_probe.dll"),
}
# One-unit-at-a-time variants were dropped: ipc_probe.cpp, ipc_pipe.cpp and qsick_http.cpp all reference
# symbols from qsick_pipe.cpp, so they do not link alone. The useful comparison is the whole payload against a
# DLL of the same build flags that does nothing.


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("variant", nargs="?", default="skeleton", choices=sorted(VARIANTS))
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()

    if args.list:
        for name in sorted(VARIANTS):
            print(f"  {name}: {' + '.join(VARIANTS[name][0])}")
        return 0

    sources, output = VARIANTS[args.variant]
    vcvars = find_vcvars()
    if vcvars is None:
        print("  找不到 MSVC 工具链")
        return 1

    paths = []
    for source in sources:
        src = NATIVE / source
        if not src.is_file():
            print(f"  源文件不存在: {src}")
            return 1
        paths.append(f'"{src}"')

    command = (
        f'call "{vcvars}" >nul 2>&1 && '
        f'cl /nologo /LD /MT /O2 /DNDEBUG /W3 {" ".join(paths)} '
        f'/Fe:"{NATIVE / output}" /Fo:"{NATIVE / "build" / (args.variant + "-")}" '
        f'/link /DLL /MACHINE:X64'
    )
    result = subprocess.run(["cmd", "/c", command], capture_output=True, text=True,
                            timeout=600, check=False, cwd=str(NATIVE))
    built = NATIVE / output
    # The compiler reports success in the output; its exit code is unreliable through `cmd /c && chaining`,
    # so the artefact is the check that matters.
    if result.returncode != 0 or not built.is_file():
        print(result.stdout[-2000:])
        print(result.stderr[-2000:])
        print(f"  {args.variant} 构建失败")
        return 1
    print(f"  {args.variant} -> {built.name}  {built.stat().st_size // 1024} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

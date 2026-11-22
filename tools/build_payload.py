"""Build the native payload and stamp the distributed binary.

    uv run python tools/build_payload.py [--skip-build]

Writes ``native/qsick-payload/BUILD.txt`` (version, size, SHA-256, dependencies) and
prints the hash so release notes can pin it.
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAYLOAD_DIR = ROOT / "native" / "qsick-payload"
DLL = PAYLOAD_DIR / "qsick_payload.dll"
HOST = PAYLOAD_DIR / "payload_host.exe"
BUILD_TXT = PAYLOAD_DIR / "BUILD.txt"
VERSION = "0.3.0"


def run_build() -> None:
    print("[build] running build.bat")
    result = subprocess.run(["cmd", "/c", "build.bat"], cwd=PAYLOAD_DIR)
    if result.returncode != 0:
        raise SystemExit(f"build failed with exit code {result.returncode}")
    print("[build] ok")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-build", action="store_true", help="stamp the existing DLL")
    args = parser.parse_args()

    if not args.skip_build:
        run_build()
    if not DLL.exists():
        raise SystemExit(f"missing {DLL}; run without --skip-build first")

    data = DLL.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    built_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [
        f"version: {VERSION}",
        f"built_at: {built_at}",
        "file: qsick_payload.dll",
        f"size: {len(data)}",
        f"sha256: {digest}",
        "dependencies: KERNEL32.dll, ADVAPI32.dll (static CRT: no VC++ redistributable)",
        "host: payload_host.exe (test only, not distributed)",
        "source: native/qsick-payload/src (untracked; not distributed)",
    ]
    BUILD_TXT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[build] {DLL.name}: {len(data)} bytes")
    print(f"[build] sha256: {digest}")
    print(f"[build] wrote {BUILD_TXT}")

    if HOST.exists():
        print("[build] payload_host.exe present (integration tests enabled)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

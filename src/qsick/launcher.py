"""Pin one official QQNT build and validate it before QSICK injects.

QSICK does not redistribute QQ. The launcher checks the client it is going to actually use, and can download the
exact official installer (URL + SHA-256) when it is missing.

    uv run qsick-launcher --check
    uv run qsick-launcher --check --root <项目根>\\runtime
    uv run qsick-launcher --download <下载目录>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PINNED_VERSION = "9.9.33-52230"
PINNED_ARCH = "x64"
INSTALLER_URL = (
    "https://qqdl.gtimg.cn/qqfile/QQNT/9.9.33/release/497e2f1f/QQ_9.9.33_260813_x64_01.exe"
)
INSTALLER_SHA256 = "b25c0d3ce9df764074a9118d0ded927e1b2d7ebf60e306112e8df18a040ec492"
INSTALLER_NAME = "QQ_9.9.33_260813_x64_01.exe"
WRAPPER_RELATIVE = Path("versions") / PINNED_VERSION / "resources" / "app" / "wrapper.node"


@dataclass(slots=True)
class InstallInfo:
    root: Path
    version: str
    wrapper: Path
    wrapper_sha256: str
    wrapper_size: int


def common_roots() -> list[Path]:
    """Where to look for the client QSICK will run: `runtime/`, and only that.

    It used to scan the machine's own QQ installs -- `%ProgramFiles%\\Tencent\\QQNT`, `%ProgramFiles(x86)%`, and two
    hardcoded D: paths. All of that is gone, because the runtime never consults a local QQ install:
    `instance_template._source_client` builds a pipe template from `runtime/` or downloads the pinned build from
    the project's own release host, and says so explicitly ("the machine's own QQ install is never consulted").

    Keeping the scan was worse than useless: it reported a status for a client nothing would ever launch, and it
    carried absolute paths belonging to whichever machine wrote it.

    `QSICK_QQ_DIR` still overrides, so a caller with the client somewhere unusual can point at it.
    """
    override = os.environ.get("QSICK_QQ_DIR")
    if override:
        return [Path(override)]
    from .instance_template import runtime_root

    return [runtime_root()]


def sha256_file(path: Path, chunk: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def discover_versions(root: Path) -> list[str]:
    versions = root / "versions"
    if not versions.is_dir():
        return []
    return sorted(
        child.name for child in versions.iterdir() if child.is_dir() and (child / "resources").is_dir()
    )


def inspect_install(root: Path) -> InstallInfo | None:
    config = root / "versions" / "config.json"
    configured = ""
    if config.is_file():
        try:
            payload = json.loads(config.read_text(encoding="utf-8"))
            configured = str(payload.get("curVersion") or payload.get("baseVersion") or "")
        except (OSError, json.JSONDecodeError):
            configured = ""
    candidates: list[tuple[str, Path]] = []
    versions_root = root / "versions"
    if versions_root.is_dir():
        for child in sorted(versions_root.iterdir()):
            wrapper = child / "resources" / "app" / "wrapper.node"
            if wrapper.is_file():
                candidates.append((child.name, wrapper))
    fallback = root / "wrapper.node"
    if not candidates and fallback.is_file():
        candidates.append((configured or "unknown", fallback))
    if not candidates:
        return None
    preferred = next((item for item in candidates if item[0] == configured), candidates[0])
    version, wrapper = preferred
    return InstallInfo(
        root=root,
        version=version or configured or "unknown",
        wrapper=wrapper,
        wrapper_sha256=sha256_file(wrapper),
        wrapper_size=wrapper.stat().st_size,
    )


def check(root: Path | None = None) -> dict[str, Any]:
    roots = [root] if root else common_roots()
    found: list[dict[str, Any]] = []
    for candidate in roots:
        info = inspect_install(candidate)
        if info is not None:
            found.append(
                {
                    "root": str(info.root),
                    "version": info.version or "unknown",
                    "wrapper": str(info.wrapper),
                    "wrapper_size": info.wrapper_size,
                    "wrapper_sha256": info.wrapper_sha256,
                    "pinned": info.version == PINNED_VERSION,
                }
            )
    ready = any(item["pinned"] for item in found)
    return {
        "pinned_version": PINNED_VERSION,
        "pinned_arch": PINNED_ARCH,
        "installer_url": INSTALLER_URL,
        "installer_sha256": INSTALLER_SHA256,
        "status": "ready" if ready else ("version_mismatch" if found else "not_installed"),
        "installs": found,
        "wrapper_relative": str(WRAPPER_RELATIVE),
    }


def download(destination: Path, *, url: str = INSTALLER_URL, expected: str = INSTALLER_SHA256) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / INSTALLER_NAME
    request = urllib.request.Request(url, headers={"User-Agent": "qsick-launcher/1.0"})
    digest = hashlib.sha256()
    with urllib.request.urlopen(request, timeout=60) as response, target.open("wb") as handle:
        while True:
            block = response.read(1024 * 1024)
            if not block:
                break
            handle.write(block)
            digest.update(block)
    actual = digest.hexdigest()
    if actual.lower() != expected.lower():
        target.unlink(missing_ok=True)
        raise RuntimeError(f"installer SHA-256 mismatch: {actual} != {expected}")
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="qsick-launcher", description=__doc__)
    parser.add_argument("--check", action="store_true", help="inspect the installed QQNT build")
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    parser.add_argument("--root", type=Path, help="QQNT install root (auto-detected by default)")
    parser.add_argument("--download", type=Path, metavar="DIR", help="download the pinned official installer")
    args = parser.parse_args(argv)

    if args.download:
        path = download(args.download)
        print(f"downloaded {path} ({path.stat().st_size} bytes), SHA-256 verified")
        return 0

    report = check(args.root)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"pinned: {report['pinned_version']} ({report['pinned_arch']})")
        print(f"status: {report['status']}")
        for item in report["installs"]:
            mark = "OK" if item["pinned"] else "mismatch"
            print(f"  [{mark}] {item['version']}  {item['root']}")
        if report["status"] != "ready":
            print(f"official installer: {INSTALLER_URL}")
            print(f"sha256: {INSTALLER_SHA256}")
    return 0 if report["status"] == "ready" else 1


if __name__ == "__main__":
    sys.exit(main())
